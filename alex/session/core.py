"""Agent 会话内核分包：会话生命周期：初始化、快照/历史负载、关闭、状态与提示。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable


from alex.config import (
    CONFIG,
    project_path,
    reload_config,
    workspace_path,
)
from alex.core.images import display_text
from alex.core.snapshots import Checkpoint, SnapshotStore

from alex.session.common import (
    ASSISTANT,
    BOOT_STAGES,
    Emit,
    SessionError,
    USER,
)


from alex.session.boot import BootMixin
class CoreMixin(BootMixin):
    """会话生命周期：初始化、快照/历史负载、关闭、状态与提示。"""

    def __init__(self, emit: Emit, *, builder: Callable[[], Any] | None = None):
        self.emit = emit
        self._builder = builder
        self.history: list[tuple[str, str]] = []
        self.stream_text = ""
        self.thinking = ""
        self.agent = None
        self._model = None
        self.toolkit = None
        self.mcps: list = []
        self._mcp_tools_by_server: dict[str, list[str]] = {}
        self._backend = None
        self._permission_gate = None
        self._snapshots: SnapshotStore | None = None
        self._checkpoints: list[Checkpoint] = []
        self._revert: dict | None = None
        self._exchange_seq = 0
        self._busy = False
        self._activity = ""
        self._stopping = False
        self._phase = "initializing"
        self._meta_base = ""
        self._reply_task: asyncio.Task | None = None
        # /queue：串行任务队列的运行时状态
        self._queue_task: asyncio.Task | None = None
        self._queue_active = False
        self._queue_stop = False
        self._queue_index = 0
        self._queue_total = 0
        # 规划完成后的确认阶段：等用户 Enter 开始 / Esc 丢弃
        self._queue_confirming = False
        self._queue_decision: asyncio.Future | None = None
        self._queue_task_ids: list[str] = []
        self._queue_subjects: list[str] = []
        self._queue_card_index: int | None = None
        # 队列开始前已存在的任务 id，用于兜底清理规划途中新建的任务
        self._queue_before_ids: set[str] = set()
        # /goal：进行中的目标卡片（历史内原地更新）与进度标签
        self._goal_state: dict[str, Any] | None = None
        self._goal_card_index: int | None = None
        self._goal_confirming = False
        self._goal_task: asyncio.Task | None = None
        self._goal_retry_task: asyncio.Task | None = None
        self._progress_state: dict[str, str] = {"text": "", "tone": "primary"}
        self._pending_confirm: dict[str, Any] | None = None
        self._pending_future: asyncio.Future | None = None
        # shell 权限卡在 history 里的位置：换主题 / 重开 TUI 后据此重建
        self._shell_card_index: int | None = None
        # 并行子代理可能同时请求确认：串行化为一次一个
        self._confirm_lock = asyncio.Lock()
        self._pending_ask: dict[str, Any] | None = None
        self._ask_future: asyncio.Future | None = None
        self._tool_started: dict[str, float] = {}
        self._tool_args: dict[str, str] = {}
        # 参数拼完、工具还没执行时记下目标文件原内容，供 Edit / Write 出 diff
        self._before_content: dict[str, tuple[str, str]] = {}
        self._tool_labels: dict[str, str] = {}
        self._tool_names: dict[str, str] = {}
        self._snapshot_warned = False
        self._last_tree: str | None = None
        self._delta_buffer: dict[str, str] = {}
        self._delta_handle: asyncio.TimerHandle | None = None
        self._last_status: dict | None = None
        self._closed = False
        self._ready = asyncio.Event()
        self._boot: dict[str, dict[str, str]] = {
            key: {"label": label, "status": "idle", "detail": ""}
            for key, label in BOOT_STAGES
        }
        # 本次会话累计用量（daemon 重启后重新计数），用于设置页 Tokens 分类
        self._session_usage = {
            "replies": 0,
            "input": 0,
            "output": 0,
            "cached": 0,
            "last": 0,
        }


    @staticmethod
    def _history_from_state(state) -> list[tuple[str, str]]:
        roles = {"user": USER, "assistant": ASSISTANT}
        history: list[tuple[str, str]] = []
        for message in state.context:
            role = roles.get(message.role)
            if role is None:
                continue
            try:
                text = message.get_text_content().strip()
            except Exception:
                continue
            if text:
                history.append((role, text))
        return history[-40:]

    def _history_file(self) -> Path:
        """显示用历史（含卡片的顺序）落盘位置，和 agent 状态文件放在一起。"""
        path = project_path("session_file")
        return path.with_suffix(".history.json")

    def _save_history(self) -> None:
        """保存完整显示历史：重启后 shell 卡、步骤摘要不会只剩问答。"""
        try:
            path = self._history_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            items = []
            for kind, text in self.history[-400:]:
                if len(text) > 20000:
                    text = text[:20000] + "…"
                items.append([kind, text])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": 1, "history": items}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:  # 历史落盘失败不阻断对话
            pass

    def _load_history(self) -> list[tuple[str, str]] | None:
        try:
            path = self._history_file()
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        items = data.get("history") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None
        history: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            history.append((str(item[0]), str(item[1])))
        return history

    def _restore_history(
        self, fallback: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """用用户原话对齐保存的显示历史，避免撤销/压缩后错位。

        显示历史里会有多段中间 assistant 文本、ask、shell 卡等，agent state
        的 context 通常只有最终消息；不能再按 assistant 数量逐条比对，否则
        一有中间文本就会误判为“对不上”，把卡片历史整份丢掉。
        """
        saved = self._load_history()
        if not saved:
            return fallback
        state_users = [
            text.strip() for kind, text in fallback if kind == USER
        ]
        saved_users = [
            text.strip() for kind, text in saved if kind == USER
        ]
        if not state_users:
            # state 没有可对齐的对话，但显示历史存在（例如杀进程时刚收尾），
            # 保留已落盘的卡片与文本。
            return saved[-400:]
        if len(saved_users) < len(state_users):
            return fallback
        if saved_users[-len(state_users):] != state_users:
            return fallback
        return saved[-400:]

    def _clear_history_file(self) -> None:
        try:
            self._history_file().unlink(missing_ok=True)
        except OSError:  # 历史文件不存在或已删除
            pass

    async def _stop_queue_task(self) -> None:
        """停止队列任务：先放行等待确认的 future，再取消并等待退出。"""
        task = self._queue_task
        if task is None or task.done():
            return
        self._queue_stop = True
        decision = self._queue_decision
        if decision is not None and not decision.done():
            decision.set_result("cancel")
        await self._cancel_task(task)

    @staticmethod
    async def _cancel_task(task) -> None:
        """取消后台任务并等待其退出；取消与异常都属收尾预期。"""
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # 取消任务属收尾预期
            pass
        except Exception:  # 任务退出异常不影响关闭
            pass

    async def close(self) -> None:
        """保存会话并释放子进程；忙碌时不落盘。"""
        if self._closed:
            return
        self._closed = True
        await self._stop_queue_task()
        if self._reply_task is not None and not self._reply_task.done():
            await self._cancel_task(self._reply_task)
        if self._backend is not None:
            try:
                await self._backend.kill_all()
            except Exception:  # 清理子进程失败不影响退出
                pass
        if self.agent is not None and not self._busy:
            from alex.core.agent import save_state

            try:
                save_state(self.agent.state)
                self._save_history()
            except Exception:  # 尽力保存；失败不阻断关闭
                pass
        for client in self.mcps:
            try:
                if client.is_stateful and client.is_connected:
                    await client.close()
            except Exception:  # MCP 连接关闭失败可忽略
                pass

    @property
    def phase(self) -> str:
        return self._phase

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self._phase,
            "history": self._history_payload(),
            "stream_text": self.stream_text,
            "thinking": self.thinking,
            "busy": self._busy,
            "activity": self._activity,
            "meta": self._meta_base,
            "workspace": str(workspace_path()),
            "exchange_seq": self._exchange_seq,
            "checkpoints": [asdict(item) for item in self._checkpoints],
            "pending_confirm": self._pending_confirm,
            "pending_ask": self._pending_ask,
            "tasks": self._tasks_payload(),
            "progress": dict(self._progress_state),
            "goal_phase": (self._goal_state or {}).get("phase", ""),
            "goal_confirmation": {
                "active": self._goal_confirming,
                "goal": (self._goal_state or {}).get("goal", ""),
            },
            "queue": {
                "active": self._queue_active,
                "confirming": self._queue_confirming,
                "index": self._queue_index,
                "total": self._queue_total,
            },
            "boot": [
                {"stage": key, **entry} for key, entry in self._boot.items()
            ],
        }

    def _history_payload(self) -> list[list[str]]:
        """限制单条文本与总条数，避免快照超过单行传输上限。"""
        items = self.history[-400:]
        payload: list[list[str]] = []
        for kind, text in items:
            if len(text) > 20000:
                text = text[:20000] + "…"
            payload.append([kind, text])
        return payload

    def _tasks_payload(self) -> dict[str, Any]:
        if self.agent is None:
            return {"visible": False, "summary": ""}
        if self._queue_active:
            # 队列进度改到输入框左上角，顶部不再占用一行
            return {"visible": False, "summary": ""}
        tasks = self.agent.state.tasks_context.tasks
        if not tasks:
            return {"visible": False, "summary": ""}
        done = sum(1 for task in tasks if task.state == "completed")
        # 全部完成且不在队列中时收起置顶栏，避免历史任务长期占位
        if done == len(tasks):
            return {"visible": False, "summary": ""}
        running = next(
            (task.subject for task in tasks if task.state == "in_progress"), ""
        )
        if not self._busy:
            running = ""
        summary = f"Tasks {done}/{len(tasks)}"
        if running:
            summary += f" · running: {running}"
        return {"visible": True, "summary": summary}

    def _queue_delta(self, kind: str, delta: str) -> None:
        """把流式增量攒到 80ms 再发，减少进程间事件数量。"""
        if not delta:
            return
        self._delta_buffer[kind] = self._delta_buffer.get(kind, "") + delta
        if self._delta_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_deltas()
            return
        self._delta_handle = loop.call_later(0.08, self._flush_deltas)

    def _flush_deltas(self) -> None:
        if self._delta_handle is not None:
            self._delta_handle.cancel()
            self._delta_handle = None
        if not self._delta_buffer:
            return
        pending, self._delta_buffer = self._delta_buffer, {}
        for kind, text in pending.items():
            self.emit(kind, {"delta": text})

    async def submit(self, text: str, images: list[str] | None = None) -> int:
        """发起一轮回复，返回 exchange_id。"""
        await self._wait_ready(timeout=30)
        if self.agent is None:
            raise SessionError("not_ready", "session is not ready")
        if self._busy or self._queue_active or self._goal_confirming:
            raise SessionError("busy", "a reply is already running")
        if self._revert is not None:
            self.commit_revert()

        self._progress("")
        paths = [Path(item) for item in (images or [])]
        display = display_text(text, paths)
        history_start = len(self.history)
        self._exchange_seq += 1
        exchange_id = self._exchange_seq
        self.history.append((USER, display))
        self._save_history()
        self.emit(
            "exchange_start",
            {"exchange_id": exchange_id, "text": display},
        )
        self._reply_task = asyncio.create_task(
            self._reply(text, history_start, exchange_id, paths)
        )
        return exchange_id

    async def _wait_ready(self, timeout: float = 180.0) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:  # 等就绪超时后照常继续，后续步骤各自报错
            pass

    async def _rebuild_agent(self) -> None:
        from alex.core.agent import build_agent, build_model
        from alex.core.tools import TrackedLocalBackend

        model = build_model()
        self._model = model
        backend = TrackedLocalBackend(workdir=str(workspace_path(create=True)))
        self._backend = backend
        self.agent, self.toolkit, _ = await build_agent(
            model, backend, self.mcps, gate=self._permission_gate, ask=self._ask_user,
            on_compacting=self._notify_compacting
        )

    async def rebuild_agent(self) -> dict[str, Any]:
        """按最新配置重建 agent（技能/规则开关），不重启 MCP。"""
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        reload_config()
        await self._rebuild_agent()
        await self._update_meta()
        return {"model": CONFIG["model"]["name"]}

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self._phase = "running"
        elif self._phase == "running":
            self._phase = "idle"
        self._emit_status()

    def _set_activity(self, text: str) -> None:
        if text == self._activity:
            return
        self._activity = text
        self._emit_status()

    def _emit_status(self) -> None:
        payload = {
            "busy": self._busy,
            "activity": self._activity,
            "phase": self._phase,
            "stopping": self._stopping,
        }
        if payload == self._last_status:
            return
        self._last_status = payload
        self.emit("status", payload)

    def _refresh_tasks(self) -> None:
        self.emit("tasks", self._tasks_payload())

    def _notice(
        self,
        message: str,
        *,
        title: str | None = None,
        severity: str = "information",
        timeout: float = 4,
    ) -> None:
        self.emit(
            "notice",
            {
                "message": message,
                "title": title or "",
                "severity": severity,
                "timeout": timeout,
            },
        )


