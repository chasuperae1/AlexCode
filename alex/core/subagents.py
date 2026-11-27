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
import contextvars
import os
import time
import uuid
from typing import Any, Callable

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

from alex.config import (
    CONFIG,
    ROOT,
    iteration_limit,
    load_rules,
    workspace_path,
)
from alex.core.permissions import (
    ConfirmationMiddleware,
    PermissionGate,
    resolve_permission_mode,
)
from alex.core.tool_chunks import error_chunk
from alex.core.tools import Cmd, WebSearch

# 当前正在执行的子代理：权限确认据此把请求归属到具体子代理。
# 每个子代理调用都在独立 task 里执行，ContextVar 天然按调用隔离。
CURRENT_SUBAGENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "alex_current_subagent", default=None
)


def current_subagent() -> dict | None:
    """返回当前 task 正在执行的子代理信息（没有则返回 None）。"""
    return CURRENT_SUBAGENT.get()


# 运行中的子代理 task：一行事件流里可以按 id 单独取消，互不牵连。
# task 句柄只在创建它的 loop 里有效，因此取用必须在同一进程（daemon）内。
_RUNNING: dict[str, asyncio.Task[Any]] = {}


def running_subagents() -> list[str]:
    """当前还在跑的子代理 id，按开始顺序。"""
    return [sid for sid, task in _RUNNING.items() if not task.done()]


def cancel_subagent(subagent_id: str) -> bool:
    """取消一个子代理；返回是否真的发出取消。

    只取消了这一个 task：同轮其它子代理照常跑，主对话等它们的结果。
    """
    task = _RUNNING.get(str(subagent_id or ""))
    if task is None or task.done():
        return False
    task.cancel()
    return True


class _DeltaCoalescer:
    """把子代理的正文/思考增量攒 80ms 再发，避免事件风暴。"""

    def __init__(self, emit: Callable[[str, dict], None]) -> None:
        self._emit = emit
        self._buffers: dict[str, str] = {}
        self._handle: asyncio.TimerHandle | None = None

    def add(self, channel: str, delta: str) -> None:
        if not delta:
            return
        self._buffers[channel] = self._buffers.get(channel, "") + delta
        if self._handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        self._handle = loop.call_later(0.08, self.flush)

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if not self._buffers:
            return
        pending, self._buffers = self._buffers, {}
        for channel, text in pending.items():
            self._emit("subagent_delta", {"channel": channel, "delta": text})


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
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._backend = backend
        # 与主代理共用同一套确认逻辑和“总是允许”规则
        self._gate = gate or PermissionGate()
        # 复用主代理 toolkit 的 skills / MCP 配置，子代理因此拥有同样能力
        self._toolkit = toolkit
        # 子代理生命周期/流式事件回调；由会话层接进事件总线
        self._on_event = on_event or (lambda _kind, _data: None)

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
            return error_chunk(
                "`prompt` is required: define the sub agent's role and the "
                "full task."
            )
        description = (description or "").strip()
        subagent_id = f"sa_{uuid.uuid4().hex[:8]}"
        label = description or "Subagent"
        task_hint = next(
            (line.strip() for line in prompt.splitlines() if line.strip()), ""
        )[:160]
        started = time.monotonic()
        model_name = str(CONFIG.get("model", {}).get("name", "") or "")

        def emit(kind: str, data: dict) -> None:
            self._on_event(kind, {"id": subagent_id, **data})

        emit(
            "subagent_start",
            {
                "label": label,
                "description": task_hint,
                "model": model_name,
                "prompt": prompt[:2000],
                "started_at": started,
            },
        )
        token = CURRENT_SUBAGENT.set({"id": subagent_id, "label": label})
        progress: dict[str, int] = {"steps": 0}
        task = asyncio.current_task()
        if task is not None:
            _RUNNING[subagent_id] = task
        try:
            text, steps = await self._run(description, prompt, emit, progress)
        except asyncio.CancelledError:
            # 单条 stop 取消的是这一条：把中断结果交回主代理，整轮继续
            emit(
                "subagent_end",
                {
                    "state": "interrupted",
                    "seconds": time.monotonic() - started,
                    "steps": int(progress.get("steps") or 0),
                    "error": "stopped by user",
                },
            )
            return error_chunk(
                "Subagent stopped by user; its partial findings are "
                "unavailable — continue without it."
            )
        except Exception as exc:  # 模型/工具异常交还给主代理
            emit(
                "subagent_end",
                {
                    "state": "failed",
                    "seconds": time.monotonic() - started,
                    "steps": 0,
                    "error": str(exc),
                },
            )
            return error_chunk(f"Subagent failed: {exc}")
        finally:
            _RUNNING.pop(subagent_id, None)
            CURRENT_SUBAGENT.reset(token)

        seconds = time.monotonic() - started
        write_subagent_transcript(subagent_id, label, prompt, text)
        emit(
            "subagent_end",
            {
                "state": "completed",
                "seconds": seconds,
                "steps": steps,
                "result": text[:2000],
            },
        )
        return ToolChunk(
            content=[TextBlock(text=text or "(subagent returned no text)")],
            state=ToolResultState.SUCCESS,
        )

    async def _run(
        self,
        description: str,
        prompt: str,
        emit: Callable[[str, dict], None],
        progress: dict[str, int],
    ) -> tuple[str, int]:
        from agentscope.event import (
            TextBlockDeltaEvent,
            ThinkingBlockDeltaEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
        )
        from agentscope.message import Msg

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
                max_iters=iteration_limit(
                    model_cfg.get("subagent_max_iters", model_cfg.get("max_iters"))
                ),
            ),
        )
        msg = UserMsg(name="user", content=prompt)
        coalescer = _DeltaCoalescer(emit)
        text_parts: list[str] = []
        tool_started: dict[str, float] = {}
        tool_names: dict[str, str] = {}
        steps = 0
        final_text = ""

        def handle(event: Any) -> None:
            nonlocal steps
            if isinstance(event, TextBlockDeltaEvent):
                text_parts.append(event.delta)
                coalescer.add("text", event.delta)
            elif isinstance(event, ThinkingBlockDeltaEvent):
                coalescer.add("thinking", event.delta)
            elif isinstance(event, ToolCallStartEvent):
                steps += 1
                progress["steps"] = steps
                tool_started[event.tool_call_id] = time.monotonic()
                tool_names[event.tool_call_id] = event.tool_call_name
                emit(
                    "subagent_tool",
                    {
                        "call_id": event.tool_call_id,
                        "name": event.tool_call_name,
                        "phase": "start",
                        "state": "running",
                        "elapsed": 0.0,
                    },
                )
            elif isinstance(event, ToolResultEndEvent):
                state = getattr(event.state, "value", event.state)
                elapsed = time.monotonic() - tool_started.pop(
                    event.tool_call_id, time.monotonic()
                )
                emit(
                    "subagent_tool",
                    {
                        "call_id": event.tool_call_id,
                        "name": tool_names.pop(event.tool_call_id, "Tool"),
                        "phase": "end",
                        "state": str(state),
                        "elapsed": elapsed,
                    },
                )
            elif isinstance(event, Msg):
                content = event.get_text_content()
                if content:
                    final_text = content

        try:
            async for event in agent.reply_stream(msg, yield_final_msg=True):
                if not isinstance(
                    event, (TextBlockDeltaEvent, ThinkingBlockDeltaEvent)
                ):
                    coalescer.flush()
                handle(event)
        finally:
            # 取消 / 异常时也要把已积累的增量发出去，正文不丢半句
            coalescer.flush()
        return (final_text or "".join(text_parts)).strip(), steps


def write_subagent_transcript(
    subagent_id: str, label: str, prompt: str, text: str
) -> None:
    """子代理完整正文落盘：事件里只传摘要，长输出不进协议。"""
    directory = ROOT / "data" / "subagents"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        body = [
            f"# {label}",
            "",
            "## Task",
            "",
            prompt,
            "",
            "## Result",
            "",
            text,
        ]
        path = directory / f"{subagent_id}.md"
        path.write_text("\n".join(body), encoding="utf-8")
        # 只保留最近 60 份，避免无限堆积
        entries = sorted(
            directory.glob("sa_*.md"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in entries[60:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:  # 落盘失败不影响子代理结果
        pass
