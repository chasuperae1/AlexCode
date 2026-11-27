"""Agent 会话内核分包：上下文与历史：token 统计、压缩、清空、撤销/重做、检查点回退。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

from agentscope.message import SystemMsg, UserMsg


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

    # 顶栏统计的合并刷新间隔：每轮工具结果都可能让它变，但肉眼不需要
    # 更密；token 计数是本地按字节粗算，不做节流会白刷大量 meta 事件。
    META_REFRESH_INTERVAL = 0.6
    # 回复进行中的兜底心跳：框架自己往上下文里塞东西（运行态提示、压缩
    # 摘要）时没有工具事件，只靠钩子会漏，忙碌时每秒补量一次。
    META_TICK = 1.0

    async def _tool_schemas(
        self, agent: Any | None = None, toolkit: Any | None = None
    ) -> list[dict[str, Any]]:
        """当前激活分组下的工具 schema，与模型调用时的集合一致。"""
        agent = self.agent if agent is None else agent
        toolkit = self.toolkit if toolkit is None else toolkit
        if agent is None or toolkit is None:
            return []
        context = getattr(agent.state, "tool_context", None)
        groups = getattr(context, "activated_groups", None) or []
        return await toolkit.get_tool_schemas(groups)

    async def _context_input(
        self, agent: Any | None = None, toolkit: Any | None = None
    ) -> dict[str, Any]:
        """本轮真正送给模型的输入：system prompt + summary + 对话 + 工具。

        压缩阈值就是拿这一份算的（AgentScope ``_prepare_model_input``）。
        顶栏、Tokens 页、压缩前后的数字都必须同口径，否则 summary 和系统
        提示一长，显示值就会比触发值低，看起来像“提前压缩”。
        """
        agent = self.agent if agent is None else agent
        toolkit = self.toolkit if toolkit is None else toolkit
        if agent is None or toolkit is None:
            return {}
        get_prompt = getattr(agent, "_get_system_prompt", None)
        if get_prompt is None:
            return {}
        try:
            messages = [
                SystemMsg(name="system", content=await get_prompt())
            ]
            summary = getattr(agent.state, "summary", None)
            if summary:
                messages.append(UserMsg(name="user", content=summary))
            messages.extend(agent.state.context)
            tools = await self._tool_schemas(agent, toolkit)
        except Exception:
            return {}
        return {"messages": messages, "tools": tools}

    def _stats_stale(self, epoch: int, agent: Any, toolkit: Any, model: Any) -> bool:
        """采样期间 agent / toolkit / model 是否已被替换，或会话已关闭。"""
        return (
            epoch != self._stats_epoch
            or self._closed
            or agent is not self.agent
            or toolkit is not self.toolkit
            or model is not getattr(self.agent, "model", None)
        )

    async def _context_usage(self) -> dict[str, Any]:
        """一次口径一致、带可用性与代际校验的上下文采样。

        ``available=False`` 只表示计数失败或采样已过期，调用方不能把它
        当成“有效但为 0”。顶栏和 Tokens 页都必须走这里，否则一处计数
        失败、另一处工具 schema 计数成功，就会出现“可用 0 占用”。
        """
        agent = self.agent
        toolkit = self.toolkit
        empty: dict[str, Any] = {
            "available": False,
            "used": 0,
            "messages": 0,
            "tools": 0,
            "total": 0,
            "threshold": 0,
            "ratio": 0.0,
            "stale": False,
        }
        if agent is None or toolkit is None:
            return empty
        model = getattr(agent, "model", None)
        epoch = self._stats_epoch
        if model is None:
            return empty
        try:
            kwargs = await self._context_input(agent, toolkit)
            if not kwargs:
                raise ValueError("context input unavailable")
            schemas = kwargs["tools"]
            used = int(await model.count_tokens(**kwargs))
            # 工具单独计数失败时，Messages/Tools 拆解不可信；整体也标不可用。
            tools_tokens = int(
                await model.count_tokens(messages=[], tools=schemas)
            )
            if used < 0 or tools_tokens < 0 or tools_tokens > used:
                raise ValueError("context token count invalid")
            total = int(getattr(model, "context_size", 0) or 0)
            ratio = float(getattr(agent.context_config, "trigger_ratio", 0.0) or 0.0)
            if not total or not ratio:
                raise ValueError("context window unavailable")
        except Exception:
            result = dict(empty)
            result["stale"] = self._stats_stale(epoch, agent, toolkit, model)
            return result
        return {
            "available": True,
            "used": used,
            "messages": max(0, used - tools_tokens),
            "tools": tools_tokens,
            "total": total,
            "threshold": int(total * ratio),
            "ratio": ratio,
            "stale": self._stats_stale(epoch, agent, toolkit, model),
        }

    async def _context_tokens(self) -> int:
        """兼容旧调用：失败与过期都返回 0，需要区分时用 ``_context_usage``。"""
        usage = await self._context_usage()
        if not usage["available"] or usage["stale"]:
            return 0
        return int(usage["used"])

    def _compaction_threshold(self, agent: Any | None = None) -> int:
        """压缩触发点（tokens），与框架 ``trigger_ratio * context_size`` 一致。"""
        agent = self.agent if agent is None else agent
        if agent is None:
            return 0
        try:
            cfg = agent.context_config
            return int(cfg.trigger_ratio * agent.model.context_size)
        except Exception:
            return 0

    @staticmethod
    def _compaction_detail(
        before: int | None,
        threshold: int,
        after: int | None,
        *,
        estimated: bool = False,
    ) -> str:
        """压缩卡的一行：压缩前用量 / 触发阈值 → 压缩后用量。

        只有中间件在真正压缩前量到的值才不加前缀；reply 侧兜底只能用最近
        一次采样，必须显式标 ``est.``。任一侧缺失时写明不可用，不能拿 0
        冒充有效计数。
        """
        if before is None or before <= 0:
            head = "before unavailable"
        else:
            head = ("est. " if estimated else "") + _format_tokens(before)
            if threshold:
                head += f" / {_format_tokens(threshold)}"
        tail = (
            _format_tokens(after)
            if after is not None and after > 0
            else "after unavailable"
        )
        return f"{head} → {tail}"

    async def _record_compaction_async(
        self,
        summary: str,
        before: int | None,
        *,
        estimated: bool = False,
    ) -> None:
        """压缩已经发生，补算压缩后用量再落卡片并刷新顶栏。

        这是没有中间件精确采样时的兜底路径；采样必须属于当前 agent 代际，
        否则直接丢弃，避免把旧模型的用量混到新实例上。
        """
        usage = await self._context_usage()
        if usage["stale"]:
            return
        after = usage["used"] if usage["available"] else None
        threshold = (
            usage["threshold"] if usage["available"] else self._compaction_threshold()
        )
        detail = self._compaction_detail(
            before, threshold, after, estimated=estimated
        )
        self._remember_compaction(summary)
        self._record_compaction(summary, detail)
        await self._update_meta()

    def _remember_compaction(self, summary: str) -> None:
        self._recorded_compactions.append(summary)
        if len(self._recorded_compactions) > 5:
            del self._recorded_compactions[:-5]

    async def _stop_meta_refresh(self) -> None:
        """取消并等待统计刷新退出；清 dirty，禁止 finally 再拉起。"""
        self._meta_halt = True
        self._meta_dirty = False
        task = self._meta_task
        self._meta_task = None
        if task is not None and not task.done():
            await self._cancel_task(task)

    def _resume_meta_refresh(self) -> None:
        """重载结束后解除暂停；关闭状态保持永久停止。"""
        if not self._closed:
            self._meta_halt = False

    def _refresh_meta_soon(self) -> None:
        """请求刷新顶栏；已有刷新在跑时只置脏标记，由它合并处理。"""
        if self._closed or self._meta_halt:
            return
        self._meta_dirty = True
        task = self._meta_task
        if task is not None and not task.done():
            return
        self._meta_task = asyncio.create_task(self._meta_refresh_loop())

    async def _meta_refresh_loop(self) -> None:
        try:
            while True:
                if self._closed or self._meta_halt:
                    break
                self._meta_dirty = False
                await self._update_meta()
                if self._closed or self._meta_halt:
                    break
                # 忙的时候靠心跳持续量；空闲后没有新请求就退出
                if not (self._busy or self._meta_dirty):
                    break
                interval = (
                    self.META_REFRESH_INTERVAL if self._meta_dirty else self.META_TICK
                )
                await asyncio.sleep(interval)
        finally:
            self._meta_task = None
            # 收尾这一瞬新来的请求会看到任务还没结束而直接退出，
            # 这里补一次，避免最后一次统计丢掉；停止/关闭时绝不复活。
            if self._meta_dirty and not (self._closed or self._meta_halt):
                self._refresh_meta_soon()

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
        usage = await self._context_usage()
        if not usage["available"] or usage["stale"]:
            # 计数失败/过期与“有效 0 占用”必须区分：这里保持 available=False，
            # 即使工具 schema 单独计数成功也不冒充可用。
            if usage["total"]:
                data["total"] = usage["total"]
            return data
        data.update(
            used=usage["used"],
            messages=usage["messages"],
            tools=usage["tools"],
            total=usage["total"],
            ratio=usage["ratio"],
            available=True,
        )
        return data

    def _emit_meta(self) -> None:
        """顶栏文案没变就不广播，心跳刷新也不会刷屏事件日志。"""
        payload = {"label": self._meta_base, "workspace": str(workspace_path())}
        key = (payload["label"], payload["workspace"])
        if key == self._meta_last:
            return
        self._meta_last = key
        self.emit("meta", payload)

    async def _update_meta(self) -> None:
        if self.agent is None or self.toolkit is None:
            self._emit_meta()
            return
        name = CONFIG["model"]["name"]
        effort = reasoning.current_level(CONFIG["model"])
        mode = CONFIG.get("permission_mode", "accept")
        label = f"{name} · {effort} · {mode}" if effort else f"{name} · {mode}"
        if self._meta_base in {"", "Starting…"}:
            self._meta_base = label
        usage = await self._context_usage()
        if usage["stale"]:
            # 采样期间 agent/model 已更换：旧值不发布，等新实例自己刷新。
            return
        if not usage["available"] or not usage["used"] or not usage["total"]:
            self._meta_base = f"{label} · context stats unavailable"
            self._emit_meta()
            return
        used = usage["used"]
        total = usage["total"]
        threshold = usage["threshold"]
        self._usage_tokens = used
        self._meta_base = (
            f"{label} · Context {used / 1000:.1f}k/"
            f"{total / 1000:.0f}k ({used / total:.0%})"
            f" · Compact at {threshold / 1000:.1f}k"
        )
        self._emit_meta()

    async def _notify_compacting(self, active: bool) -> None:
        """压缩中途的实时状态，仅用于界面提示。"""
        self.emit("compacting", {"active": active})

    async def _on_compacted(
        self,
        summary: str,
        before: int | None,
        after: int | None,
    ) -> None:
        """中间件在真正压缩前后量到的精确数字，直接落卡。

        这条路径在压缩完成的当下同步执行，卡片不会被异步统计挤到回答后面；
        同轮多次压缩也各落一张，不会复用同一次回复开始时的旧快照。
        """
        if self._closed:
            return
        detail = self._compaction_detail(
            before, self._compaction_threshold(), after
        )
        self._remember_compaction(summary)
        self._record_compaction(summary, detail)
        self._refresh_meta_soon()

    async def compact(self) -> dict[str, Any]:
        agent = self.agent
        if agent is None:
            raise SessionError("not_ready", "session is not ready")
        if not agent.state.context:
            raise SessionError("not_ready", "Nothing to compact yet")
        if self._turn_open():
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
                    detail = self._compaction_detail(
                        before_tokens, self._compaction_threshold(), after_tokens
                    )
                else:
                    detail = f"{before_msgs} → {after_msgs} messages"
                summary = str(getattr(agent.state, "summary", "") or "")
                # 中间件在 compress_context 内已经按精确前后值落过卡，这里
                # 只补没有回调的路径，避免手动压缩出现重复卡。
                if summary not in self._recorded_compactions:
                    self._remember_compaction(summary)
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
        if self._turn_open():
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
        await self._stop_meta_refresh()
        self._stats_epoch += 1
        try:
            model = build_model()
            backend = TrackedLocalBackend(workdir=str(workspace_path(create=True)))
            self._backend = backend
            self.agent, self.toolkit, _ = await build_agent(
                model, backend, self.mcps, gate=self._permission_gate, ask=self._ask_user,
                on_compacting=self._notify_compacting,
                on_compacted=self._on_compacted,
                on_subagent_event=self._on_subagent_event,
            )
        except BaseException:
            self._resume_meta_refresh()
            self._refresh_meta_soon()
            raise
        else:
            self._resume_meta_refresh()
        # 清空后不塞任何占位消息：聊天区应该真正空白，提示只走一次性 toast
        self.history = []
        self._subagents = {}
        self._progress_state = {"text": "", "tone": "primary"}
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
            {**self._history_view(), "checkpoints": []},
        )
        self.emit("checkpoints", {"checkpoints": []})
        self.emit("progress", dict(self._progress_state))
        self._refresh_tasks()
        await self._update_meta()
        self._notice("Session cleared", timeout=2)

    async def undo(self, index: int | None = None) -> dict[str, Any]:
        if self.agent is None or self._snapshots is None:
            raise SessionError("not_ready", "snapshots are not available")
        if self._turn_open():
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
        self._subagents = {}
        self._revert["index"] = index
        self._revert["prompt"] = checkpoint.prompt
        await self._rebuild_agent()
        self.emit(
            "history_reset",
            {
                **self._history_view(),
                "checkpoints": [asdict(item) for item in self._checkpoints],
            },
        )
        self._refresh_tasks()
        await self._update_meta()
        return {"index": index, "files": len(files), "prompt": checkpoint.prompt}

    async def redo(self) -> dict[str, Any]:
        if self.agent is None or self._snapshots is None or self._revert is None:
            raise SessionError("not_ready", "Nothing to redo")
        if self._turn_open():
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
        self._subagents = {}
        self._revert = None
        await self._rebuild_agent()
        self.emit(
            "history_reset",
            {
                **self._history_view(),
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





