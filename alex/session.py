"""Agent 会话内核：与界面无关，供 TUI 与后台 daemon 共用。

所有对 Agent / Toolkit / MCP / 快照 / 配置写操作都在这里，通过 ``emit``
把渲染所需的状态以事件形式发出去。TUI 只消费事件，不持有 Agent。
"""

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

from .config import (
    CONFIG,
    LOCKED_SKILLS,
    ROOT,
    lock_skill_files,
    project_path,
    reload_config,
    save_config,
    workspace_path,
)
from .images import build_user_message, display_text
from . import reasoning
from .snapshots import Checkpoint, SnapshotError, SnapshotStore, now_label

Emit = Callable[[str, dict[str, Any]], None]

USER = "user"
ASSISTANT = "assistant"

DEFAULT_WELCOME = (
    "Hi. I can read and write files, run commands, and manage tasks. "
    "Give me a task to start."
)

BOOT_STAGES = (
    ("skills", "Skills & rules"),
    ("session", "Session history"),
    ("model", "Model & tools"),
    ("mcp", "MCP servers"),
)

# 只读工具：一轮对话里只调用这些工具时，工作区不会变化，可跳过快照
READONLY_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "Skill",
    "AskUser",
    "TaskCreate",
    "TaskList",
    "TaskGet",
    "TaskUpdate",
}


class SessionError(Exception):
    """可映射为 RPC 错误码的会话异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


class GoalRestatement(BaseModel):
    restatement: str = Field(
        description="A concise clarification of what the user wants achieved. Do not execute it."
    )


GOAL_RESTATEMENT_PROMPT = (
    "你只负责复述用户想完成的目标，不执行任务、不调用工具、不做实现。\n"
    "用中文输出 1 到 3 个简洁要点，明确：目标成果、关键范围、必要约束。\n"
    "不要把它写成第一人称，也不要说‘我理解的任务是’。\n"
    "如果原始需求不完整，保留不确定性，不要擅自补充假设。"
)


class AgentSession:
    """一次会话的完整运行时：模型、工具、MCP、快照与对话状态。"""

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
        self._progress_state: dict[str, str] = {"text": "", "tone": "primary"}
        self._pending_confirm: dict[str, Any] | None = None
        self._pending_future: asyncio.Future | None = None
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

    # ------------------------------------------------------------- 生命周期
    def _prepare_runtime(self):
        import agentscope

        agentscope.setup_logger("WARNING")
        from .agent import build_agent, build_mcps, build_model
        from .permissions import PermissionGate
        from .tools import TrackedLocalBackend

        return (
            build_model(),
            build_agent,
            build_mcps,
            PermissionGate(self._confirm_tool),
            TrackedLocalBackend,
        )

    async def initialize(self) -> None:
        """按启动面板顺序装配：Skills → Session → Model → MCP。"""
        # 1) Skills & rules：纯文件扫描，不依赖模型，最先完成
        self._boot_update("skills", "active", "scanning…")
        # 内置技能（alexcode）标记为只读，避免被改写
        try:
            await asyncio.to_thread(lock_skill_files)
        except Exception:
            pass
        try:
            rules, skills = self._scan_sources()
        except Exception:
            rules = skills = 0
        self._boot_update("skills", "done", f"{skills} skills · {rules} rules")

        # 2) Session history：快速确认有可恢复的会话状态（完整解析在最后装配时做）
        self._boot_update("session", "active", "checking…")
        try:
            restored = await asyncio.to_thread(self._has_saved_state)
        except Exception:
            restored = False
        self._boot_update(
            "session", "done", "restored" if restored else "new session"
        )

        # 3) Model & tools
        self._boot_update("model", "active", "loading model…")
        try:
            model, build_agent_fn, build_mcps_fn, gate, backend_cls = (
                await asyncio.to_thread(self._builder or self._prepare_runtime)
            )
            self._model = model
            self._permission_gate = gate
            backend = backend_cls(workdir=str(workspace_path(create=True)))
            self._backend = backend
        except SystemExit as exc:
            self._boot_update("model", "failed", str(exc))
            self._fail_init(str(exc))
            return
        except Exception as exc:
            self._boot_update("model", "failed", str(exc))
            self._fail_init(f"Init failed: {exc}")
            return
        self._boot_update("model", "done", "ready")

        # 4) MCP servers（含最后的 agent 装配，依赖模型 + MCP）
        self._boot_update("mcp", "active", "starting…")
        mcp_error = None
        try:
            self.mcps = await build_mcps_fn()
        except Exception as exc:
            self.mcps = []
            mcp_error = str(exc)

        self.agent, toolkit, _ = await build_agent_fn(
            model,
            self._backend,
            self.mcps,
            gate=self._permission_gate,
            ask=self._ask_user,
            on_compacting=self._notify_compacting,
        )
        self.toolkit = toolkit
        # 历史在 agent 装配后从恢复的状态里取，和旧行为保持一致
        if restored:
            self.history = self._history_from_state(self.agent.state)
            if not self.history:
                self.history.append(
                    (
                        ASSISTANT,
                        "The previous session was restored, but it had no messages to show.",
                    )
                )
        else:
            self.history.append((ASSISTANT, DEFAULT_WELCOME))
            self._announce_sources()
        self._boot_update("session", "done", f"{len(self.history)} messages")

        if mcp_error is not None:
            self._boot_update("mcp", "failed", mcp_error)
        else:
            expected = self._expected_mcp_servers()
            detail = f"{len(self.mcps)} server(s)"
            status = "done" if len(self.mcps) >= expected else "warn"
            self._boot_update("mcp", status, detail)

        self._refresh_tasks()
        self._init_snapshots()
        self._phase = "idle"
        self._ready.set()
        self.emit("history_reset", {"history": self._history_payload()})
        self.emit(
            "checkpoints", {"checkpoints": [asdict(item) for item in self._checkpoints]}
        )
        await self._update_meta()
        # 进程被关闭时未回答的问题在重启后恢复（需排在历史重建之后）
        self._restore_pending_ask()
        self._emit_status()

    def _fail_init(self, message: str) -> None:
        self._phase = "failed"
        self._ready.set()
        self._notice(message, title="Init failed", severity="error", timeout=10)
        self._emit_status()

    @staticmethod
    def _expected_mcp_servers() -> int:
        try:
            data = json.loads(
                project_path("mcp_config").read_text(encoding="utf-8")
            )
            return len(data.get("mcpServers", {}))
        except (OSError, ValueError):
            return 0

    def _boot_update(self, stage: str, status: str, detail: str = "") -> None:
        entry = self._boot.get(stage)
        if entry is None:
            entry = {"label": stage, "status": "idle", "detail": ""}
            self._boot[stage] = entry
        entry["status"] = status
        entry["detail"] = detail
        self.emit(
            "boot",
            {
                "stage": stage,
                "label": entry["label"],
                "status": status,
                "detail": detail,
            },
        )

    def _init_snapshots(self) -> None:
        self._last_tree = None
        try:
            self._snapshots = SnapshotStore(workspace_path(create=True))
            self._checkpoints = list(self._snapshots.checkpoints)
            self._exchange_seq = max(
                (item.exchange_id for item in self._checkpoints), default=0
            )
            if not self._snapshots.available and not self._snapshot_warned:
                self._snapshot_warned = True
                self._notice(
                    "Checkpoints disabled: git was not found",
                    severity="warning",
                    timeout=6,
                )
        except Exception as exc:
            self._snapshots = None
            self._checkpoints = []
            if not self._snapshot_warned:
                self._snapshot_warned = True
                self._notice(
                    f"Checkpoints disabled: {exc}", severity="warning", timeout=6
                )

    @staticmethod
    def _scan_sources() -> tuple[int, int]:
        """只统计 rules / skills 数量，不读写对话历史。"""
        rules_dir = project_path("rules_dir")
        skills_dir = project_path("skills_dir")
        rules = sum(1 for _ in rules_dir.glob("*.md"))
        skills = sum(1 for _ in skills_dir.glob("*/SKILL.md"))
        return rules, skills

    @staticmethod
    def _model_supports_vision() -> bool:
        """当前模型是否支持图片输入（config 里 model.vision）。"""
        return bool(CONFIG.get("model", {}).get("vision", False))

    @staticmethod
    def _has_saved_state() -> bool:
        """快速判断是否有可恢复的会话状态（不做完整反序列化）。"""
        path = project_path("session_file")
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(data.get("context"), list)

    def _announce_sources(self) -> tuple[int, int]:
        rules_dir = project_path("rules_dir")
        skills_dir = project_path("skills_dir")
        rules = sorted(path.stem for path in rules_dir.glob("*.md"))
        skills = sorted(path.parent.name for path in skills_dir.glob("*/SKILL.md"))
        if rules:
            self.history.insert(0, ("rule", "Rules: " + ", ".join(rules)))
        if skills:
            self.history.insert(
                1 if rules else 0, ("skill", "Skills: " + ", ".join(skills))
            )
        return len(rules), len(skills)

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

    async def close(self) -> None:
        """保存会话并释放子进程；忙碌时不落盘。"""
        if self._closed:
            return
        self._closed = True
        if self._queue_task is not None and not self._queue_task.done():
            self._queue_stop = True
            if self._queue_decision is not None and not self._queue_decision.done():
                self._queue_decision.set_result("cancel")
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._reply_task is not None and not self._reply_task.done():
            self._reply_task.cancel()
            try:
                await self._reply_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self._backend is not None:
            try:
                await self._backend.kill_all()
            except Exception:
                pass
        if self.agent is not None and not self._busy:
            from .agent import save_state

            try:
                save_state(self.agent.state)
            except Exception:
                pass
        for client in self.mcps:
            try:
                if client.is_stateful and client.is_connected:
                    await client.close()
            except Exception:
                pass

    # --------------------------------------------------------------- 快照视图
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

    # --------------------------------------------------------------- 对话主体
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
        self.emit(
            "exchange_start",
            {"exchange_id": exchange_id, "text": display},
        )
        self._reply_task = asyncio.create_task(
            self._reply(text, history_start, exchange_id, paths)
        )
        return exchange_id

    async def goal(self, text: str) -> dict[str, Any]:
        """先复述并等待确认；确认后才启动执行者与验收者循环。"""
        await self._wait_ready(timeout=30)
        if self.agent is None or self._model is None:
            raise SessionError("not_ready", "session is not ready")
        if self._busy or self._queue_active or self._goal_confirming:
            raise SessionError("busy", "a reply is already running")
        if not text.strip():
            raise SessionError("internal", "empty goal")
        if self._revert is not None:
            self.commit_revert()

        max_iters = int(CONFIG["model"].get("goal_max_iters", 3))
        self._goal_state = state = {
            "goal": text,
            "max_iters": max_iters,
            "round": 1,
            "phase": "awaiting_confirmation",
            "rows": [],
            "started": 0.0,
            "seconds": 0.0,
            "execute_started": 0.0,
            "verify_started": 0.0,
            "last_result": "",
            "failed_rounds": 0,
        }
        state["restatement"] = await self._restate_goal(text)
        display = f"/goal {text}"
        history_start = len(self.history)
        self._exchange_seq += 1
        exchange_id = self._exchange_seq
        self._goal_state["history_start"] = history_start
        self._goal_state["exchange_id"] = exchange_id
        self._goal_state["display"] = display
        self.history.append((USER, display))
        self.emit(
            "exchange_start",
            {"exchange_id": exchange_id, "text": display},
        )
        self._goal_card_index = len(self.history)
        self.history.append(("goal_card", self._goal_card_json()))
        self.emit("goal_card", self._goal_card_view())
        self._goal_confirming = True
        self._phase = "confirming_goal"
        self._progress("Goal · awaiting confirmation", "warn")
        self._emit_status()
        return {"awaiting_confirmation": True}

    async def goal_confirm(self) -> dict[str, Any]:
        """确认目标并开始执行。"""
        state = self._goal_state
        if not self._goal_confirming or state is None:
            raise SessionError("no_goal", "no goal is waiting for confirmation")
        from agentscope.message import UserMsg
        from agentscope.pipeline import GoalPipeline

        from .agent import build_verifier

        state["phase"] = "executing"
        state["rows"] = [
            {
                "round": 1,
                "phase": "execute",
                "status": "run",
                "text": "",
                "seconds": 0.0,
            }
        ]
        state["started"] = time.monotonic()
        state["execute_started"] = state["started"]
        self._goal_confirming = False
        pipeline = GoalPipeline(
            executor=self.agent,
            verifier=build_verifier(
                self._model, self._backend, on_result=self._on_goal_verdict
            ),
            verifier_reset_context=True,
            max_iters=state["max_iters"],
        )
        self._update_goal_card()
        self._progress(f"Goal Round 1/{state['max_iters']} · executing")
        self._busy = True
        self._phase = "running"
        self._emit_status()
        self._reply_task = asyncio.create_task(
            self._reply(
                state["goal"],
                state["history_start"],
                state["exchange_id"],
                [],
                label=state["display"],
                events=self._goal_events(
                    pipeline.reply_stream(
                        UserMsg(name="user", content=state["goal"])
                    )
                ),
            )
        )
        return {"started": True}

    async def goal_update(self, text: str) -> dict[str, Any]:
        """修改尚未确认的目标需求。"""
        state = self._goal_state
        if not self._goal_confirming or state is None:
            raise SessionError("no_goal", "no goal is waiting for confirmation")
        if not text.strip():
            raise SessionError("internal", "empty goal")
        state["goal"] = text.strip()
        state["restatement"] = await self._restate_goal(text)
        self._update_goal_card()
        return {"updated": True}

    async def goal_cancel(self) -> dict[str, Any]:
        """取消尚未确认的目标，不调用任何工具。"""
        if not self._goal_confirming or self._goal_state is None:
            raise SessionError("no_goal", "no goal is waiting for confirmation")
        self._goal_state["phase"] = "cancelled"
        self._goal_confirming = False
        self._update_goal_card()
        self._goal_state = None
        self._goal_card_index = None
        self._progress("")
        self._phase = "idle"
        self._emit_status()
        return {"cancelled": True}

    # ------------------------------------------------------------ 目标进度
    def _progress(self, text: str, tone: str = "primary") -> None:
        """输入框左上角的一行进度：目标或队列共用。"""
        payload = {"text": text, "tone": tone if text else "primary"}
        if payload == self._progress_state:
            return
        self._progress_state = payload
        self.emit("progress", payload)

    def _goal_card_view(self) -> dict[str, Any]:
        state = self._goal_state or {}
        rows = [
            {
                "round": row["round"],
                "phase": row["phase"],
                "status": row["status"],
                "text": row["text"],
                "seconds": int(row["seconds"]),
            }
            for row in state.get("rows", [])
        ]
        return {
            "goal": state.get("goal", ""),
            "restatement": state.get("restatement", ""),
            "max_iters": state.get("max_iters", 1),
            "round": state.get("round", 1),
            "phase": state.get("phase", "executing"),
            "seconds": int(state.get("seconds", 0)),
            "failed_rounds": state.get("failed_rounds", 0),
            "rows": rows,
        }

    def _goal_card_json(self) -> str:
        return json.dumps(self._goal_card_view(), ensure_ascii=False)

    def _update_goal_card(self) -> None:
        if self._goal_state is None or self._goal_card_index is None:
            return
        if self._goal_card_index < len(self.history):
            self.history[self._goal_card_index] = (
                "goal_card",
                self._goal_card_json(),
            )
        self.emit("goal_card", self._goal_card_view())

    @staticmethod
    def _goal_row(state: dict, phase: str) -> dict | None:
        for row in reversed(state["rows"]):
            if row["phase"] == phase and row["status"] == "run":
                return row
        return None

    def _on_goal_report(self, report: str) -> None:
        """执行者交出报告：记下本轮结果，进入验收。"""
        state = self._goal_state
        if state is None:
            return
        row = self._goal_row(state, "execute")
        if row is not None:
            row["status"] = "ok"
            row["text"] = self._one_line(report, 70) or "report ready"
            row["seconds"] = time.monotonic() - state["execute_started"]
        state["phase"] = "verifying"
        state["verify_started"] = time.monotonic()
        state["rows"].append(
            {
                "round": state["round"],
                "phase": "verify",
                "status": "run",
                "text": "checking the workspace…",
                "seconds": 0.0,
            }
        )
        self.history.append(("verifier", ""))
        self._update_goal_card()
        self._progress(
            f"Goal Round {state['round']}/{state['max_iters']} · verifying"
        )
        self.emit("goal_verify_start", {"round": state["round"]})

    def _on_goal_verdict(self, result: dict) -> None:
        """验收者的最终判定（框架内部消费，这里同步到卡片与进度）。"""
        state = self._goal_state
        if state is None:
            return
        verdict = str(result.get("result") or "fail")
        message = str(result.get("message") or "")
        row = self._goal_row(state, "verify")
        if row is not None:
            row["status"] = "ok" if verdict == "pass" else "bad"
            detail = self._one_line(message, 80)
            row["text"] = (
                f"{verdict} — {detail}" if detail and verdict != "pass"
                else verdict
            )
            row["seconds"] = time.monotonic() - state["verify_started"]
        state["last_result"] = verdict
        self.emit(
            "goal_verdict",
            {
                "round": state["round"],
                "result": verdict,
                "message": message,
            },
        )
        if verdict == "fail" and state["round"] < state["max_iters"]:
            state["failed_rounds"] += 1
            state["round"] += 1
            state["phase"] = "executing"
            state["execute_started"] = time.monotonic()
            state["rows"].append(
                {
                    "round": state["round"],
                    "phase": "execute",
                    "status": "run",
                    "text": "applying the verifier's feedback…",
                    "seconds": 0.0,
                }
            )
            self._update_goal_card()
            self._progress(
                f"Goal Round {state['round']}/{state['max_iters']} · executing",
                "warn",
            )
            self.emit("goal_round", {"round": state["round"]})
            return
        self._update_goal_card()
        if verdict == "pass":
            self._progress(
                f"Goal passed · Round {state['round']}/{state['max_iters']}", "ok"
            )
        elif verdict == "impossible":
            self._progress(
                f"Goal stopped · Round {state['round']}/{state['max_iters']}", "bad"
            )

    async def _goal_events(self, source):
        """包装 GoalPipeline 事件流：补上轮次、报告与结束状态。"""
        from agentscope.event import TextBlockDeltaEvent
        from agentscope.message import AssistantMsg, Msg

        executor_text: list[str] = []
        executor_round = 0
        try:
            async for event in source:
                state = self._goal_state
                if (
                    isinstance(event, TextBlockDeltaEvent)
                    and state is not None
                    and state.get("phase") == "executing"
                ):
                    round_no = int(state.get("round", 1))
                    if round_no != executor_round:
                        executor_text.clear()
                        executor_round = round_no
                    executor_text.append(event.delta)
                    # 执行者的回答先暂存，避免看起来像最终答复；
                    # 等验收工具与结果都显示完后再输出。
                    continue
                if isinstance(event, Msg) and event.structured_output is not None:
                    report = event.structured_output.get("report")
                    if report is not None:
                        self._on_goal_report(str(report))
                        # 结构化占位消息不展示，最终回答沿用执行者的正文
                        continue
                yield event
        except asyncio.CancelledError:
            self._finish_goal("stopped")
            raise
        except Exception:
            self._finish_goal("failed")
            raise
        else:
            state = self._goal_state or {}
            result = state.get("last_result")
            if result == "pass":
                status = "passed"
            elif result == "impossible":
                status = "impossible"
            elif result == "fail":
                status = "failed"
            else:
                status = "stopped"
            self._finish_goal(status)
            if executor_text:
                yield AssistantMsg(name="Alex", content="".join(executor_text))

    def _finish_goal(self, status: str) -> None:
        state = self._goal_state
        if state is None:
            return
        state["phase"] = status
        state["seconds"] = time.monotonic() - state["started"]
        for row in reversed(state["rows"]):
            if row["status"] == "run":
                row["status"] = "bad" if status != "passed" else "ok"
                if not row["text"]:
                    row["text"] = "stopped" if status != "passed" else "done"
                break
        if status == "passed":
            self._progress(
                f"Goal passed · Round {state['round']}/{state['max_iters']}", "ok"
            )
        elif status == "failed":
            self._progress(
                f"Goal failed · Round {state['round']}/{state['max_iters']}", "bad"
            )
        else:
            self._progress(
                f"Goal stopped · Round {state['round']}/{state['max_iters']}", "bad"
            )
        self._update_goal_card()
        self.emit(
            "goal_end",
            {
                "status": status,
                "round": state["round"],
                "max_iters": state["max_iters"],
                "seconds": int(state["seconds"]),
            },
        )
        self._goal_state = None
        self._goal_card_index = None

    # ------------------------------------------------------------ 任务队列
    async def queue(self, text: str) -> dict[str, Any]:
        """拆成互不依赖的任务并串行执行；立即返回，进度用事件推送。"""
        await self._wait_ready(timeout=30)
        if self.agent is None:
            raise SessionError("not_ready", "session is not ready")
        if self._busy or self._queue_active or self._goal_confirming:
            raise SessionError("busy", "a reply is already running")
        if not text.strip():
            raise SessionError("internal", "empty queue")
        if self._revert is not None:
            self.commit_revert()

        before = {task.id for task in self.agent.state.tasks_context.tasks}
        self._queue_before_ids = before
        self._queue_active = True
        self._queue_stop = False
        self._queue_index = 0
        self._queue_total = 0
        self._queue_confirming = False
        self._queue_task_ids = []
        self._queue_subjects = []
        self._queue_card_index = None
        self._refresh_tasks()  # 规划期间先收起旧的置顶任务栏
        self._queue_task = asyncio.create_task(self._run_queue(text, before))
        return {"started": True}

    async def _run_queue(self, text: str, before: set[str]) -> None:
        results: list[tuple[str, str, str]] = []
        try:
            plan = (
                "Plan a queue of small, independent tasks for the request "
                "below.\n\n"
                "Rules:\n"
                "- If any task needs another task's output or completion "
                "first, do NOT call TaskCreate. Explain the dependency and "
                "ask the user to run the tasks one at a time.\n"
                "- Otherwise call TaskCreate once per task, in execution "
                "order.\n"
                "- Do not start any task now.\n\n"
                f"Request:\n{text}"
            )
            try:
                await self._start_turn(plan, f"/queue {text}")
            except asyncio.CancelledError:
                self._queue_stop = True
                return
            if self._queue_stop:
                return
            new_tasks = [
                task
                for task in self.agent.state.tasks_context.tasks
                if task.id not in before
            ]
            if not new_tasks:
                self._notice(
                    "Queue not started — the tasks depend on each other. "
                    "Run them one at a time.",
                    title="Queue",
                    severity="warning",
                    timeout=8,
                )
                return

            total = len(new_tasks)
            self._queue_total = total
            self._queue_subjects = [task.subject for task in new_tasks]
            self._queue_task_ids = [task.id for task in new_tasks]
            self._queue_confirming = True
            payload = self._queue_card_payload("confirming")
            self.history.append(
                ("queue_card", json.dumps(payload, ensure_ascii=False))
            )
            self._queue_card_index = len(self.history) - 1
            self.emit("queue_card", payload)
            self._progress(f"Queue 0/{total} · awaiting start", "warn")
            self._phase = "confirming"
            self._emit_status()
            self._refresh_tasks()

            decision = await self._wait_queue_decision()
            if decision != "start" or self._queue_stop:
                self._update_queue_card("cancelled")
                self._progress(f"Queue 0/{total} · not started", "muted")
                # 丢弃后立即落盘，避免 daemon 重启后任务又出现在列表里
                await self._drop_queue_tasks()
                return

            self._queue_confirming = False
            self._update_queue_card("started")
            self._phase = "running"
            self._emit_status()
            started = time.monotonic()

            for index, task in enumerate(new_tasks, 1):
                self._queue_index = index
                done_now = sum(1 for _, state, _ in results if state == "done")
                self._progress(
                    f"Queue {index}/{total} · {done_now}/{total} done"
                )
                if self._queue_stop:
                    results.append((task.subject, "skipped", ""))
                    continue
                if task.state == "completed":
                    results.append((task.subject, "done", ""))
                    continue

                header = f"Queue {index}/{total} · {task.subject}"
                self.history.append(("queue", header))
                self.emit(
                    "queue",
                    {"index": index, "total": total, "subject": task.subject},
                )
                self._refresh_tasks()
                prompt = (
                    "Work only on this queued task. Do not touch other "
                    "queued tasks.\n\n"
                    f"Task:\n{task.subject}\n{task.description or ''}\n\n"
                    "When it is fully done, mark it completed with "
                    f'TaskUpdate(task_id="{task.id}", status="completed"). '
                    "If it cannot be completed, leave the status unchanged "
                    "and reply with one short sentence explaining why."
                )
                try:
                    answer = await self._start_turn(
                        prompt, header, label=task.subject, bubble=False
                    )
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise  # 会话关闭导致整个队列被取消
                    self._queue_stop = True
                    answer = ""

                if task.state == "completed":
                    results.append((task.subject, "done", ""))
                elif self._queue_stop:
                    if task.state == "in_progress":
                        task.state = "pending"
                    results.append((task.subject, "skipped", ""))
                else:
                    results.append(
                        (task.subject, "failed", self._one_line(answer))
                    )
                    # 失败的任务退回 pending，避免下次空闲时误显示为运行中
                    if task.state == "in_progress":
                        task.state = "pending"
                    self._refresh_tasks()

            done = sum(1 for _, status, _ in results if status == "done")
            failed = sum(1 for _, status, _ in results if status == "failed")
            skipped = sum(1 for _, status, _ in results if status == "skipped")
            payload = {
                "results": [list(item) for item in results],
                "done": done,
                "failed": failed,
                "skipped": skipped,
                "seconds": max(0, int(time.monotonic() - started)),
            }
            self.history.append(
                ("queue_result", json.dumps(payload, ensure_ascii=False))
            )
            self.emit("queue_result", payload)
            self._update_queue_card("done")
            self._progress("")
            # 队列结束即清理本次任务：结果卡已记录成败，旧任务不再堆到下一次队列
            await self._drop_queue_tasks()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._notice(
                f"Queue failed: {exc}",
                title="Queue",
                severity="error",
                timeout=8,
            )
            self._progress("")
            await self._drop_queue_tasks()
        finally:
            # 会话关闭等外部取消路径也要清理，落盘交给 close()
            self._discard_queue_tasks()
            self._queue_active = False
            self._queue_stop = False
            self._queue_task = None
            self._queue_index = 0
            self._queue_total = 0
            self._queue_confirming = False
            self._queue_decision = None
            self._queue_task_ids = []
            self._queue_subjects = []
            self._queue_card_index = None
            self._queue_before_ids = set()
            self._phase = "running"
            self._set_busy(False)
            self._refresh_tasks()

    # ------------------------------------------------------- 队列确认 / 取消
    async def queue_confirm(self) -> dict[str, Any]:
        """用户确认开始：释放队列任务，继续执行。"""
        future = self._queue_decision
        if not self._queue_confirming or future is None or future.done():
            raise SessionError("no_queue", "no queue is waiting for confirmation")
        self._queue_confirming = False
        future.set_result("start")
        return {"started": True}

    async def queue_cancel(self) -> dict[str, Any]:
        """用户二次 Esc：丢弃本次拆分出来的任务。"""
        future = self._queue_decision
        if not self._queue_confirming or future is None or future.done():
            raise SessionError("no_queue", "no queue is waiting for confirmation")
        self._queue_confirming = False
        future.set_result("cancel")
        return {"cancelled": True}

    def _queue_card_payload(self, state: str) -> dict[str, Any]:
        return {"tasks": list(self._queue_subjects), "state": state}

    def _update_queue_card(self, state: str) -> None:
        """改写历史里的计划卡并推送增量，重连时也能看到最终状态。"""
        if self._queue_card_index is None:
            return
        if self._queue_card_index >= len(self.history):
            return
        payload = self._queue_card_payload(state)
        self.history[self._queue_card_index] = (
            "queue_card",
            json.dumps(payload, ensure_ascii=False),
        )
        self.emit("queue_card", payload)

    async def _wait_queue_decision(self) -> str:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue_decision = future
        try:
            return str(await future)
        except asyncio.CancelledError:
            self._queue_stop = True
            return "cancel"
        finally:
            if self._queue_decision is future:
                self._queue_decision = None

    def _discard_queue_tasks(self) -> bool:
        """移除本次队列新建的任务，并清理依赖引用。"""
        if self.agent is None:
            return False
        context = self.agent.state.tasks_context
        ids = set(self._queue_task_ids)
        if not ids and self._queue_before_ids:
            # 规划途中被取消时 _queue_task_ids 还没写入，按开始前的快照兜底
            ids = {
                task.id
                for task in context.tasks
                if task.id not in self._queue_before_ids
            }
        if not ids:
            return False
        remaining = [task for task in context.tasks if task.id not in ids]
        removed = len(remaining) != len(context.tasks)
        context.tasks = remaining
        for task in context.tasks:
            task.blocks = [item for item in task.blocks if item not in ids]
            task.blocked_by = [
                item for item in task.blocked_by if item not in ids
            ]
        return removed

    async def _drop_queue_tasks(self) -> None:
        """队列结束后清理本次任务并落盘，避免旧任务堆到下一次队列。"""
        if not self._discard_queue_tasks():
            return
        from .agent import save_state

        try:
            await asyncio.to_thread(save_state, self.agent.state)
        except Exception:
            pass

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
                    if name == "Skill":
                        skill = self._skill_arg(event.tool_call_id)
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
            from .agent import save_state

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
                        label or content,
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

    # --------------------------------------------------------------- 权限确认
    async def _confirm_tool(self, agent_name, tool, tool_input, decision):
        from .permissions import ConfirmChoice, permission_detail

        # 并行子代理可能同时请求确认：排队逐个处理，避免互相覆盖
        async with self._confirm_lock:
            if self._stopping:
                return ConfirmChoice.DENY
            try:
                from .diffs import tool_diff

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
        from .permissions import ConfirmChoice

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

    # --------------------------------------------------------------- 选择题提问
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
        from .tools import AskUser

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

    # ----------------------------------------------------------- 上下文与元信息
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
        except Exception:
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
            from .agent import save_state

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
                self.emit("compaction", {"summary": summary, "detail": detail})
                return {"compacted": True}
            raise SessionError(
                "not_ready", "Context is below the compaction threshold; nothing to do"
            )
        finally:
            self._set_activity("")
            self.emit("compacting", {"active": False})
            self._set_busy(False)

    # --------------------------------------------------------------- 会话操作
    async def clear_session(self) -> None:
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        from .agent import build_agent, build_model
        from .tools import TrackedLocalBackend

        path = project_path("session_file")
        if path.is_file():
            path.unlink()
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
        self.emit("history_reset", {"history": self._history_payload()})
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
        from .agent import save_state

        save_state(self.agent.state)

        self.history = self._history_from_state(self.agent.state)
        self._revert["index"] = index
        self._revert["prompt"] = checkpoint.prompt
        await self._rebuild_agent()
        self.emit("history_reset", {"history": self._history_payload()})
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
        from .agent import save_state

        save_state(self.agent.state)
        self.history = self._history_from_state(self.agent.state)
        self._revert = None
        await self._rebuild_agent()
        self.emit("history_reset", {"history": self._history_payload()})
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

    async def _rebuild_agent(self) -> None:
        from .agent import build_agent, build_model
        from .tools import TrackedLocalBackend

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

    async def reload(self) -> dict[str, Any]:
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        from .agent import build_agent, build_mcps, build_model
        from .tools import TrackedLocalBackend

        reload_config()
        for client in self.mcps:
            if client.is_stateful and client.is_connected:
                await client.close()
        model = build_model()
        self._model = model
        backend = TrackedLocalBackend(workdir=str(workspace_path(create=True)))
        self._backend = backend
        self.mcps = await build_mcps()
        self.agent, self.toolkit, _ = await build_agent(
            model, backend, self.mcps, gate=self._permission_gate, ask=self._ask_user,
            on_compacting=self._notify_compacting
        )
        await self._update_meta()
        return {"mcp_servers": len(self.mcps), "model": CONFIG["model"]["name"]}

    async def change_workspace(self, raw: str) -> dict[str, Any]:
        if self._revert is not None:
            raise SessionError(
                "not_ready", "Commit the revert before changing workspace"
            )
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        from .agent import build_agent, build_model, save_state
        from .tools import TrackedLocalBackend

        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = workspace_path() / target
        old = CONFIG.get("workdir")
        try:
            target = target.resolve()
            target.mkdir(parents=True, exist_ok=True)
            if not target.is_dir():
                raise NotADirectoryError(target)
        except OSError as exc:
            raise SessionError("internal", f"Cannot use workspace: {exc}") from exc
        CONFIG["workdir"] = str(target)
        save_config()
        try:
            save_state(self.agent.state)
            model = build_model()
            self._model = model
            backend = TrackedLocalBackend(workdir=str(target))
            self._backend = backend
            self.agent, self.toolkit, _ = await build_agent(
                model, backend, self.mcps, gate=self._permission_gate, ask=self._ask_user,
            on_compacting=self._notify_compacting
            )
            store = SnapshotStore(target)
            self._snapshots = store
            self._last_tree = None
            self._checkpoints = list(store.checkpoints)
            self.emit("checkpoints", {"checkpoints": [asdict(i) for i in self._checkpoints]})
            await self._update_meta()
            return {"workspace": str(target)}
        except Exception as exc:
            CONFIG["workdir"] = old
            save_config()
            raise SessionError("internal", f"Workspace switch failed: {exc}") from exc

    async def clear_memory(self) -> None:
        from .memory import clear_memory, find_memory_middleware

        try:
            middleware = find_memory_middleware(self.agent)
            if middleware is not None:
                await middleware.clear()
            else:
                await asyncio.to_thread(clear_memory, str(ROOT), CONFIG["memory_dir"])
        except Exception as exc:
            raise SessionError("internal", f"Clear memory failed: {exc}") from exc

    async def set_permission_mode(self, mode: str) -> None:
        from .permissions import resolve_permission_mode

        CONFIG["permission_mode"] = mode
        save_config()
        if self.agent is not None:
            self.agent.state.permission_context.mode = resolve_permission_mode(mode)
        await self._update_meta()

    async def model_catalog(self) -> list[dict[str, Any]]:
        """给 /model 弹窗使用的模型与思考档位。"""
        current_name = CONFIG["model"]["name"]
        saved = CONFIG.get("reasoning_choices", {}) or {}
        items: list[dict[str, Any]] = []
        for profile in CONFIG.get("model_profiles", []):
            name = str(profile.get("name", ""))
            if not name:
                continue
            is_current = name == current_name
            cfg = CONFIG["model"] if is_current else profile
            preferred = None if is_current else saved.get(name)
            items.append(
                {
                    "name": name,
                    "current": is_current,
                    "levels": list(reasoning.levels_of(cfg)),
                    "level": reasoning.resolve_level(cfg, preferred),
                }
            )
        return items

    async def switch_model(
        self, name: str, reasoning_level: str | None = None
    ) -> dict[str, Any]:
        profiles = {p["name"]: p for p in CONFIG.get("model_profiles", [])}
        if name not in profiles:
            raise SessionError(
                "not_ready",
                f"Unknown model {name}; available: {', '.join(profiles) or 'none'}",
            )
        # 深拷贝，避免把 current 写回 model_profiles
        profile = json.loads(json.dumps(profiles[name]))
        profile.setdefault("vision", False)
        levels = reasoning.levels_of(profile)
        if reasoning_level is not None and reasoning_level not in levels:
            raise SessionError(
                "not_ready",
                f"Unknown reasoning level {reasoning_level} for {name}; "
                f"available: {', '.join(levels) or 'none'}",
            )
        saved = CONFIG.get("reasoning_choices", {}) or {}
        level = reasoning.resolve_level(
            profile, reasoning_level or saved.get(name)
        )
        if name == CONFIG["model"]["name"]:
            current = reasoning.current_level(CONFIG["model"])
            if reasoning_level is None or reasoning_level == current:
                return {"model": name, "changed": False, "reasoning": current}
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        from .agent import build_agent, build_model
        from .tools import TrackedLocalBackend

        CONFIG["model"].pop("reasoning", None)
        CONFIG["model"].update(profile)
        if level is not None:
            setting = profile.get("reasoning")
            setting = dict(setting) if isinstance(setting, dict) else {}
            setting["current"] = level
            CONFIG["model"]["reasoning"] = setting
            CONFIG.setdefault("reasoning_choices", {})[name] = level
        save_config()
        model = build_model()
        backend = TrackedLocalBackend(workdir=str(workspace_path(create=True)))
        self._backend = backend
        self.agent, self.toolkit, _ = await build_agent(
            model, backend, self.mcps, gate=self._permission_gate, ask=self._ask_user,
            on_compacting=self._notify_compacting
        )
        await self._update_meta()
        return {"model": name, "changed": True, "reasoning": level}

    def set_visibility(self, kind: str, item: str, visible: bool) -> None:
        if kind == "skill":
            # 内置只读技能始终启用，拒绝任何关闭请求
            if item in LOCKED_SKILLS:
                return
            excluded = set(CONFIG.get("excluded_skills", []))
            if visible:
                excluded.discard(item)
            else:
                excluded.add(item)
            CONFIG["excluded_skills"] = sorted(excluded)
            save_config()
        elif kind == "rule":
            excluded = set(CONFIG.get("excluded_rules", []))
            if visible:
                excluded.discard(item)
            else:
                excluded.add(item)
            CONFIG["excluded_rules"] = sorted(excluded)
            save_config()
        else:
            path = project_path("mcp_config")
            data = json.loads(path.read_text(encoding="utf-8"))
            if kind == "server":
                server_cfg = data.get("mcpServers", {}).get(item, {})
                tools = self._mcp_tools_by_server.get(item, [])
                server_cfg["disable_tools"] = [] if visible else sorted(tools)
            else:
                server, tool = item.split("/", 1)
                server_cfg = data.get("mcpServers", {}).get(server, {})
                disabled = set(server_cfg.get("disable_tools", []))
                if visible:
                    disabled.discard(tool)
                else:
                    disabled.add(tool)
                server_cfg["disable_tools"] = sorted(disabled)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    async def get_diff(self, exchange_id: int) -> dict[str, Any]:
        if self._snapshots is None:
            raise SessionError("not_ready", "snapshots are not available")
        checkpoint = next(
            (item for item in self._checkpoints if item.exchange_id == exchange_id),
            None,
        )
        if checkpoint is None:
            raise SessionError("not_ready", "no checkpoint for this exchange")
        try:
            text = await self._snapshots.diff_text(
                checkpoint.before, checkpoint.after
            )
        except Exception as exc:
            raise SessionError("internal", f"Cannot load diff: {exc}") from exc
        index = self._checkpoints.index(checkpoint)
        return {
            "text": text,
            "index": index,
            "prompt": checkpoint.prompt,
            "files": len(checkpoint.files),
            "additions": checkpoint.additions,
            "deletions": checkpoint.deletions,
        }

    async def _wait_ready(self, timeout: float = 180.0) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def describe(self) -> dict[str, Any]:
        """设置页所需的技能 / 规则 / MCP / token 信息。"""
        await self._wait_ready()
        import frontmatter

        skills_dir = project_path("skills_dir")
        excluded_skills = set(CONFIG.get("excluded_skills", []))
        skills = []
        skill_details: dict[str, str] = {}
        if skills_dir.is_dir():
            for path in sorted(skills_dir.rglob("SKILL.md")):
                try:
                    metadata = frontmatter.loads(
                        path.read_text(encoding="utf-8-sig")
                    )
                    name = str(metadata.get("name") or path.parent.name)
                    description = " ".join(
                        str(metadata.get("description") or "").split()
                    )
                    if len(description) > 150:
                        description = (
                            description[:147].rsplit(" ", 1)[0] + "…"
                        )
                    skills.append(
                        (
                            name,
                            description,
                            name in LOCKED_SKILLS or name not in excluded_skills,
                        )
                    )
                    skill_details[name] = str(metadata.content or "")
                except (OSError, ValueError):
                    continue

        rules_dir = project_path("rules_dir")
        excluded_rules = set(CONFIG.get("excluded_rules", []))
        rules = []
        rule_details: dict[str, str] = {}
        if rules_dir.is_dir():
            for path in sorted(rules_dir.glob("*.md")):
                description = ""
                text = ""
                try:
                    text = path.read_text(encoding="utf-8-sig")
                    for line in text.splitlines():
                        stripped = line.strip()
                        if not stripped:
                            continue
                        description = stripped.lstrip("# ").strip()
                        break
                except OSError:
                    pass
                if len(description) > 150:
                    description = description[:147].rsplit(" ", 1)[0] + "…"
                rules.append(
                    (path.name, description, path.name not in excluded_rules)
                )
                rule_details[path.name] = text

        mcp_path = project_path("mcp_config")
        server_config: dict = {}
        if mcp_path.is_file():
            try:
                server_config = json.loads(
                    mcp_path.read_text(encoding="utf-8")
                ).get("mcpServers", {})
            except (OSError, ValueError):
                server_config = {}
        connected = {
            str(getattr(client, "name", ""))
            for client in self.mcps
            if client.is_connected
        }
        mcp_servers = [
            (name, str(cfg.get("type", "stdio")), name in connected)
            for name, cfg in server_config.items()
        ]
        mcp_tools = []
        tools_by_server: dict[str, list[str]] = {}

        async def _discover(client):
            disabled = set(
                server_config.get(client.name, {}).get("disable_tools", [])
            )
            items = []
            try:
                for tool in await client._discover_tools():
                    description = " ".join(str(tool.description or "").split())
                    items.append(
                        (client.name, tool.name, description, tool.name not in disabled)
                    )
                    tools_by_server.setdefault(client.name, []).append(tool.name)
            except Exception:
                pass
            return items

        results = await asyncio.gather(
            *(_discover(client) for client in self.mcps),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, list):
                mcp_tools.extend(result)
        self._mcp_tools_by_server = tools_by_server

        return {
            "skills": skills,
            "skill_details": skill_details,
            "rules": rules,
            "rule_details": rule_details,
            "mcp_servers": mcp_servers,
            "mcp_tools": mcp_tools,
            "sections": self._settings_sections(),
            "tokens": await self._token_snapshot(),
            "memory": self._memory_page(),
        }

    @staticmethod
    def _parse_memory_entries(text: str) -> list[dict[str, Any]]:
        """按 ``## 标题`` 切分 MEMORY.md，提取标题、日期、摘要与全文。"""
        blocks: list[tuple[str, list[str]]] = []
        heading: str | None = None
        body: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if heading is not None:
                    blocks.append((heading, body))
                heading = line[3:].strip()
                body = []
            elif heading is not None:
                body.append(line)
        if heading is not None:
            blocks.append((heading, body))

        entries: list[dict[str, Any]] = []
        for heading, lines in blocks:
            content = "\n".join(lines).strip()
            match = re.search(r"(\d{4}-\d{2}-\d{2})", heading)
            title = re.sub(
                r"[（(][^）)]*\d{4}-\d{2}-\d{2}[^）)]*[)）]\s*$", "", heading
            ).strip() or heading
            snippet = ""
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("|"):
                    continue
                candidate = stripped.lstrip("- ").replace("**", "").strip()
                if candidate:
                    snippet = candidate[:90] + ("…" if len(candidate) > 90 else "")
                    break
            full = f"## {heading}\n\n{content}".strip()
            entries.append(
                {
                    "title": title,
                    "date": match.group(1) if match else "—",
                    "size": len(full.encode("utf-8")),
                    "snippet": snippet,
                    "content": full,
                }
            )
        return entries

    @staticmethod
    def _parse_lessons(text: str) -> list[dict[str, Any]]:
        """按 ``## 日期 · 标题`` 切分 LESSONS.md，跳过 type/confidence 元数据行。"""
        blocks: list[tuple[str, list[str]]] = []
        heading: str | None = None
        body: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if heading is not None:
                    blocks.append((heading, body))
                heading = line[3:].strip()
                body = []
            elif heading is not None:
                body.append(line)
        if heading is not None:
            blocks.append((heading, body))

        items: list[dict[str, Any]] = []
        for heading, lines in blocks:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", heading)
            title = (
                re.sub(r"^\d{4}-\d{2}-\d{2}\s*[·\-]\s*", "", heading).strip()
                or heading
            )
            snippet = ""
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("- type:"):
                    continue
                candidate = stripped.lstrip("- ").replace("**", "").strip()
                if candidate:
                    snippet = candidate[:90] + ("…" if len(candidate) > 90 else "")
                    break
            content = f"## {heading}\n\n" + "\n".join(lines).strip()
            items.append(
                {
                    "title": title,
                    "date": match.group(1) if match else "—",
                    "size": len(content.encode("utf-8")),
                    "snippet": snippet,
                    "content": content.strip(),
                }
            )
        return items

    def _memory_page(self) -> dict[str, Any]:
        """设置页 Memory 分类的结构化数据：统计卡、长期记忆条目、每日日志。"""
        memory_dir = project_path("memory_dir")
        entries: list[dict[str, Any]] = []

        memory_file = memory_dir / "MEMORY.md"
        memory_exists = memory_file.is_file()
        if memory_exists:
            try:
                entries = self._parse_memory_entries(
                    memory_file.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                entries = []

        def _meta(path: Path) -> tuple[int, str]:
            try:
                stat = path.stat()
            except OSError:
                return 0, ""
            return stat.st_size, datetime.fromtimestamp(stat.st_mtime).strftime(
                "%m-%d %H:%M"
            )

        long_term_size, long_term_updated = (
            _meta(memory_file) if memory_exists else (0, "")
        )

        lessons_file = memory_dir / "LESSONS.md"
        lessons_exists = lessons_file.is_file()
        lessons_count = 0
        lessons_size = 0
        lessons_updated = ""
        lesson_items: list[dict[str, Any]] = []
        if lessons_exists:
            lessons_size, lessons_updated = _meta(lessons_file)
            try:
                text = lessons_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            lesson_items = self._parse_lessons(text)
            bullets = [
                line for line in text.splitlines() if line.startswith("- ")
            ]
            lessons_count = len(lesson_items) or len(bullets)

        logs: list[dict[str, Any]] = []
        daily_dir = memory_dir / "memory"
        if daily_dir.is_dir():
            for path in sorted(daily_dir.glob("*.md"), reverse=True):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                logs.append(
                    {
                        "name": path.name,
                        "date": path.stem,
                        "size": path.stat().st_size,
                        "count": sum(
                            1
                            for line in text.splitlines()
                            if line.startswith("## ")
                        ),
                        "content": text,
                    }
                )

        return {
            "path": str(memory_dir),
            "long_term": {
                "exists": memory_exists,
                "count": len(entries),
                "size": long_term_size,
                "updated": long_term_updated,
            },
            "lessons": {
                "exists": lessons_exists,
                "count": lessons_count,
                "size": lessons_size,
                "updated": lessons_updated,
                "items": lesson_items,
            },
            "daily": {
                "days": len(logs),
                "latest": logs[0]["date"] if logs else "",
                "latest_count": logs[0]["count"] if logs else 0,
            },
            "entries": entries,
            "logs": logs,
        }

    def _settings_sections(self) -> list[tuple[str, str, str]]:
        skills_dir = project_path("skills_dir")
        skills = (
            sorted(
                path.parent.relative_to(skills_dir).as_posix()
                for path in skills_dir.rglob("SKILL.md")
            )
            if skills_dir.is_dir()
            else []
        )
        rules: list[str] = []
        rules_dir = project_path("rules_dir")
        if rules_dir.is_dir():
            for path in sorted(rules_dir.glob("*.md")):
                title = ""
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("#"):
                            title = line.lstrip("# ").strip()
                            break
                except OSError:
                    pass
                rules.append(f"- {path.name}" + (f" — {title}" if title else ""))
        for name in CONFIG.get("extra_rules", []):
            if (Path.cwd() / name).is_file():
                rules.append(f"- {name} (extra)")

        mcp_path = project_path("mcp_config")
        servers: dict = {}
        if mcp_path.is_file():
            try:
                servers = json.loads(mcp_path.read_text(encoding="utf-8")).get(
                    "mcpServers", {}
                )
            except (OSError, ValueError):
                servers = {}
        connected = {str(getattr(client, "name", "")) for client in self.mcps}
        mcp_lines = [f"Connected: {len(self.mcps)} / {len(servers)}"]
        for name, cfg in servers.items():
            kind = str(cfg.get("type", "stdio"))
            suffix = " · connected" if name in connected else ""
            mcp_lines.append(f"- {name} ({kind}){suffix}")

        # 顺序固定为 Tokens / Rules / Skills / MCP / Memory。
        # Tokens 与 Memory 由设置页的卡片单独渲染，这里仅保留占位。
        return [
            ("tokens", "Tokens", ""),
            ("rules", "Rules", "\n".join(rules) or "No rules loaded."),
            (
                "skills",
                "Skills",
                "\n".join(f"- {name}" for name in skills) or "No skills loaded.",
            ),
            (
                "mcp",
                "MCP",
                "\n".join(mcp_lines) if servers else "No MCP servers configured.",
            ),
            ("memory", "Memory", ""),
        ]

    # --------------------------------------------------------------- 状态小工具
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

    # ------------------------------------------------------------- 工具改动 diff
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
        from .diffs import text_diff

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

    # ------------------------------------------------------------ 目标复述
    async def _restate_goal(self, text: str) -> str:
        """一次性结构化调用，把目标复述成确认卡片上的一句话。"""
        from agentscope.message import UserMsg

        try:
            res = await self._model.generate_structured_output(
                messages=[
                    UserMsg(
                        name="user",
                        content=f"{GOAL_RESTATEMENT_PROMPT}\n\n用户需求：\n{text}",
                    )
                ],
                structured_model=GoalRestatement,
            )
            raw = str((getattr(res, "content", None) or {}).get("restatement") or "")
        except Exception:
            return self._one_line(text, 200)
        points = [
            line.lstrip("-•* 　").strip()
            for line in raw.splitlines()
            if line.strip()
        ]
        return self._one_line("；".join(points), 200) or self._one_line(text, 200)
