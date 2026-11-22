"""Agent 会话内核分包：一轮对话（_reply）与快照检查点记录。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from alex.config import (
    CONFIG,
    LOCKED_SKILLS,
    ROOT,
    lock_skill_files,
    project_path,
    reload_config,
    save_config,
    workspace_path,
)
from alex.core.images import build_user_message, display_text
from alex.core import reasoning
from alex.core.snapshots import Checkpoint, SnapshotError, SnapshotStore, now_label

from alex.session.common import (
    ASSISTANT,
    BOOT_STAGES,
    DEFAULT_WELCOME,
    Emit,
    GOAL_RESTATEMENT_PROMPT,
    GoalRestatement,
    READONLY_TOOLS,
    SessionError,
    USER,
    _format_tokens,
)


class ReplyMixin:
    """一轮对话（_reply）与快照检查点记录。"""

    async def _start_turn(
        self,
        content: str,
        display: str,
        *,
        label: str | None = None,
        bubble: bool = True,
    ) -> str:
        """执行一轮完整回复，返回最终文本（供队列复用）。"""
        if self._revert is not None:
            self.commit_revert()
        history_start = len(self.history)
        self._exchange_seq += 1
        exchange_id = self._exchange_seq
        if bubble:
            self.history.append((USER, display))
            self.emit(
                "exchange_start",
                {"exchange_id": exchange_id, "text": display},
            )
        # 挂到 _reply_task 上，Esc 中断才能取消当前这一轮
        task = asyncio.create_task(
            self._reply(content, history_start, exchange_id, [], label=label)
        )
        self._reply_task = task
        await task
        return self._assistant_since(history_start)

    def _assistant_since(self, start: int) -> str:
        for kind, text in reversed(self.history[start:]):
            if kind == ASSISTANT:
                return text
        return ""

    @staticmethod
    def _one_line(text: str, limit: int = 120) -> str:
        line = " ".join(str(text or "").split())
        return line if len(line) <= limit else line[: limit - 1] + "…"

    @staticmethod
    def _queue_summary(results: list[tuple[str, str, str]]) -> str:
        done = sum(1 for _, status, _ in results if status == "done")
        failed = sum(1 for _, status, _ in results if status == "failed")
        skipped = sum(1 for _, status, _ in results if status == "skipped")
        parts = [f"{done} completed"]
        if failed:
            parts.append(f"{failed} failed")
        if skipped:
            parts.append(f"{skipped} skipped")
        lines = ["Queue finished · " + " · ".join(parts)]
        marks = {"done": "✓", "failed": "✗", "skipped": "–"}
        for subject, status, reason in results:
            line = f"{marks.get(status, '–')} {subject}"
            if reason:
                line += f" — {reason}"
            lines.append(line)
        return "\n".join(lines)

    async def _reply(
        self,
        content: str,
        history_start: int,
        exchange_id: int,
        images: list[Path],
        *,
        label: str | None = None,
        events: Any = None,
    ) -> None:
        from agentscope.event import (
            ExceedMaxItersEvent,
            ModelCallEndEvent,
            ReplyEndEvent,
            ReplyFinishedReason,
            ReplyStartEvent,
            TextBlockDeltaEvent,
            ThinkingBlockDeltaEvent,
            ThinkingBlockEndEvent,
            ThinkingBlockStartEvent,
            ToolCallDeltaEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
            ToolResultStartEvent,
        )

        self._stopping = False
        self._busy = True
        self._phase = "running"
        self._activity = ""
        self.stream_text = ""
        self.thinking = ""
        self._tool_started.clear()
        self._tool_args.clear()
        self._before_content.clear()
        self._tool_labels.clear()
        self._tool_names.clear()
        self._emit_status()
        final_text = ""
        interrupted = False
        started = time.monotonic()
        input_tokens = output_tokens = cache_tokens = 0
        summary_before = getattr(self.agent.state, "summary", None)
        # 上下文压缩可能在 ReAct 循环中途发生，事件流本身不暴露该动作，
        # 用摘要变化来发现并立即上报，而不是等整轮结束
        compacted = False
        context_start = len(self.agent.state.context)
        store = self._snapshots
        mutated = False
        # 上一轮没有写入时工作区未变，直接复用上一棵 tree，省掉一次 git add
        before = self._last_tree
        try:
            if store is not None and store.available and before is None:
                before = await store.snapshot()
                self._last_tree = before or self._last_tree
            source = events
            if source is None:
                source = self.agent.reply_stream(
                    build_user_message(
                        content, images, vision=self._model_supports_vision()
                    ),
                    yield_final_msg=True,
                )
            async for event in source:
                if not isinstance(
                    event, (ThinkingBlockDeltaEvent, TextBlockDeltaEvent)
                ):
                    self._flush_deltas()
                if isinstance(event, ThinkingBlockStartEvent):
                    self.thinking = ""
                    self.emit("thinking_start", {})
                    self._set_activity("Thinking…")
                elif isinstance(event, ThinkingBlockDeltaEvent):
                    self.thinking += event.delta
                    self._queue_delta("thinking_delta", event.delta)
                elif isinstance(event, ThinkingBlockEndEvent):
                    text = self.thinking.strip()
                    if text:
                        self.history.append(("think", text))
                    self.thinking = ""
                    self._flush_deltas()
                    self.emit("thinking_end", {"text": text})
                elif isinstance(event, TextBlockDeltaEvent):
                    self.stream_text += event.delta
                    self._queue_delta("text_delta", event.delta)
                elif isinstance(event, ToolCallStartEvent):
                    if event.tool_call_name not in READONLY_TOOLS:
                        mutated = True
                    self._tool_started[event.tool_call_id] = time.monotonic()
                    self._tool_args[event.tool_call_id] = ""
                    self._tool_names[event.tool_call_id] = event.tool_call_name
                    # AskUser 由 ask_request / ask_end 单独呈现，不写调试记录
                    if event.tool_call_name != "AskUser":
                        kind, label = self._tool_label(event.tool_call_name)
                        # 历史只记一条完成结果，TUI 按行还原，避免开始/结果重复
                        self.emit(
                            "tool_start",
                            {
                                "id": event.tool_call_id,
                                "name": event.tool_call_name,
                                "kind": kind,
                                "label": label,
                            },
                        )
                    self._set_activity(f"Calling {event.tool_call_name}")
                elif isinstance(event, ToolCallDeltaEvent):
                    self._tool_args[event.tool_call_id] = (
                        self._tool_args.get(event.tool_call_id, "") + event.delta
                    )
                    # 参数一拼完就记住文件原内容（此时工具尚未执行）
                    self._capture_before_content(event.tool_call_id)
                    # 参数一完整就把任务名补到行上，不用等子代理跑完
                    if (
                        self._tool_names.get(event.tool_call_id) == "Subagent"
                        and event.tool_call_id not in self._tool_labels
                    ):
                        task = self._tool_arg(event.tool_call_id, "description")
                        if task:
                            self._tool_labels[event.tool_call_id] = task
                            self.emit(
                                "tool_label",
                                {
                                    "id": event.tool_call_id,
                                    "name": "Subagent",
                                    "kind": "tool",
                                    "label": f"Subagent · {task}",
                                },
                            )
                elif isinstance(event, ToolResultStartEvent):
                    name = self._tool_names.get(event.tool_call_id, "Tool")
                    self._set_activity(f"{name} running…")
                elif isinstance(event, ToolResultEndEvent):
                    state = getattr(event.state, "value", event.state)
                    elapsed = time.monotonic() - self._tool_started.pop(
                        event.tool_call_id, time.monotonic()
                    )
                    ok = state == "success"
                    name = self._tool_names.pop(event.tool_call_id, "Tool")
                    self._tool_labels.pop(event.tool_call_id, None)
                    if name == "AskUser":
                        continue
                    label = name
                    skill = ""
                    if name == "Skill":
                        skill = self._skill_arg(event.tool_call_id)
                    elif name == "Read":
                        skill = self._read_skill_arg(event.tool_call_id)
                    elif name in {"PowerShell", "Bash", "Cmd"}:
                        skill = self._command_skill_arg(event.tool_call_id)
                    if skill:
                        label = f"Skill {skill}"
                    elif name == "Subagent":
                        task = self._tool_arg(event.tool_call_id, "description")
                        if task:
                            label = f"Subagent · {task}"
                    kind = "result" if ok else "error"
                    text = f"{label} {'done' if ok else state} · {elapsed:.1f}s"
                    # Edit / Write 成功后立刻给出这次调用自己的 diff，界面可即时挂卡片
                    diff_text = self._tool_change_diff(event.tool_call_id, name)
                    if not ok:
                        diff_text = ""
                    self.history.append((kind, text))
                    self.emit(
                        "tool_result",
                        {
                            "id": event.tool_call_id,
                            "name": name,
                            "kind": kind,
                            "label": label,
                            "skill": skill,
                            "ok": ok,
                            "state": str(state),
                            "elapsed": elapsed,
                            "text": text,
                            "diff": diff_text,
                        },
                    )
                    self._set_activity(text)
                    self._refresh_tasks()
                elif isinstance(event, ModelCallEndEvent):
                    input_tokens += event.input_tokens
                    output_tokens += event.output_tokens
                    cache_tokens += event.cache_input_tokens
                elif isinstance(event, ReplyStartEvent):
                    self._set_activity("Generating reply…")
                elif isinstance(event, ExceedMaxItersEvent):
                    text = "Reached max iterations; reply truncated"
                    self.history.append(("error", text))
                    self.emit("tool_result", {"id": "", "name": "", "kind": "error",
                                              "label": "", "ok": False, "state": "error",
                                              "elapsed": 0.0, "text": text})
                    self._set_activity("Reached max iterations")
                elif isinstance(event, ReplyEndEvent):
                    if event.finished_reason == ReplyFinishedReason.INTERRUPTED:
                        interrupted = True
                        self._set_activity("Stopped")
                    elif event.error is not None:
                        self._notice(
                            f"Reply error: {event.error}",
                            severity="error",
                            timeout=6,
                        )
                elif hasattr(event, "get_text_content"):
                    final_text = event.get_text_content()

                # 推理前框架会自动压缩超阈值的上下文，这里在事件间隙检查
                summary_now = getattr(self.agent.state, "summary", None)
                if summary_now != summary_before:
                    summary_before = summary_now
                    compacted = True
                    self.emit(
                        "compaction",
                        {
                            "summary": str(summary_now or ""),
                            "detail": "older messages summarized",
                        },
                    )
                    asyncio.create_task(self._update_meta())
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as exc:
            self._notice(
                f"Request failed: {exc}",
                title="Request failed",
                severity="error",
                timeout=8,
            )
            if images:
                self._notice(
                    f"{CONFIG['model']['name']} may not support image input; "
                    "switch with /model",
                    severity="warning",
                    timeout=8,
                )
            final_text = f"(Request failed: {exc})"
        finally:
            self._flush_deltas()
            self.stream_text = ""
            if interrupted:
                final_text = "Stopped."
            if final_text:
                self.history.append((ASSISTANT, final_text))
            self._activity = ""
            self._stopping = False
            # 队列执行期间保持忙碌，避免任务之间的空档解锁输入框
            self._busy = self._queue_active
            self._phase = "running" if self._queue_active else "idle"
            self._reply_task = None
            from alex.core.agent import save_state

            try:
                await asyncio.to_thread(save_state, self.agent.state)
            except Exception:
                pass

            duration = time.monotonic() - started
            if input_tokens or output_tokens:
                usage = self._session_usage
                usage["replies"] += 1
                usage["input"] += input_tokens
                usage["output"] += output_tokens
                usage["cached"] += cache_tokens
                usage["last"] = input_tokens + output_tokens
            compacted = compacted or (
                getattr(self.agent.state, "summary", None) != summary_before
            )
            self.emit(
                "reply_end",
                {
                    "text": final_text,
                    "interrupted": interrupted,
                    "duration": duration,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_tokens": cache_tokens,
                    "compacted": compacted,
                },
            )
            self._emit_status()
            self._refresh_tasks()
            self.emit("tasks", self._tasks_payload())
            if before and store is not None:
                asyncio.create_task(
                    self._record_checkpoint(
                        store,
                        before,
                        context_start,
                        history_start,
                        content,
                        exchange_id,
                        input_tokens,
                        output_tokens,
                        cache_tokens,
                        duration,
                        compacted,
                        mutated,
                    )
                )
            asyncio.create_task(self._update_meta())

    async def _record_checkpoint(
        self,
        store: SnapshotStore,
        before: str,
        context_start: int,
        history_start: int,
        prompt: str,
        exchange_id: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_input_tokens: int = 0,
        duration: float = 0.0,
        compacted: bool = False,
        mutated: bool = True,
    ) -> None:
        if mutated:
            try:
                after = await store.snapshot()
            except Exception:
                return
            if not after:
                return
            try:
                files = await store.changed_files(before, after)
            except Exception:
                files = []
            additions = deletions = 0
            try:
                additions, deletions, _count = await store.diff_stat(before, after)
            except Exception:
                pass
        else:
            # 本轮只调用了只读工具：工作区未变，复用快照，省掉 3 次 git 调用
            after = before
            files = []
            additions = deletions = 0
        self._last_tree = after or self._last_tree
        checkpoint = Checkpoint(
            before=before,
            after=after,
            context_start=context_start,
            history_start=history_start,
            prompt=prompt,
            created_at=now_label(),
            files=files,
            exchange_id=exchange_id,
            additions=additions,
            deletions=deletions,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_input_tokens=cache_input_tokens,
            duration=duration,
            compacted=compacted,
        )
        try:
            checkpoints = await store.add_checkpoint(checkpoint)
        except Exception:
            return
        if store is self._snapshots and self._revert is None:
            self._checkpoints = checkpoints
            self.emit(
                "checkpoints",
                {"checkpoints": [asdict(item) for item in self._checkpoints]},
            )
            if checkpoint.files:
                self.emit("changes", {"checkpoint": asdict(checkpoint)})

