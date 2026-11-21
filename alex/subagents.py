"""子代理：主代理按需派发的独立 Agent。

设计约定：
- 只有一个通用类型：角色和任务全部由主代理在 ``prompt`` 里定义。
- 每次派发都新建独立的 ``AgentState`` 与工具实例，上下文互不共享；
  模型、backend、rules、skills 与 MCP 客户端复用主代理的配置。
- 工具标记为 concurrency-safe：同一轮里的多个 ``Subagent`` 调用由框架
  ``asyncio.gather`` 并发执行，没有代码层面的数量上限。
- 子代理工具集里没有 ``Subagent``，因此最多两层：主代理 + 一层子代理。

本模块只组合 AgentScope 的 ``Agent`` / ``Toolkit``，不修改框架源码。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.message import TextBlock, UserMsg
from agentscope.permission import (
    PermissionBehavior,
    PermissionDecision,
)
from agentscope.state import AgentState
from agentscope.tool import (
    Bash,
    Edit,
    Glob,
    Grep,
    PowerShell,
    Read,
    Toolkit,
    ToolBase,
    ToolChunk,
    Write,
)
from agentscope.tool._response import ToolResultState

from .config import CONFIG, load_rules, workspace_path
from .permissions import (
    ConfirmationMiddleware,
    PermissionGate,
    resolve_permission_mode,
)
from .tools import Cmd, WebSearch

# 子代理能用的工具：完整读写 + 命令执行 + 联网检索
SUBAGENT_TOOLS = (
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebSearch",
    "Shell",
    "PowerShell",
)


def _make_tool(name: str, backend, workspace: str) -> ToolBase | None:
    """按名称创建子代理可用的框架工具。"""
    if name == "Read":
        return Read(backend=backend)
    if name == "Write":
        return Write(backend=backend)
    if name == "Edit":
        return Edit(backend=backend)
    if name == "Glob":
        return Glob(backend=backend)
    if name == "Grep":
        return Grep(backend=backend)
    if name == "WebSearch":
        return WebSearch()
    if name == "Shell":
        if os.name == "nt":
            return Cmd(cwd=workspace, backend=backend)
        return Bash(cwd=workspace, backend=backend)
    if name == "PowerShell" and os.name == "nt":
        return PowerShell(cwd=workspace, backend=backend)
    return None


def build_subagent_tools(
    backend,
    workspace: str | os.PathLike[str],
) -> list[ToolBase]:
    """构建子代理工具集；这里永远不包含 ``Subagent``。"""
    workspace = os.fspath(workspace)
    tools = [_make_tool(name, backend, workspace) for name in SUBAGENT_TOOLS]
    return [tool for tool in tools if tool is not None]


def _system_prompt(
    description: str,
    workspace,
    has_skills: bool = False,
    has_mcp: bool = False,
) -> str:
    parts = [
        "You are a subagent dispatched by Alex.",
        "Your role and task are defined by the parent agent in the task "
        "prompt below; follow it exactly.",
        f"Workspace: {workspace}",
        "You have full tool access: read, write, edit and search files, run "
        "commands, and search the web. Prefer dedicated tools over shell "
        "commands.",
        "You are a leaf agent: you cannot ask the user questions and you "
        "cannot delegate to another agent. Just do the work.",
        "Finish with one final message containing the result the parent "
        "needs: concrete findings, file paths and any changes you made. "
        "Skip progress narration.",
    ]
    if description:
        parts.insert(2, f"Task label: {description}")
    if has_skills:
        parts.append(
            "Skills are available through the Skill tool. When the task "
            "matches a skill, load it before doing specialized work."
        )
    if has_mcp:
        parts.append(
            "MCP tools from the configured servers are available alongside "
            "the built-in tools; use them when the task needs them."
        )
    rules = load_rules()
    if rules:
        parts.append(f"<rules>\n{rules}\n</rules>")
    return "\n".join(parts)


def _error(message: str) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=message)],
        state=ToolResultState.ERROR,
    )


class Subagent(ToolBase):
    """派发一个独立子代理处理自包含的子任务。"""

    name: str = "Subagent"
    description: str = """Dispatches an isolated sub agent to complete one delegated task.

Each call creates a fresh agent with its own context and full tools. You
define the agent's role entirely in `prompt`; there are no predefined agent
types. The sub agent cannot see this conversation and returns only its final
text result.

Call this tool multiple times in the same reply to run sub agents
concurrently. Calls in one reply execute in parallel (one fresh instance per
call), so use a separate call per independent task and put the role, goal and
output format in each prompt.

When to use:
- Independent, self-contained subtasks that would flood this conversation.
- Parallel investigations or implementations that do not depend on each other.

When NOT to use:
- One-step work (read a file, run one command, answer a question).
- Tasks that need back-and-forth with the user.
- Anything you cannot fully specify in a single prompt.

Sub agents are leaf agents: they cannot delegate further (max depth is 2
levels: main agent + one sub agent).
"""
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Short (3-6 words) label for the subtask.",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete, self-contained instructions, including the "
                    "role the sub agent should play. It cannot see the "
                    "current conversation."
                ),
            },
        },
        "required": ["prompt"],
    }
    # 多个调用会被框架放进同一个并发批次（asyncio.gather）
    is_concurrency_safe: bool = True
    is_read_only: bool = False
    is_external_tool: bool = False
    is_state_injected: bool = False

    def __init__(
        self,
        model,
        backend,
        gate: PermissionGate | None = None,
        toolkit: Toolkit | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._backend = backend
        # 与主代理共用同一套确认逻辑和“总是允许”规则
        self._gate = gate or PermissionGate()
        # 复用主代理 toolkit 的 skills / MCP 配置，子代理因此拥有同样能力
        self._toolkit = toolkit

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context,
    ) -> PermissionDecision:
        # 派发本身不是文件修改或危险命令，不需要确认；
        # 子代理内部的写文件/危险命令仍由同一个 gate 逐一确认。
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Subagent dispatch is allowed.",
            decision_reason="Subagent internals are permission-checked separately",
        )

    async def call(
        self,
        prompt: str = "",
        description: str = "",
    ) -> ToolChunk:
        prompt = (prompt or "").strip()
        if not prompt:
            return _error(
                "`prompt` is required: define the sub agent's role and the "
                "full task."
            )
        description = (description or "").strip()
        try:
            text = await self._run(description, prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 模型/工具异常交还给主代理
            return _error(f"Subagent failed: {exc}")

        return ToolChunk(
            content=[TextBlock(text=text or "(subagent returned no text)")],
            state=ToolResultState.SUCCESS,
        )

    async def _run(self, description: str, prompt: str) -> str:
        workspace = workspace_path(create=True)
        basic = (
            self._toolkit.tool_groups[0]
            if self._toolkit is not None
            else None
        )
        loaders = list(basic.skills_or_loaders) if basic is not None else []
        mcps = list(basic.mcps) if basic is not None else []
        toolkit = Toolkit(
            tools=build_subagent_tools(self._backend, workspace),
            skills_or_loaders=loaders or None,
            mcps=mcps or None,
        )
        state = AgentState()
        state.permission_context.mode = resolve_permission_mode(
            CONFIG.get("permission_mode", "accept")
        )
        model_cfg = CONFIG["model"]
        agent = Agent(
            name="subagent",
            system_prompt=_system_prompt(
                description, workspace, bool(loaders), bool(mcps)
            ),
            model=self._model,
            toolkit=toolkit,
            state=state,
            middlewares=[ConfirmationMiddleware(self._gate)],
            context_config=ContextConfig(
                trigger_ratio=model_cfg.get("context_trigger_ratio", 0.8),
            ),
            react_config=ReActConfig(
                max_iters=int(
                    model_cfg.get("subagent_max_iters", model_cfg["max_iters"])
                ),
            ),
        )
        msg = await agent.reply(UserMsg(name="user", content=prompt))
        return (msg.get_text_content() or "").strip()
