"""Agent 会话内核分包：工作区与模型：重载配置、切换工作区/模型/权限、读取 diff。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any


from alex.config import (
    CONFIG,
    LOCKED_SKILLS,
    ROOT,
    project_path,
    reload_config,
    save_config,
    workspace_path,
)
from alex.core import reasoning
from alex.core.snapshots import SnapshotStore

from alex.session.common import (
    SessionError,
)


class WorkspaceMixin:
    """工作区与模型：重载配置、切换工作区/模型/权限、读取 diff。"""

    async def reload(self) -> dict[str, Any]:
        if self._busy:
            raise SessionError("busy", "a reply is still running")
        from alex.core.agent import build_agent, build_mcps, build_model
        from alex.core.tools import TrackedLocalBackend

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
            on_compacting=self._notify_compacting,
            on_subagent_event=self._on_subagent_event,
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
        from alex.core.agent import build_agent, build_model, save_state
        from alex.core.tools import TrackedLocalBackend

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
            on_compacting=self._notify_compacting,
            on_subagent_event=self._on_subagent_event,
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
        from alex.memory import clear_memory, find_memory_middleware

        try:
            middleware = find_memory_middleware(self.agent)
            if middleware is not None:
                await middleware.clear()
            else:
                await asyncio.to_thread(clear_memory, str(ROOT), CONFIG["memory_dir"])
        except Exception as exc:
            raise SessionError("internal", f"Clear memory failed: {exc}") from exc

    async def set_permission_mode(self, mode: str) -> None:
        from alex.core.permissions import resolve_permission_mode

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
        from alex.core.agent import build_agent, build_model
        from alex.core.tools import TrackedLocalBackend

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
            on_compacting=self._notify_compacting,
            on_subagent_event=self._on_subagent_event,
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


