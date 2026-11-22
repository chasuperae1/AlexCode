"""纯文本 REPL 入口：``python -m alex.cli``。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import agentscope
from agentscope.message import UserMsg

from alex.core.agent import build_agent, build_mcps, build_model, save_state
from alex.config import CONFIG, ROOT, project_path, save_config, workspace_path
from alex.memory import clear_memory, find_memory_middleware
from alex.core.permissions import (
    MODE_NAMES,
    ConfirmChoice,
    PermissionGate,
    permission_detail,
    resolve_permission_mode,
)
from alex.core.tools import TrackedLocalBackend


async def _terminal_confirm(agent_name, tool, tool_input, decision) -> ConfirmChoice:
    """终端里的权限确认。"""
    print(f"\n[permission] {agent_name} · {tool.name}")
    print(permission_detail(tool.name, tool_input))
    if decision.decision_reason:
        print(f"Reason: {decision.decision_reason}")
    prompt = "Allow? [y] once / [a] lways / [N] o: "
    try:
        answer = (await asyncio.to_thread(input, prompt)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return ConfirmChoice.DENY
    if answer in {"a", "always"}:
        return ConfirmChoice.ALWAYS if not decision.bypass_immune else ConfirmChoice.ALLOW
    if answer in {"y", "yes"}:
        return ConfirmChoice.ALLOW
    return ConfirmChoice.DENY


async def _terminal_ask(payload: dict) -> dict:
    """终端里的选择题：输入编号选择，其他文本作为自定义回答。"""
    question = str(payload.get("question", ""))
    options = payload.get("options", []) or []
    multi = bool(payload.get("multi"))
    print(f"\n? {question}")
    for index, item in enumerate(options, 1):
        description = str(item.get("description", ""))
        suffix = f"  {description}" if description else ""
        print(f"  {index}. {item['label']}{suffix}")
    if multi:
        prompt = f"Select 1-{len(options)} (comma separated, Enter to skip): "
    else:
        prompt = f"Select 1-{len(options)} (Enter to skip): "
    try:
        raw = (await asyncio.to_thread(input, prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return {"skipped": True, "choices": [], "other": ""}
    if not raw:
        return {"skipped": True, "choices": [], "other": ""}
    picks: list[str] = []
    tokens = re.split(r"[,\s，]+", raw)
    if tokens and all(token.isdigit() for token in tokens):
        for token in tokens:
            number = int(token)
            if 1 <= number <= len(options):
                picks.append(str(options[number - 1]["label"]))
        if picks:
            if not multi:
                picks = picks[:1]
            return {"skipped": False, "choices": picks, "other": ""}
    return {"skipped": False, "choices": [], "other": raw}


async def _run_goal(agent, model, backend, text: str) -> str:
    """跑一次 GoalPipeline，返回验收结束时的文本。"""
    from agentscope.pipeline import GoalPipeline

    from alex.core.agent import build_verifier

    pipeline = GoalPipeline(
        executor=agent,
        verifier=build_verifier(model, backend),
        verifier_reset_context=True,
        max_iters=int(CONFIG["model"].get("goal_max_iters", 3)),
    )
    final_text = ""
    async for event in pipeline.reply_stream(
        UserMsg(name="user", content=text)
    ):
        if hasattr(event, "get_text_content"):
            final_text = event.get_text_content() or final_text
    return final_text


@dataclass
class _Runtime:
    """REPL 运行期对象：模型、工作区后端、MCP、权限闸门与当前 agent。"""

    model: Any
    backend: TrackedLocalBackend
    mcps: list
    gate: PermissionGate
    agent: Any
    toolkit: Any
    restored: bool = False

    async def rebuild(self) -> None:
        """按当前工作区 / 权限配置重建 agent 与工具列表。"""
        self.agent, self.toolkit, _ = await build_agent(
            self.model, self.backend, self.mcps, gate=self.gate, ask=_terminal_ask
        )


async def _bootstrap() -> _Runtime:
    """解析运行参数：模型、工作区后端、MCP、权限闸门，并建好首个 agent。"""
    agentscope.setup_logger("WARNING")
    model = build_model()
    backend = TrackedLocalBackend(workdir=str(workspace_path(create=True)))
    mcps = await build_mcps()
    gate = PermissionGate(_terminal_confirm)
    agent, toolkit, restored = await build_agent(
        model, backend, mcps, gate=gate, ask=_terminal_ask
    )
    return _Runtime(
        model=model,
        backend=backend,
        mcps=mcps,
        gate=gate,
        agent=agent,
        toolkit=toolkit,
        restored=restored,
    )


async def _print_banner(runtime: _Runtime) -> None:
    if runtime.restored:
        print(f"Restored the previous session ({CONFIG['session_file']}).")
    print(
        f"Alex is ready: model {CONFIG['model']['name']}, "
        f"{len(await runtime.toolkit.get_tool_schemas())} tools, "
        f"{len(runtime.mcps)} MCP servers, permission {CONFIG['permission_mode']}."
    )
    print("Type a message to chat; /clear resets memory; /help for help; /exit to quit.")


async def _cmd_exit(runtime: _Runtime, arg: str) -> bool:
    return True


async def _cmd_help(runtime: _Runtime, arg: str) -> bool:
    print(
        "/cd [path] changes the workspace; /permission "
        "accept|bypass|explore; /goal run until an independent "
        "verifier passes it; /init memory clears all "
        "memory; /clear resets the session; /exit quits.\n"
    )
    return False


async def _cmd_init(runtime: _Runtime, arg: str) -> bool:
    if arg.lower() != "memory":
        print("Usage: /init memory\n")
        return False
    middleware = find_memory_middleware(runtime.agent)
    if middleware is not None:
        await middleware.clear()
    else:
        await asyncio.to_thread(clear_memory, str(ROOT), CONFIG["memory_dir"])
    print("Memory cleared.\n")
    return False


async def _cmd_cd(runtime: _Runtime, arg: str) -> bool:
    if not arg:
        print(f"Workspace: {workspace_path()}\n")
        return False
    target = Path(arg).expanduser()
    if not target.is_absolute():
        target = workspace_path() / target
    try:
        target = target.resolve()
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise NotADirectoryError(target)
    except OSError as exc:
        print(f"Cannot use workspace: {exc}\n")
        return False
    CONFIG["workdir"] = str(target)
    save_config()
    save_state(runtime.agent.state)
    runtime.backend = TrackedLocalBackend(workdir=str(target))
    await runtime.rebuild()
    print(f"Workspace: {target}\n")
    return False


async def _cmd_permission(runtime: _Runtime, arg: str) -> bool:
    current = CONFIG.get("permission_mode", "accept")
    if not arg:
        print(f"Permission: {current}. Modes: {', '.join(MODE_NAMES)}\n")
        return False
    mode = arg.lower()
    if mode not in MODE_NAMES:
        print(f"Unknown mode {mode}; use {', '.join(MODE_NAMES)}\n")
        return False
    CONFIG["permission_mode"] = mode
    save_config()
    runtime.agent.state.permission_context.mode = resolve_permission_mode(mode)
    print(f"Permission mode: {mode}\n")
    return False


async def _cmd_goal(runtime: _Runtime, arg: str) -> bool:
    if not arg:
        print("Usage: /goal <what must be achieved>\n")
        return False
    try:
        answer = await _run_goal(runtime.agent, runtime.model, runtime.backend, arg)
    except Exception as exc:
        print(f"Goal failed: {exc}\n")
        return False
    print(f"Alex: {answer}\n")
    save_state(runtime.agent.state)
    return False


async def _cmd_clear(runtime: _Runtime, arg: str) -> bool:
    path = project_path("session_file")
    if path.is_file():
        path.unlink()
    await runtime.rebuild()
    print("Memory cleared; starting a new session.\n")
    return False


_COMMANDS = {
    "/exit": _cmd_exit,
    "/quit": _cmd_exit,
    "/help": _cmd_help,
    "/init": _cmd_init,
    "/cd": _cmd_cd,
    "/permission": _cmd_permission,
    "/goal": _cmd_goal,
    "/clear": _cmd_clear,
}


async def _dispatch_command(runtime: _Runtime, raw: str) -> bool:
    """分发斜杠命令；返回 True 表示应当退出 REPL。"""
    command, _, arg = raw.partition(" ")
    handler = _COMMANDS.get(command.lower())
    if handler is None:
        print("Unknown command. Type /help for help.\n")
        return False
    return bool(await handler(runtime, arg.strip()))


async def _repl(runtime: _Runtime) -> None:
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_input:
            continue
        if user_input.startswith("/"):
            if await _dispatch_command(runtime, user_input):
                return
            continue

        reply = await runtime.agent.reply(
            UserMsg(name="user", content=user_input)
        )
        print(f"Alex: {reply.get_text_content()}\n")
        save_state(runtime.agent.state)


async def _shutdown(runtime: _Runtime) -> None:
    save_state(runtime.agent.state)
    for client in runtime.mcps:
        if client.is_stateful and client.is_connected:
            await client.close()
    print("Session saved.")


async def main() -> None:
    runtime = await _bootstrap()
    await _print_banner(runtime)
    await _repl(runtime)
    await _shutdown(runtime)


if __name__ == "__main__":
    asyncio.run(main())
