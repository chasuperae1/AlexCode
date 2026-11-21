"""权限级别与交互确认。

级别：
- ``accept``（默认）：文件修改和危险命令需要用户确认，其余自动放行
- ``bypass``：全部自动放行
- ``explore``：只读，任何修改都被拒绝

复用 AgentScope 的 ``PermissionMode`` / ``PermissionEngine`` / 中间件扩展点，
不修改框架源码。确认通过 ``on_check_permission`` 中间件完成，这样主代理和
子代理可以共用同一套确认逻辑。
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Awaitable, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.permission import (
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
)
from agentscope.tool import ToolBase

from .config import CONFIG

MODE_NAMES = ("accept", "bypass", "explore")

MODE_DESCRIPTIONS = {
    "accept": "File changes and dangerous commands need approval",
    "bypass": "Everything runs without approval",
    "explore": "Read-only; modifications are denied",
}

_MODE_MAP = {
    "accept": PermissionMode.DEFAULT,
    "default": PermissionMode.DEFAULT,
    "bypass": PermissionMode.BYPASS,
    "explore": PermissionMode.EXPLORE,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "dont_ask": PermissionMode.DONT_ASK,
}

# 默认危险命令（正则，忽略大小写）。可在 config.json 的
# ``dangerous_commands`` 中覆盖。
DEFAULT_DANGEROUS_PATTERNS = [
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\bshutdown\b",
    r"\bbcdedit\b",
    r"\breg\s+(?:add|delete|import|load|restore|save|unload)\b",
    r"\bdel\b",
    r"\berase\b",
    r"\brd\b",
    r"\brmdir\b",
    r"\bmove\b",
    r"\bren(?:ame)?\b",
    r"\bcopy\b",
    r"\bxcopy\b",
    r"\brobocopy\b",
    r"\bmkdir\b",
    r"\bmklink\b",
    r"\battrib\b",
    r"\bicacls\b",
    r"\btakeown\b",
    r"\bcipher\s+/w\b",
    r"\bnet\s+(?:user|localgroup)\b",
    r"\bsc\s+delete\b",
    r"\bschtasks\b.*?/delete\b",
    r"\btaskkill\b",
    r"\bwmic\b",
    r"\bgit\s+push\b.*?(?:--force\b|-f\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b.*?\s-\w*f",
    r"(?<![0-9&])>>?(?!&)",
]

_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')


class ConfirmChoice(str, Enum):
    """用户在确认框里的选择。"""

    ALLOW = "allow"
    ALWAYS = "always"
    DENY = "deny"


ConfirmCallback = Callable[
    [str, ToolBase, dict[str, Any], PermissionDecision],
    Awaitable[ConfirmChoice],
]


def resolve_permission_mode(value: str | None) -> PermissionMode:
    """把配置里的级别名映射到 AgentScope 的权限模式。"""
    return _MODE_MAP.get(str(value or "").strip().lower(), PermissionMode.DEFAULT)


def dangerous_patterns() -> list[str]:
    """危险命令正则：配置优先，未配置时使用内置清单。"""
    configured = CONFIG.get("dangerous_commands")
    if isinstance(configured, list):
        return [str(pattern) for pattern in configured]
    return DEFAULT_DANGEROUS_PATTERNS


def is_dangerous_command(command: str) -> str | None:
    """返回命中的危险正则；安全命令返回 None。"""
    # 去掉引号里的内容，避免提交信息之类的文本误触发
    cleaned = _QUOTED.sub(" ", command or "")
    for pattern in dangerous_patterns():
        try:
            if re.search(pattern, cleaned, re.IGNORECASE):
                return pattern
        except re.error:
            continue
    return None


def permission_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
    """把工具输入压缩成一行可读的确认信息。"""
    for key in ("command", "file_path", "path", "query", "description", "prompt"):
        value = tool_input.get(key)
        if value:
            text = str(value)
            if key in {"prompt", "description"}:
                text = text[:600]
            return f"{key}: {text}"
    try:
        return json.dumps(tool_input, ensure_ascii=False)[:600]
    except (TypeError, ValueError):
        return str(tool_input)[:600]


class PermissionGate:
    """把权限检查结果转成允许/拒绝，并记住本次会话的“总是允许”。"""

    def __init__(self, confirm: ConfirmCallback | None = None) -> None:
        self._confirm = confirm
        self._session_rules: list[PermissionRule] = []

    async def decide(
        self,
        agent_name: str,
        tool: ToolBase,
        tool_input: dict[str, Any],
        decision: PermissionDecision,
    ) -> PermissionDecision:
        # 本次会话中用户选择过“总是允许”的规则
        for rule in self._session_rules:
            if rule.tool_name != tool.name:
                continue
            try:
                matched = await tool.match_rule(rule.rule_content, tool_input)
            except Exception:
                matched = False
            if matched:
                return PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message=f"Allowed by session rule for {tool.name}",
                    decision_reason="Session permission rule",
                )

        if self._confirm is None:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"No interactive confirmation available for {tool.name}",
                decision_reason="Permission confirmation unavailable",
            )

        choice = await self._confirm(agent_name, tool, tool_input, decision)
        if choice == ConfirmChoice.DENY:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"Permission denied by user for {tool.name}",
                decision_reason="User denied the operation",
            )
        if choice == ConfirmChoice.ALWAYS and not decision.bypass_immune:
            for rule in decision.suggested_rules or []:
                if rule.behavior == PermissionBehavior.ALLOW:
                    self._session_rules.append(rule)
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"Permission granted by user for {tool.name}",
            decision_reason="User approved the operation",
        )


class ConfirmationMiddleware(MiddlewareBase):
    """把 ASK/PASSTHROUGH 交给 PermissionGate 处理。"""

    def __init__(self, gate: PermissionGate) -> None:
        super().__init__()
        self._gate = gate

    async def on_check_permission(
        self,
        agent,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[PermissionDecision]],
    ) -> PermissionDecision:
        decision = await next_handler(**input_kwargs)
        if decision.behavior not in (
            PermissionBehavior.ASK,
            PermissionBehavior.PASSTHROUGH,
        ):
            return decision
        return await self._gate.decide(
            agent.name,
            input_kwargs["tool"],
            input_kwargs["tool_input"],
            decision,
        )


async def deny_confirmation(
    agent_name: str,
    tool: ToolBase,
    tool_input: dict[str, Any],
    decision: PermissionDecision,
) -> ConfirmChoice:
    """没有交互界面时的兜底：安全拒绝。"""
    return ConfirmChoice.DENY
