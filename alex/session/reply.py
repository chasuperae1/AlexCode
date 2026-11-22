"""Agent 会话内核分包：一轮对话（_reply）与快照检查点记录。"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from alex.config import CONFIG, workspace_path
from alex.core.images import build_user_message
from alex.core.snapshots import Checkpoint, now_label

from alex.session.common import (
    ASSISTANT,
    READONLY_TOOLS,
    USER,
)


@dataclass
class ReplyStats:
    """一轮回复的统计快照，供检查点与设置页复用。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_input_tokens: int = 0
    duration: float = 0.0
    compacted: bool = False
    mutated: bool = True


@dataclass
class _ReplyRun:
    """单轮回复的可变状态，替代散落在 _reply 里的临时变量。"""

    history_start: int
    exchange_id: int
    started: float
    summary_before: Any
    context_start: int
    store: Any
    before: str | None
    final_text: str = ""
    interrupted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    compacted: bool = False
    mutated: bool = False


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
            self._save_history()
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

        self._begin_turn()
        run = _ReplyRun(
            history_start=history_start,
            exchange_id=exchange_id,
            started=time.monotonic(),
            summary_before=getattr(self.agent.state, "summary", None),
            context_start=len(self.agent.state.context),
            store=self._snapshots,
            before=self._last_tree,
        )
        handlers = {
            ThinkingBlockStartEvent: self._on_thinking_start,
            ThinkingBlockDeltaEvent: self._on_thinking_delta,
            ThinkingBlockEndEvent: self._on_thinking_end,
            TextBlockDeltaEvent: self._on_text_delta,
            ToolCallStartEvent: self._on_tool_call_start,
            ToolCallDeltaEvent: self._on_tool_call_delta,
            ToolResultStartEvent: self._on_tool_result_start,
            ToolResultEndEvent: self._on_tool_result_end,
            ModelCallEndEvent: self._on_model_call_end,
            ReplyStartEvent: self._on_reply_start,
            ExceedMaxItersEvent: self._on_exceed_max_iters,
            ReplyEndEvent: self._on_reply_end,
        }
        try:
            await self._ensure_baseline(run)
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
                handler = handlers.get(type(event))
                if handler is not None:
                    handler(event, run)
                elif hasattr(event, "get_text_content"):
                    run.final_text = event.get_text_content()
                self._check_compaction(run)
        except asyncio.CancelledError:
            run.interrupted = True
            raise
        except Exception as exc:
            self._report_failure(exc, images, run)
        finally:
            await self._finish_reply(run, content)

    def _begin_turn(self) -> None:
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

    async def _ensure_baseline(self, run: _ReplyRun) -> None:
        # 上一轮没有写入时工作区未变，直接复用上一棵 tree，省掉一次 git add
        if run.store is None or not run.store.available or run.before is not None:
            return
        run.before = await run.store.snapshot()
        self._last_tree = run.before or self._last_tree

    def _check_compaction(self, run: _ReplyRun) -> None:
        # 推理前框架会自动压缩超阈值的上下文，这里在事件间隙检查
        summary_now = getattr(self.agent.state, "summary", None)
        if summary_now == run.summary_before:
            return
        run.summary_before = summary_now
        run.compacted = True
        self.emit(
            "compaction",
            {
                "summary": str(summary_now or ""),
                "detail": "older messages summarized",
            },
        )
        asyncio.create_task(self._update_meta())

    def _report_failure(self, exc: Exception, images: list[Path], run: _ReplyRun) -> None:
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
        run.final_text = f"(Request failed: {exc})"

    async def _finish_reply(self, run: _ReplyRun, content: str) -> None:
        self._flush_deltas()
        self.stream_text = ""
        if run.interrupted:
            run.final_text = "Stopped."
        if run.final_text:
            self.history.append((ASSISTANT, run.final_text))
            self._save_history()
        self._activity = ""
        self._stopping = False
        # 队列执行期间保持忙碌，避免任务之间的空档解锁输入框
        self._busy = self._queue_active
        self._phase = "running" if self._queue_active else "idle"
        self._reply_task = None
        from alex.core.agent import save_state

        try:
            await asyncio.to_thread(save_state, self.agent.state)
        except Exception:  # 尽力保存会话状态，失败不阻断收尾
            pass

        duration = time.monotonic() - run.started
        if run.input_tokens or run.output_tokens:
            usage = self._session_usage
            usage["replies"] += 1
            usage["input"] += run.input_tokens
            usage["output"] += run.output_tokens
            usage["cached"] += run.cache_tokens
            usage["last"] = run.input_tokens + run.output_tokens
        run.compacted = run.compacted or (
            getattr(self.agent.state, "summary", None) != run.summary_before
        )
        stats = ReplyStats(
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cache_input_tokens=run.cache_tokens,
            duration=duration,
            compacted=run.compacted,
            mutated=run.mutated,
        )
        self.emit(
            "reply_end",
            {
                "text": run.final_text,
                "interrupted": run.interrupted,
                "duration": duration,
                "input_tokens": stats.input_tokens,
                "output_tokens": stats.output_tokens,
                "cache_tokens": stats.cache_input_tokens,
                "compacted": stats.compacted,
            },
        )
        self._emit_status()
        self._refresh_tasks()
        self.emit("tasks", self._tasks_payload())
        if run.before and run.store is not None:
            asyncio.create_task(
                self._record_checkpoint(run, content, stats)
            )
        asyncio.create_task(self._update_meta())

    # ---- 事件处理 ----

    def _persist_intermediate_text(self) -> None:
        """工具/新一轮思考之前，把已流式显示的 assistant 文本落进 history。

        模型一次工具循环里可能出现“先说话 → 调工具 → 再说话”，这些中间
        文本原先只存在于当前 TUI，重建聊天区后会消失；这里按 assistant
        消息保存，最终回答仍由 _finish_reply 追加。
        """
        text = self.stream_text.strip()
        if not text:
            return
        self.history.append((ASSISTANT, text))
        self._save_history()
        self.stream_text = ""

    def _on_thinking_start(self, event: Any, run: _ReplyRun) -> None:
        self._persist_intermediate_text()
        self.thinking = ""
        self.emit("thinking_start", {})
        self._set_activity("Thinking…")

    def _on_thinking_delta(self, event: Any, run: _ReplyRun) -> None:
        self.thinking += event.delta
        self._queue_delta("thinking_delta", event.delta)

    def _on_thinking_end(self, event: Any, run: _ReplyRun) -> None:
        text = self.thinking.strip()
        if text:
            self.history.append(("think", text))
            self._save_history()
        self.thinking = ""
        self._flush_deltas()
        self.emit("thinking_end", {"text": text})

    def _on_text_delta(self, event: Any, run: _ReplyRun) -> None:
        self.stream_text += event.delta
        self._queue_delta("text_delta", event.delta)

    def _on_tool_call_start(self, event: Any, run: _ReplyRun) -> None:
        self._persist_intermediate_text()
        if event.tool_call_name not in READONLY_TOOLS:
            run.mutated = True
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

    def _on_tool_call_delta(self, event: Any, run: _ReplyRun) -> None:
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

    def _on_tool_result_start(self, event: Any, run: _ReplyRun) -> None:
        name = self._tool_names.get(event.tool_call_id, "Tool")
        self._set_activity(f"{name} running…")

    def _on_tool_result_end(self, event: Any, run: _ReplyRun) -> None:
        state = getattr(event.state, "value", event.state)
        elapsed = time.monotonic() - self._tool_started.pop(
            event.tool_call_id, time.monotonic()
        )
        ok = state == "success"
        name = self._tool_names.pop(event.tool_call_id, "Tool")
        self._tool_labels.pop(event.tool_call_id, None)
        if name == "AskUser":
            return
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
        shell = name in {"PowerShell", "Bash", "Cmd", "Shell"}
        result_text = self._tool_result_text(event.tool_call_id) if shell else ""
        stats = self._shell_stats(result_text, ok) if shell else {}
        payload = None
        if shell:
            payload = {
                "tool": name,
                "command": self._tool_arg(event.tool_call_id, "command"),
                "cwd": str(CONFIG.get("workspace") or workspace_path()),
                "allow_always": False,
                "rules": [],
            }
        # shell 权限卡已经在 history 里占位，完成后原地改状态，不再追加一行重复结果
        card_logged = self._finish_shell_card(
            name,
            ok,
            text,
            stats=stats,
            payload=payload,
        )
        # Edit / Write 成功后立刻给出这次调用自己的 diff，界面可即时挂卡片
        diff_text = self._tool_change_diff(event.tool_call_id, name)
        if not ok:
            diff_text = ""
        if not card_logged:
            self.history.append((kind, text))
        self._save_history()
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
                "card_logged": card_logged,
                "command": payload.get("command", "") if payload else "",
                "stdout": stats.get("stdout", ""),
                "line_count": stats.get("line_count", 0),
                "exit_code": stats.get("exit_code"),
            },
        )
        self._set_activity(text)
        self._refresh_tasks()

    def _tool_result_text(self, tool_call_id: str) -> str:
        """从 agent context 的 tool_result block 取 stdout/stderr 正文。"""
        context = getattr(getattr(self.agent, "state", None), "context", None) or []
        for message in reversed(context[-8:]):
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                block_id = (
                    block.get("id")
                    if isinstance(block, dict)
                    else getattr(block, "id", None)
                )
                block_type = (
                    block.get("type")
                    if isinstance(block, dict)
                    else getattr(block, "type", "")
                )
                if str(block_id or "") != str(tool_call_id) or block_type != "tool_result":
                    continue
                output = (
                    block.get("output")
                    if isinstance(block, dict)
                    else getattr(block, "output", "")
                )
                return self._block_text(output)[:30000]
        return ""

    @classmethod
    def _block_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return "\n".join(
                part for item in value if (part := cls._block_text(item)).strip()
            )
        if isinstance(value, dict):
            for key in ("text", "content", "output"):
                if key in value:
                    return cls._block_text(value[key])
            return ""
        text = getattr(value, "text", None)
        if text is not None:
            return str(text)
        return str(value)

    @staticmethod
    def _shell_stats(text: str, ok: bool) -> dict[str, Any]:
        clean = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line for line in clean.splitlines() if line.strip()]
        exit_code: int | None = None
        for line in lines[-4:]:
            match = re.search(
                r"(?:exit code|exit)\s*\]?\s*[:=]?\s*(-?\d+)", line, re.I
            )
            if match:
                exit_code = int(match.group(1))
                break
        if exit_code is None:
            exit_code = 0 if ok else 1
        return {
            "stdout": clean[:20000],
            "line_count": len(lines),
            "exit_code": exit_code,
        }


    def _on_model_call_end(self, event: Any, run: _ReplyRun) -> None:
        run.input_tokens += event.input_tokens
        run.output_tokens += event.output_tokens
        run.cache_tokens += event.cache_input_tokens

    def _on_reply_start(self, event: Any, run: _ReplyRun) -> None:
        self._set_activity("Generating reply…")

    def _on_exceed_max_iters(self, event: Any, run: _ReplyRun) -> None:
        text = "Reached max iterations; reply truncated"
        self.history.append(("error", text))
        self.emit(
            "tool_result",
            {
                "id": "",
                "name": "",
                "kind": "error",
                "label": "",
                "ok": False,
                "state": "error",
                "elapsed": 0.0,
                "text": text,
            },
        )
        self._set_activity("Reached max iterations")

    def _on_reply_end(self, event: Any, run: _ReplyRun) -> None:
        reason = getattr(event.finished_reason, "value", event.finished_reason)
        if reason == "interrupted":
            run.interrupted = True
            self._set_activity("Stopped")
        elif event.error is not None:
            self._notice(
                f"Reply error: {event.error}",
                severity="error",
                timeout=6,
            )

    async def _record_checkpoint(
        self,
        run: _ReplyRun,
        prompt: str,
        stats: ReplyStats | None = None,
    ) -> None:
        """按本轮状态记录检查点：工具改过文件时才落一次快照。"""
        stats = stats or ReplyStats()
        store, before = run.store, run.before
        context_start, history_start = run.context_start, run.history_start
        exchange_id = run.exchange_id
        if stats.mutated:
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
            except Exception:  # 统计失败保持 0，只影响展示
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
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            cache_input_tokens=stats.cache_input_tokens,
            duration=stats.duration,
            compacted=stats.compacted,
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
