"""Agent 装配：模型、MCP、工具与会话状态。

全部复用 AgentScope 自带组件，仅做装配：
- Agent / AgentState        : ReAct 循环、状态、自动上下文压缩
- DeepSeekChatModel         : 模型接入
- Toolkit + 内置工具        : 文件读写、搜索、命令执行
- LocalSkillLoader          : 技能（skills/<name>/SKILL.md）
- MCPClient                 : MCP 工具（config/mcp_servers.json）
- PermissionEngine          : 工具权限（config/config.json）
- Subagent (tool)          : 并发派发子代理（alex/subagents.py）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from dotenv import load_dotenv

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.credential import DeepSeekCredential, OpenAICredential
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.model import DeepSeekChatModel, OpenAIChatModel
from agentscope.skill import LocalSkillLoader
from agentscope.state import AgentState
from agentscope.tool import (
    Bash,
    Edit,
    Glob,
    Grep,
    LocalBackend,
    PowerShell,
    Read,
    TaskGet,
    TaskList,
    TaskUpdate,
    Toolkit,
    Write,
)

from alex.memory import DailyMemoryMiddleware

from alex.config import (
    CONFIG,
    LOCKED_SKILLS,
    ROOT,
    load_rules,
    project_path,
    workspace_path,
)
from alex.core.permissions import (
    ConfirmationMiddleware,
    PermissionGate,
    resolve_permission_mode,
)
from alex.core.reasoning import request_parameters
from alex.core.subagents import Subagent
from alex.core.tools import AskCallback, AskUser, Cmd, PlanTaskCreate, WebSearch


class ThinkingSafeDeepSeekModel(DeepSeekChatModel):
    """thinking 模式下不发送 tool_choice。

    DeepSeek 的 thinking 模式只接受普通工具调用，不认 tool_choice：框架在
    max_iters 收尾时强制 ``none``、结构化输出时指定具体工具，都会被 API 以
    400 "Thinking mode does not support this tool_choice" 拒绝。这里在发送前
    丢掉该参数，让模型按提示自行收尾。
    """

    def _format_tools(self, tools, tool_choice):
        if self.parameters.thinking_enable:
            tool_choice = None
        return super()._format_tools(tools, tool_choice)


class FilteredLocalSkillLoader(LocalSkillLoader):
    """Local skill loader that hides explicitly excluded skill names."""

    def __init__(self, directory: str, excluded: set[str]) -> None:
        super().__init__(directory, scan_subdir=True)
        self.excluded = excluded

    async def list_skills(self):
        return [
            skill
            for skill in await super().list_skills()
            if skill.name not in self.excluded
        ]


def build_toolkit(backend: LocalBackend, mcps: list[MCPClient]) -> Toolkit:
    """按当前系统装配内置工具，通用能力全部来自框架。"""
    workspace = str(workspace_path(create=True))
    tools = [
        Read(backend=backend),
        Write(backend=backend),
        Edit(backend=backend),
        Glob(backend=backend),
        Grep(backend=backend),
        WebSearch(),
        # Windows 默认 cmd（不依赖 PowerShell 权限），PowerShell 作为补充
        Cmd(cwd=workspace, backend=backend)
        if os.name == "nt"
        else Bash(cwd=workspace, backend=backend),
        PowerShell(cwd=workspace, backend=backend)
        if os.name == "nt"
        else None,
        # 任务清单：框架内置，多步骤任务的进度跟踪（描述按本项目加细粒度要求）
        PlanTaskCreate(),
        TaskList(),
        TaskGet(),
        TaskUpdate(),
    ]
    tools = [tool for tool in tools if tool is not None]

    skills_dir = project_path("skills_dir")
    has_skills = skills_dir.is_dir() and any(skills_dir.rglob("SKILL.md"))
    # 内置只读技能始终可用，不受 excluded_skills 影响
    excluded = set(CONFIG.get("excluded_skills", [])) - LOCKED_SKILLS
    loaders = (
        [FilteredLocalSkillLoader(str(skills_dir), excluded)] if has_skills else None
    )
    return Toolkit(tools=tools, skills_or_loaders=loaders, mcps=mcps or None)


GOAL_VERIFIER_PROMPT = (
    "You are the independent verifier in a goal pipeline. "
    "The executor reports what it has done; you must check the actual "
    "workspace before judging.\n"
    "Use Read / Glob / Grep to inspect files, outputs and artifacts. "
    "Never modify anything.\n"
    "Judge strictly: 'pass' only when the goal is fully achieved; 'fail' "
    "with concrete, actionable issues (file paths, missing behavior); "
    "'impossible' only when the goal cannot be achieved at all."
)


class VerifierAgent(Agent):
    """验收 Agent：GoalPipeline 会消费最终消息，这里额外把判定回调给会话。"""

    def __init__(self, *args, on_result=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_result = on_result

    async def reply_stream(self, *args, **kwargs):
        from agentscope.message import Msg
        from agentscope.types import ReplyFinishedReason

        async for event in super().reply_stream(*args, **kwargs):
            if (
                isinstance(event, Msg)
                and event.finished_reason == ReplyFinishedReason.COMPLETED
                and event.structured_output is not None
            ):
                if self._on_result is not None:
                    self._on_result(dict(event.structured_output))
            yield event


def build_verifier(model, backend, on_result=None) -> Agent:
    """构建 /goal 的验收 Agent：独立状态、只读工具。"""
    toolkit = Toolkit(
        tools=[
            Read(backend=backend),
            Glob(backend=backend),
            Grep(backend=backend),
        ]
    )
    return VerifierAgent(
        name="Verifier",
        system_prompt=GOAL_VERIFIER_PROMPT,
        model=model,
        toolkit=toolkit,
        on_result=on_result,
    )


def _mcp_logger() -> logging.Logger:
    """MCP 启停日志：写 data/mcp.log，避免 TUI 下 stderr 不可用时丢失报错。"""
    logger = logging.getLogger("alex.mcp")
    if not logger.handlers:
        path = ROOT / "data" / "mcp.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        logger.propagate = False
    return logger


def _node_entry(package: str) -> Path | None:
    """已全局安装的 npm 包入口文件；未安装返回 None。"""
    appdata = os.environ.get("APPDATA")
    roots = [Path("/usr/local/lib/node_modules")]
    if appdata:
        roots.insert(0, Path(appdata) / "npm" / "node_modules")
    for root in roots:
        manifest = root / package / "package.json"
        if not manifest.is_file():
            continue
        try:
            bin_field = json.loads(manifest.read_text(encoding="utf-8")).get("bin")
        except (OSError, ValueError):
            continue
        entry = (
            bin_field
            if isinstance(bin_field, str)
            else next(iter((bin_field or {}).values()), None)
        )
        if entry:
            return (root / package / entry).resolve()
    return None


def _direct_node(command: str, args: list[str]) -> tuple[str, list[str]] | None:
    """把 ``npx -y <pkg>`` 换成 ``node <entry>``。

    npx 每次启动都要向 registry 解析版本（约 2s），全局装过包后
    直接拉起入口文件可以把这段时间省掉。
    """
    if Path(command).name.lower() not in {"npx", "npx.cmd"}:
        return None
    for index, arg in enumerate(args):
        if arg.startswith("-"):
            continue
        entry = _node_entry(arg)
        if entry is None:
            return None
        return "node", [str(entry), *args[index + 1:]]
    return None


async def build_mcps() -> list[MCPClient]:
    """按 config/mcp_servers.json 创建并并发连接 MCP 客户端。

    支持格式：
    {"mcpServers": {"名称": {"type": "stdio", "command": "...", "args": [...]}}}
    {"mcpServers": {"名称": {"type": "http", "url": "...", "headers": {}}}}

    各服务器并行启动（stdin/stdout 进程互不影响），单个失败只跳过该服务器。
    """
    path = project_path("mcp_config")
    if not path.is_file():
        return []
    servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {})

    async def start(name: str, raw_cfg: dict) -> MCPClient:
        cfg = dict(raw_cfg)
        kind = cfg.pop("type", "stdio")
        disable_tools = cfg.pop("disable_tools", None)
        if kind == "http":
            config = HttpMCPConfig(**cfg)
            stateful = False
        else:
            command = str(cfg.pop("command", ""))
            args = [str(item) for item in cfg.pop("args", [])]
            direct = _direct_node(command, args)
            if direct is not None:
                command, args = direct
            config = StdioMCPConfig(command=command, args=args, **cfg)
            stateful = True
        client = MCPClient(
            name=name,
            is_stateful=stateful,
            mcp_config=config,
            disable_tools=disable_tools,
        )
        if stateful:
            await client.connect()
        return client

    results = await asyncio.gather(
        *(start(name, cfg) for name, cfg in servers.items()),
        return_exceptions=True,
    )
    clients: list[MCPClient] = []
    for name, result in zip(servers, results):
        if isinstance(result, BaseException):
            _mcp_logger().error(
                "MCP server %s failed to start", name, exc_info=result
            )
            continue
        clients.append(result)
    return clients


def load_state() -> tuple[AgentState, bool]:
    """读取本地会话状态，不存在则新建。"""
    path = project_path("session_file")
    if path.is_file():
        try:
            return AgentState.model_validate_json(path.read_text(encoding="utf-8")), True
        except Exception:  # 状态文件损坏时从头开始
            pass
    return AgentState(), False


def save_state(state: AgentState) -> None:
    path = project_path("session_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(), encoding="utf-8")
    tmp.replace(path)


async def build_agent(
    model,
    backend,
    mcps,
    gate: PermissionGate | None = None,
    ask: AskCallback | None = None,
    on_compacting: Callable[[bool], Awaitable[None]] | None = None,
    on_subagent_event: Callable[[str, dict], None] | None = None,
) -> tuple[Agent, Toolkit, bool]:
    state, restored = load_state()
    state.permission_context.mode = resolve_permission_mode(
        CONFIG.get("permission_mode", "accept")
    )
    gate = gate or PermissionGate()
    toolkit = build_toolkit(backend, mcps)
    # 并行子代理：同一轮多次调用由框架并发执行，角色由 prompt 定义
    await toolkit.add_tool(
        Subagent(
            model=model,
            backend=backend,
            gate=gate,
            toolkit=toolkit,
            on_event=on_subagent_event,
        )
    )
    # 选择题提问：由会话层把问题转成 UI 事件并等待用户回答
    await toolkit.add_tool(AskUser(ask=ask))
    workspace = workspace_path(create=True)

    system_prompt = (
        "你是 Alex，运行在用户电脑上的智能助手。\n"
        f"当前工作区：{workspace}（不存在时自动创建）。你也可以访问任意绝对路径。\n"
        "需要查看或修改文件、执行命令时，直接调用对应工具。\n"
        "输出表格时必须用标准 Markdown 管道表格（表头 + | --- | 分隔行）；"
        "禁止用 ┌ ─ │ 等字符手绘表格，终端会把连续框线合并成错位的长行。\n"
        "当需要用户在多个方案中选择、或缺少关键信息时，调用 AskUser 工具给出"
        "简短选项，而不是在正文里反复追问。\n"
        "交付最终文件时，文件必须已经写完并真实存在。在回答正文里把每个文件单独放一行，"
        "写成 [文件名](绝对路径)，界面会把这一行换成文件卡；不要把同一路径再用纯文本重复一遍。"
        "只给最终交付物，不要给中间产物、日志或源码加这种链接。\n"
        "长期偏好和规则写在 rules/ 目录，不要写进记忆文件。\n"
        f"日常任务流水自动记录在 {CONFIG['memory_dir']}/memory/YYYY-MM-DD.md，"
        f"合并后的长期任务记忆是 {CONFIG['memory_dir']}/MEMORY.md。\n"
        f"压缩时沉淀的长期经验在 {CONFIG['memory_dir']}/LESSONS.md，"
        "回答相关问题时先读它。\n"
        "当用户提到最近做过的事时，先读 MEMORY.md，再按需读最近的日期文件。\n"
    )
    rules = load_rules()
    if rules:
        system_prompt += f"\n<rules>\n{rules}\n</rules>\n"

    # 记忆流水：call 结束、压缩前、上下文溢出时 flush；后台定期合并
    middlewares = [ConfirmationMiddleware(gate)]
    if CONFIG.get("memory_dir"):
        middlewares.append(
            DailyMemoryMiddleware(
                workdir=str(ROOT),
                memory_dir=CONFIG["memory_dir"],
                consolidation_minutes=CONFIG.get(
                    "memory_consolidation_minutes", 30
                ),
                max_memory_chars=CONFIG.get("memory_max_chars", 8000),
                retention_days=CONFIG.get("memory_retention_days", 30),
                on_compacting=on_compacting,
            ),
        )

    agent = Agent(
        name="Alex",
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        state=state,
        middlewares=middlewares or None,
        context_config=ContextConfig(
            trigger_ratio=CONFIG["model"].get("context_trigger_ratio", 0.8),
        ),
        react_config=ReActConfig(max_iters=CONFIG["model"]["max_iters"]),
    )
    return agent, toolkit, restored


def build_model():
    """按 config/config.json 的 provider 构造模型，密钥从 .env 读取。"""
    load_dotenv(ROOT / ".env")
    cfg = CONFIG["model"]
    provider = cfg.get("provider", "deepseek").lower()
    key_env = cfg.get("api_key_env", "API_KEY")
    api_key = os.environ.get(key_env)
    context_size = int(cfg.get("context_size", 128000))
    if not api_key:
        raise SystemExit(f"Set {key_env} in .env before starting.")
    params = request_parameters(cfg)

    if provider == "deepseek":
        return ThinkingSafeDeepSeekModel(
            credential=DeepSeekCredential(
                api_key=api_key, base_url=cfg.get("base_url") or "https://api.deepseek.com"
            ),
            model=cfg["name"],
            parameters=DeepSeekChatModel.Parameters(**params),
            context_size=context_size,
        )
    if provider in {"openai", "openai-compatible", "custom"}:
        if provider != "openai" and not cfg.get("base_url"):
            raise SystemExit(f"Set base_url for provider {provider}.")
        return OpenAIChatModel(
            credential=OpenAICredential(
                api_key=api_key, base_url=cfg.get("base_url")
            ),
            model=cfg["name"],
            parameters=OpenAIChatModel.Parameters(**params),
            context_size=context_size,
            extra_body=cfg.get("extra_body") or None,
        )
    raise SystemExit(
        f"Unsupported provider: {provider} (use deepseek / openai / custom)"
    )
