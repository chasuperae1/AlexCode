"""Agent 会话内核分包：工具确认、AskUser 往返、中断，以及变更 diff 采集。"""

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


def skill_name_from_path(path: str) -> str:
    """从 skills/<name>/SKILL.md 路径还原技能名；不是技能正文返回空串。"""
    raw = (path or "").strip().strip('"')
    if not raw:
        return ""
    try:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = project_path("skills_dir").parent / target
        target = target.resolve()
        relative = target.relative_to(project_path("skills_dir").resolve())
    except (OSError, RuntimeError, ValueError):
        return ""
    if relative.name.lower() != "skill.md" or len(relative.parts) < 2:
        return ""
    return relative.parent.name


def skill_name_from_command(command: str) -> str:
    """命令行里直接访问 skills/<name>/SKILL.md 时还原技能名。"""
    match = re.search(
        r"(?<![A-Za-z0-9_-])skills[\\/]+([^\\/\s\"']+)[\\/]+SKILL\.md",
        command or "",
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


class ToolsMixin:
    """工具确认、AskUser 往返、中断，以及变更 diff 采集。"""

    async def _confirm_tool(self, agent_name, tool, tool_input, decision):
        from alex.core.permissions import ConfirmChoice, permission_detail

        # 并行子代理可能同时请求确认：排队逐个处理，避免互相覆盖
        async with self._confirm_lock:
            if self._stopping:
                return ConfirmChoice.DENY
            try:
                from alex.screens.diff import tool_diff

                diff_text = tool_diff(tool.name, tool_input, base=workspace_path())
            except Exception:
                diff_text = ""
            request_id = uuid.uuid4().hex
            allow_always = not decision.bypass_immune
            payload = {
                "request_id": request_id,
                "agent": agent_name,
                "tool": tool.name,
                "detail": permission_detail(tool.name, tool_input),
                "reason": decision.decision_reason or "",
                "allow_always": allow_always,
                "diff": diff_text,
            }
            self._pending_confirm = payload
            self._pending_future = asyncio.get_running_loop().create_future()
            self._phase = "waiting_confirm"
            self._set_activity("Waiting for approval…")
            self.emit("confirm_request", payload)
            try:
                choice = await self._pending_future
            except asyncio.CancelledError:
                raise
            finally:
                self._pending_confirm = None
                self._pending_future = None
                self._phase = "running" if self._busy else "idle"
                self._set_activity("")
            return choice

    async def confirm(self, request_id: str, choice: str) -> None:
        """回应一个待决的权限请求。"""
        from alex.core.permissions import ConfirmChoice

        if (
            self._pending_confirm is None
            or self._pending_future is None
            or self._pending_confirm["request_id"] != request_id
        ):
            raise SessionError("stale_confirm", "no matching pending confirmation")
        try:
            value = ConfirmChoice(choice)
        except ValueError as exc:
            raise SessionError("internal", f"unknown choice: {choice}") from exc
        if not self._pending_future.done():
            self._pending_future.set_result(value)
        self.emit("confirm_end", {"request_id": request_id, "choice": value.value})

    def _pending_ask_path(self) -> Path:
        return ROOT / "data" / "pending_ask.json"

    def _save_pending_ask(self, payload: dict[str, Any]) -> None:
        """落盘待答问题，进程被关闭后重启还能恢复。"""
        try:
            path = self._pending_ask_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _clear_pending_ask(self) -> None:
        try:
            self._pending_ask_path().unlink(missing_ok=True)
        except OSError:
            pass

    def _restore_pending_ask(self) -> None:
        """恢复上次未回答的问题；此时原工具调用已不存在，回答按新消息继续。"""
        path = self._pending_ask_path()
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._clear_pending_ask()
            return
        if (
            not isinstance(payload, dict)
            or not payload.get("request_id")
            or not payload.get("question")
        ):
            self._clear_pending_ask()
            return
        self._pending_ask = payload
        self._ask_future = None
        self._phase = "waiting_ask"
        self._set_activity("Waiting for your answer…")
        self.emit("ask_request", payload)

    async def _ask_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把 AskUser 工具的问题转成 UI 事件，并等待用户作答。"""
        if self._stopping:
            return {"skipped": True, "choices": [], "other": ""}
        request_id = uuid.uuid4().hex
        payload = {**payload, "request_id": request_id}
        self._pending_ask = payload
        self._save_pending_ask(payload)
        self._ask_future = asyncio.get_running_loop().create_future()
        self._phase = "waiting_ask"
        self._set_activity("Waiting for your answer…")
        self.emit("ask_request", payload)
        keep_pending = False
        try:
            answer = await self._ask_future
        except asyncio.CancelledError:
            # 用户主动停止（interrupt）才丢弃问题；daemon 退出/重启导致的中断
            # 要保留落盘文件，新进程启动后才能恢复
            keep_pending = not self._stopping
            self.emit(
                "ask_end",
                {
                    "request_id": request_id,
                    "summary": "已取消",
                    "skipped": True,
                    "choices": [],
                    "other": "",
                    "question": payload.get("question", ""),
                },
            )
            raise
        finally:
            self._pending_ask = None
            self._ask_future = None
            if not keep_pending:
                self._clear_pending_ask()
            self._phase = "running" if self._busy else "idle"
            self._set_activity("")
        summary = self._ask_summary(answer)
        self.history.append(("ask", summary))
        self.emit(
            "ask_end",
            {
                "request_id": request_id,
                "summary": summary,
                "skipped": bool(answer.get("skipped")),
                "choices": answer.get("choices", []),
                "other": answer.get("other", ""),
                "question": payload.get("question", ""),
            },
        )
        return answer

    async def answer_ask(
        self,
        request_id: str,
        choices: list[str] | None = None,
        other: str = "",
        skipped: bool = False,
        images: list[str] | None = None,
    ) -> None:
        """回应一个待答的选择题。"""
        if (
            self._pending_ask is None
            or self._pending_ask.get("request_id") != request_id
        ):
            raise SessionError("stale_ask", "no matching pending question")
        image_paths = [str(item) for item in (images or []) if str(item).strip()]
        if skipped:
            answer = {"skipped": True, "choices": [], "other": "", "images": []}
        else:
            answer = {
                "skipped": False,
                "choices": [str(item) for item in (choices or [])],
                "other": str(other or "").strip(),
                "images": image_paths,
            }
            if not answer["choices"] and not answer["other"] and not image_paths:
                raise SessionError("internal", "an answer needs a choice or text")
            allowed = [
                str(item.get("label", ""))
                for item in self._pending_ask.get("options", [])
            ]
            if any(item not in allowed for item in answer["choices"]):
                raise SessionError("internal", "answer contains an unknown option")
            if not self._pending_ask.get("multi"):
                answer["choices"] = answer["choices"][:1]
        if self._ask_future is not None:
            if not self._ask_future.done():
                self._ask_future.set_result(answer)
            return
        # 进程重启后恢复的问题：等待中的工具调用已经结束，
        # 把回答作为一条用户消息交给模型继续完成任务。
        payload = self._pending_ask
        summary = self._ask_summary(answer)
        self._pending_ask = None
        self._clear_pending_ask()
        self._phase = "idle"
        self._set_activity("")
        self.history.append(("ask", summary))
        self.emit(
            "ask_end",
            {
                "request_id": request_id,
                "summary": summary,
                "skipped": bool(answer.get("skipped")),
                "choices": answer.get("choices", []),
                "other": answer.get("other", ""),
                "question": payload.get("question", ""),
            },
        )
        if self._stopping or self.agent is None or self._busy:
            return
        from alex.core.tools import AskUser

        text = AskUser._format_answer(answer, bool(payload.get("multi")))
        self._exchange_seq += 1
        asyncio.create_task(
            self._reply(
                f'Answer to the question "{payload.get("question", "")}": {text}',
                len(self.history),
                self._exchange_seq,
                [Path(item) for item in image_paths],
            )
        )

    @staticmethod
    def _ask_summary(answer: dict[str, Any]) -> str:
        images = [str(item) for item in answer.get("images", []) if str(item)]
        suffix = f"（附 {len(images)} 张图片）" if images else ""
        if answer.get("skipped"):
            return "已跳过 · 按默认策略继续"
        other = str(answer.get("other", "")).strip()
        if other:
            return f"已输入：{other}{suffix}"
        choices = [str(item) for item in answer.get("choices", []) if str(item)]
        if choices:
            return "已选择：" + "、".join(choices) + suffix
        if images:
            return f"已附 {len(images)} 张图片"
        return "已跳过"

    async def interrupt(self) -> None:
        """停止当前回复。"""
        if self._queue_active:
            self._queue_stop = True
            if (
                self._queue_confirming
                and self._queue_decision is not None
                and not self._queue_decision.done()
            ):
                self._queue_decision.set_result("cancel")
        if self._ask_future is not None and not self._ask_future.done():
            self._stopping = True
            self._ask_future.cancel()
        if self._pending_future is not None and not self._pending_future.done():
            self._stopping = True
            self._pending_future.cancel()
        task = self._reply_task
        if task is None or task.done():
            self._stopping = False
            self._emit_status()
            return
        self._stopping = True
        task.cancel()
        # 先终止正在运行的 shell 子进程，再等待 AgentScope 的事件流退出；
        # 否则等待中的工具可能迟迟不响应取消，Esc 会一直停在 stopping。
        if self._backend is not None:
            try:
                await self._backend.kill_all()
            except Exception:
                pass
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        self._stopping = False
        self._emit_status()

    def _capture_before_content(self, tool_call_id: str) -> None:
        """参数刚拼完、工具尚未执行时，记下目标文件的原文。"""
        if tool_call_id in self._before_content:
            return
        name = self._tool_names.get(tool_call_id, "")
        if name not in {"Write", "Edit"}:
            return
        raw = self._tool_args.get(tool_call_id) or ""
        # 参数是逐段到达的：只有对象收尾时才值得尝试解析，避免大文件反复 JSON 解析
        if not raw.rstrip().endswith("}"):
            return
        try:
            args = json.loads(raw)
        except Exception:
            return
        if not isinstance(args, dict):
            return
        raw = str(args.get("file_path") or args.get("path") or "").strip()
        if not raw:
            return
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = workspace_path() / target
        try:
            before = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            before = ""
        self._before_content[tool_call_id] = (raw, before)

    def _tool_change_diff(self, tool_call_id: str, name: str) -> str:
        """把这次 Edit / Write 的改动还原成 unified diff。"""
        from alex.screens.diff import text_diff

        stored = self._before_content.pop(tool_call_id, None)
        if name not in {"Write", "Edit"}:
            return ""
        try:
            args = json.loads(self._tool_args.get(tool_call_id) or "")
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        path = stored[0] if stored else str(
            args.get("file_path") or args.get("path") or ""
        ).strip()
        if not path:
            return ""
        if name == "Write":
            # 拿不到写入前的原文就不出卡片，避免整篇误标成新增
            if stored is None:
                return ""
            return text_diff(path, stored[1], str(args.get("content") or ""))
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if not old:
            return ""
        if stored is None or old not in stored[1]:
            # 原文没读到（新建 / 路径漂移）：退回片段对比，至少能看到改了什么
            return text_diff(path, old, new)
        base = stored[1]
        after = (
            base.replace(old, new)
            if args.get("replace_all")
            else base.replace(old, new, 1)
        )
        return text_diff(path, base, after)

    def _skill_arg(self, tool_call_id: str) -> str:
        raw = self._tool_args.pop(tool_call_id, "")
        try:
            return str(json.loads(raw).get("skill", ""))
        except Exception:
            return ""

    def _read_skill_arg(self, tool_call_id: str) -> str:
        """Read 直接打开 skills/<name>/SKILL.md 时，也视为一次技能读取。"""
        raw = self._tool_args.get(tool_call_id, "")
        try:
            args = json.loads(raw)
        except Exception:
            return ""
        for key in ("file_path", "path"):
            name = skill_name_from_path(str(args.get(key) or ""))
            if name:
                return name
        return ""

    def _command_skill_arg(self, tool_call_id: str) -> str:
        raw = self._tool_args.get(tool_call_id, "")
        try:
            args = json.loads(raw)
        except Exception:
            return ""
        return skill_name_from_command(str(args.get("command") or ""))

    def _tool_arg(self, tool_call_id: str, key: str) -> str:
        try:
            return str(json.loads(self._tool_args.get(tool_call_id, "")).get(key, ""))
        except Exception:
            return ""

    @staticmethod
    def _tool_label(name: str) -> tuple[str, str]:
        local = {"Read", "Write", "Edit", "Glob", "Grep", "PowerShell", "Bash", "Cmd"}
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            server = parts[1] if len(parts) > 1 else "mcp"
            tool = parts[2] if len(parts) > 2 else name
            return "mcp", f"MCP {server} · {tool}"
        if name == "Skill":
            return "skill", "Load skill"
        if name == "Subagent":
            return "tool", "Subagent"
        if name in local or name.startswith("Task"):
            return "tool", f"Tool {name}"
        return "tool", f"Tool {name}"


