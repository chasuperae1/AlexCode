"""纯文本 REPL 入口：``python -m alex.cli``。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

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


async def main() -> None:
    agentscope.setup_logger("WARNING")
    model = build_model()
    backend = TrackedLocalBackend(workdir=str(workspace_path(create=True)))
    mcps = await build_mcps()
    gate = PermissionGate(_terminal_confirm)
    agent, toolkit, restored = await build_agent(
        model, backend, mcps, gate=gate, ask=_terminal_ask
    )

    if restored:
        print(f"Restored the previous session ({CONFIG['session_file']}).")
    print(
        f"Alex is ready: model {CONFIG['model']['name']}, "
        f"{len(await toolkit.get_tool_schemas())} tools, "
        f"{len(mcps)} MCP servers, permission {CONFIG['permission_mode']}."
    )
    print("Type a message to chat; /clear resets memory; /help for help; /exit to quit.")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.startswith("/"):
            command, _, arg = user_input.partition(" ")
            command = command.lower()
            arg = arg.strip()
            if command in {"/exit", "/quit"}:
                break
            if command == "/help":
                print(
                    "/cd [path] changes the workspace; /permission "
                    "accept|bypass|explore; /goal run until an independent "
                    "verifier passes it; /init memory clears all "
                    "memory; /clear resets the session; /exit quits.\n"
                )
                continue
            if command == "/init":
                if arg.lower() != "memory":
                    print("Usage: /init memory\n")
                    continue
                middleware = find_memory_middleware(agent)
                if middleware is not None:
                    await middleware.clear()
                else:
                    await asyncio.to_thread(
                        clear_memory, str(ROOT), CONFIG["memory_dir"]
                    )
                print("Memory cleared.\n")
                continue
            if command == "/cd":
                if not arg:
                    print(f"Workspace: {workspace_path()}\n")
                    continue
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
                    continue
                CONFIG["workdir"] = str(target)
                save_config()
                save_state(agent.state)
                backend = TrackedLocalBackend(workdir=str(target))
                agent, toolkit, _ = await build_agent(
                    model, backend, mcps, gate=gate, ask=_terminal_ask
                )
                print(f"Workspace: {target}\n")
                continue
            if command == "/permission":
                current = CONFIG.get("permission_mode", "accept")
                if not arg:
                    print(
                        f"Permission: {current}. Modes: "
                        f"{', '.join(MODE_NAMES)}\n"
                    )
                    continue
                mode = arg.lower()
                if mode not in MODE_NAMES:
                    print(f"Unknown mode {mode}; use {', '.join(MODE_NAMES)}\n")
                    continue
                CONFIG["permission_mode"] = mode
                save_config()
                agent.state.permission_context.mode = resolve_permission_mode(
                    mode
                )
                print(f"Permission mode: {mode}\n")
                continue
            if command == "/goal":
                if not arg:
                    print("Usage: /goal <what must be achieved>\n")
                    continue
                try:
                    answer = await _run_goal(agent, model, backend, arg)
                except Exception as exc:
                    print(f"Goal failed: {exc}\n")
                    continue
                print(f"Alex: {answer}\n")
                save_state(agent.state)
                continue
            if command == "/clear":
                path = project_path("session_file")
                if path.is_file():
                    path.unlink()
                agent, toolkit, _ = await build_agent(
                    model, backend, mcps, gate=gate, ask=_terminal_ask
                )
                print("Memory cleared; starting a new session.\n")
                continue
            print("Unknown command. Type /help for help.\n")
            continue

        reply = await agent.reply(UserMsg(name="user", content=user_input))
        print(f"Alex: {reply.get_text_content()}\n")
        save_state(agent.state)

    save_state(agent.state)
    for client in mcps:
        if client.is_stateful and client.is_connected:
            await client.close()
    print("Session saved.")


if __name__ == "__main__":
    asyncio.run(main())
