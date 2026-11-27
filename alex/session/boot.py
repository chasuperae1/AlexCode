"""Agent 会话内核分包：会话生命周期：初始化、快照/历史负载、关闭、状态与提示。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict


from alex.config import (
    CONFIG,
    lock_skill_files,
    project_path,
    workspace_path,
)
from alex.core.snapshots import Checkpoint, SnapshotStore

from alex.session.common import (
    ASSISTANT,
    DEFAULT_WELCOME,
)




class BootMixin:
    """运行时初始化、启动阶段回报与初始快照。"""

    def _prepare_runtime(self):
        import agentscope

        agentscope.setup_logger("WARNING")
        from alex.core.agent import build_agent, build_mcps, build_model
        from alex.core.permissions import PermissionGate
        from alex.core.tools import TrackedLocalBackend

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
        except Exception as exc:  # 不阻止启动，但要让用户知道只读保护没生效
            self._notice(
                f"Skills 只读锁定失败：{exc}",
                title="Skills 未锁定",
                severity="warning",
                timeout=6,
            )
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
            on_compacted=self._on_compacted,
        )
        self.toolkit = toolkit
        # 历史在 agent 装配后从恢复的状态里取，和旧行为保持一致
        if restored:
            self.history = self._restore_history(
                self._history_from_state(self.agent.state)
            )
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
        self.emit(
            "history_reset",
            {
                **self._history_view(),
                "checkpoints": [asdict(item) for item in self._checkpoints],
            },
        )
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

    def _backfill_checkpoint_prompts(self, checkpoints: list[Checkpoint]) -> None:
        """旧检查点把工具名存成了 prompt，回填成真实用户消息以匹配历史。"""
        context = getattr(getattr(self, "agent", None), "state", None)
        context = getattr(context, "context", None) or []
        if not context:
            return
        for checkpoint in checkpoints:
            index = int(getattr(checkpoint, "context_start", 0) or 0)
            message = None
            for offset in (0, -1):
                candidate = index + offset
                if not 0 <= candidate < len(context):
                    continue
                item = context[candidate]
                if getattr(item, "role", "") == "user":
                    message = item
                    break
            if message is None:
                continue
            try:
                text = message.get_text_content().strip()
            except Exception:
                continue
            if text:
                checkpoint.prompt = text

    def _init_snapshots(self) -> None:
        self._last_tree = None
        try:
            self._snapshots = SnapshotStore(workspace_path(create=True))
            self._checkpoints = list(self._snapshots.checkpoints)
            self._backfill_checkpoint_prompts(self._checkpoints)
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

