"""Agent 会话内核分包：上下文与历史：token 统计、压缩、清空、撤销/重做、检查点回退。"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any


from alex.config import (
    CONFIG,
    project_path,
    workspace_path,
)
from alex.core import reasoning
from alex.core.snapshots import SnapshotError

from alex.session.common import (
    ASSISTANT,
    SessionError,
    _format_tokens,
    record_payload,
)


class HistoryMixin:
    """上下文与历史：token 统计、压缩、清空、撤销/重做、检查点回退。"""

    async def _context_tokens(self) -> int:
        if self.agent is None or self.toolkit is None:
            return 0
        try:
            tools = await self.toolkit.get_tool_schemas()
            return int(
                await self.agent.model.count_tokens(
                    messages=self.agent.state.context, tools=tools
                )
            )
        except Exception:
            return 0

    async def _token_snapshot(self) -> dict[str, Any]:
        """设置页 Tokens 分类的数据：上下文占用、去向拆解与会话累计用量。"""
        model_cfg = CONFIG.get("model", {})
        data: dict[str, Any] = {
            "model": model_cfg.get("name", "?"),
            "permission": CONFIG.get("permission_mode", "accept"),
            "workspace": str(workspace_path()),
            "total": int(model_cfg.get("context_size", 0) or 0),
            "ratio": float(model_cfg.get("context_trigger_ratio", 0.8) or 0.8),
            "used": 0,
            "messages": 0,
            "tools": 0,
            "available": False,
            "session": dict(self._session_usage),
        }
        if self.agent is None or self.toolkit is None:
            return data
        try:
            model = self.agent.model
            schemas = await self.toolkit.get_tool_schemas()
            used = int(
                await model.count_tokens(
                    messages=self.agent.state.context, tools=schemas
                )
            )
            tools_tokens = int(
                await model.count_tokens(messages=[], tools=schemas)
            )
            data.update(
                used=used,
                messages=max(0, used - tools_tokens),
                tools=tools_tokens,
                total=getattr(model, "context_size", 0) or data["total"],
                ratio=self.agent.context_config.trigger_ratio,
                available=True,
            )
        except Exception:  # 统计失败时退回粗算值
            pass
        return data

    async def _update_meta(self) -> None:
        if self.agent is None or self.toolkit is None:
            self.emit("meta", {"label": self._meta_base, "workspace": str(workspace_path())})
            return
        name = CONFIG["model"]["name"]
        effort = reasoning.current_level(CONFIG["model"])
        mode = CONFIG.get("permission_mode", "accept")
        label = f"{name} · {effort} · {mode}" if effort else f"{name} · {mode}"
        if self._meta_base in {"", "Starting…"}:
            self._meta_base = label
        try:
            model = self.agent.model
            tools = await self.toolkit.get_tool_schemas()
            used = await model.count_tokens(
                messages=self.agent.state.context, tools=tools
            )
            total = getattr(model, "context_size", 0) or 0
            trigger = self.agent.context_config.trigger_ratio
            if not total:
                self._meta_base = label
            else:
                remain = max(0, int(total * trigger) - used)
                self._meta_base = (
                    f"{label} · Context {used / 1000:.1f}k/"
                    f"{total / 1000:.0f}k ({used / total:.0%})"
                    f" · Compact in {remain / 1000:.1f}k"
                )
        except Exception:
            self._meta_base = f"{label} · context stats unavailable"
        self.emit("meta", {"label": self._meta_base, "workspace": str(workspace_path())})

    async def _notify_compacting(self, active: bool) -> None:
        """压缩中途的实时状态，仅用于界面提示。"""
        self.emit("compacting", {"active": active})

    async def compact(self) -> dict[str, Any]:
        agent = self.agent
        if agent is None:
            raise SessionError("not_ready", "session is not ready")
        if not agent.state.context:
            raise SessionError("not_ready", "Nothing to compact yet")
        if self._busy or self._queue_active:
            raise SessionError("busy", "a reply is still running")
        self._set_busy(True)
        self._set_activity("Compacting context…")
        self.emit("compacting", {"active": True})
        try:
            before_tokens = await self._context_tokens()
            before_msgs = len(agent.state.context)
            try:
                await agent.compress_context()
                after_tokens = await self._context_tokens()
                if (
                    after_tokens >= before_tokens
                    and len(agent.state.context) >= before_msgs
                ):
                    manual = agent.context_config.model_copy(
                        update={
                            "trigger_ratio": 0.05,
                            "reserve_ratio": 0.04,
                            "context_buffer_ratio": 0.0,
                        }
                    )
                    await agent.compress_context(context_config=manual)
                    after_tokens = await self._context_tokens()
            except Exception as exc:
                raise SessionError("internal", f"Compact failed: {exc}") from exc
            from alex.core.agent import save_state

            save_state(agent.state)
            await self._update_meta()
            after_msgs = len(agent.state.context)
            if after_msgs < before_msgs or (
                before_tokens and after_tokens < before_tokens
            ):
                if before_tokens and after_tokens:
                    detail = (
                        f"{_format_tokens(before_tokens)} → "
                        f"{_format_tokens(after_tokens)} tokens"
                    )
                else:
                    detail = f"{before_msgs} → {after_msgs} messages"
                summary = str(getattr(agent.state, "summary", "") or "")
                self._record_compaction(summary, detail)
                return {"compacted": True}
            raise SessionError(
                "not_ready", "Context is below the compaction threshold; nothing to do"
            )
        finally:
            self._set_activity("")
            self.emit("compacting", {"active": False})
            self._set_busy(False)

    async def clear_session(self) -> None:
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        from alex.core.agent import build_agent, build_model
        from alex.core.tools import TrackedLocalBackend

        path = project_path("session_file")
        if path.is_file():
            path.unlink()
        self._clear_history_file()
        self._pending_ask = None
        self._ask_future = None
        self._clear_pending_ask()
        if self._snapshots is not None:
            self._snapshots.clear_checkpoints()
        self._checkpoints = []
        self._revert = None
        model = build_model()
        backend = TrackedLocalBackend(workdir=str(workspace_path(create=True)))
        self._backend = backend
        self.agent, self.toolkit, _ = await build_agent(
            model, backend, self.mcps, gate=self._permission_gate, ask=self._ask_user,
            on_compacting=self._notify_compacting
        )
        self.history = [(ASSISTANT, "Session cleared. What should we do next?")]
        self.stream_text = ""
        self.thinking = ""
        self._session_usage = {
            "replies": 0,
            "input": 0,
            "output": 0,
            "cached": 0,
            "last": 0,
        }
        self.emit(
            "history_reset",
            {"history": self._history_payload(), "checkpoints": []},
        )
        self.emit("checkpoints", {"checkpoints": []})
        self._refresh_tasks()
        await self._update_meta()
        self._notice("Session cleared", timeout=2)

    async def undo(self, index: int | None = None) -> dict[str, Any]:
        if self.agent is None or self._snapshots is None:
            raise SessionError("not_ready", "snapshots are not available")
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        if not self._checkpoints:
            raise SessionError("not_ready", "No checkpoints yet")
        if index is None:
            if self._revert is not None:
                index = int(self._revert.get("index", len(self._checkpoints))) - 1
            else:
                index = len(self._checkpoints) - 1
        if index < 0:
            raise SessionError("not_ready", "No earlier checkpoint")
        if index >= len(self._checkpoints):
            raise SessionError("not_ready", "Invalid checkpoint")
        checkpoint = self._checkpoints[index]

        created = self._revert is None
        if created:
            try:
                current = await self._snapshots.snapshot()
            except Exception as exc:
                raise SessionError(
                    "internal", f"Cannot snapshot current state: {exc}"
                ) from exc
            if not current:
                raise SessionError(
                    "internal", "Cannot snapshot current state; undo aborted"
                )
            self._revert = {
                "index": index,
                "prompt": checkpoint.prompt,
                "current": current,
                "context": list(self.agent.state.context),
            }

        try:
            files = await self._snapshots.restore(checkpoint.before)
        except (SnapshotError, OSError) as exc:
            if created:
                self._revert = None
            raise SessionError("internal", f"Undo failed: {exc}") from exc
        self._last_tree = None

        saved_context = self._revert["context"]
        if checkpoint.context_start > len(saved_context):
            start = len(saved_context)
            self._notice(
                "Context was compacted; only files were reverted",
                severity="warning",
                timeout=5,
            )
        else:
            start = checkpoint.context_start
        self.agent.state.context = list(saved_context[:start])
        self.agent.state.tool_context.read_file_cache = []
        from alex.core.agent import save_state

        save_state(self.agent.state)

        self.history = self._history_from_state(self.agent.state)
        self._revert["index"] = index
        self._revert["prompt"] = checkpoint.prompt
        await self._rebuild_agent()
        self.emit(
            "history_reset",
            {
                "history": self._history_payload(),
                "checkpoints": [asdict(item) for item in self._checkpoints],
            },
        )
        self._refresh_tasks()
        await self._update_meta()
        return {"index": index, "files": len(files), "prompt": checkpoint.prompt}

    async def redo(self) -> dict[str, Any]:
        if self.agent is None or self._snapshots is None or self._revert is None:
            raise SessionError("not_ready", "Nothing to redo")
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        revert = self._revert
        try:
            files = await self._snapshots.restore(revert["current"])
        except (SnapshotError, OSError) as exc:
            raise SessionError("internal", f"Redo failed: {exc}") from exc
        self._last_tree = None
        self.agent.state.context = list(revert["context"])
        self.agent.state.tool_context.read_file_cache = []
        from alex.core.agent import save_state

        save_state(self.agent.state)
        self.history = self._history_from_state(self.agent.state)
        self._revert = None
        await self._rebuild_agent()
        self.emit(
            "history_reset",
            {
                "history": self._history_payload(),
                "checkpoints": [asdict(item) for item in self._checkpoints],
            },
        )
        self._refresh_tasks()
        await self._update_meta()
        return {"files": len(files)}

    def commit_revert(self) -> None:
        if self._revert is None:
            return
        index = int(self._revert.get("index", len(self._checkpoints)))
        self._revert = None
        self._checkpoints = self._checkpoints[:index]
        if self._snapshots is not None:
            self._snapshots.save_checkpoints(self._checkpoints)
        self.emit(
            "checkpoints", {"checkpoints": [asdict(item) for item in self._checkpoints]}
        )





