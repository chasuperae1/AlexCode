"""设置界面共享的常量与小工具。"""

from __future__ import annotations

SECTION_META = {
    "tokens": (
        "Tokens",
        "Context window, usage and runtime limits",
    ),
    "memory": (
        "Memory",
        "Long-term memory, distilled lessons and daily logs. Click an entry to read it.",
    ),
    "skills": (
        "Skills",
        "Manage the skills available to the agent. Excluded skills are not offered to it.",
    ),
    "rules": (
        "Rules",
        "Manage the rule files injected into the system prompt. Excluded rules are not loaded.",
    ),
    "mcp": (
        "MCP Tools",
        "Manage tools per server. Excluded tools are not offered to the agent.",
    ),
    "preference": (
        "Preference",
        "Only the chat area: Developer shows every step, Concise folds a turn into one summary.",
    ),
}

MODE_OPTIONS = [("Enabled", "enabled"), ("Excluded", "excluded")]


def _mode_value(enabled: bool) -> str:
    return "enabled" if enabled else "excluded"


def _short_tokens(value: int | float) -> str:
    """128000 -> 128k, 102400 -> 102.4k."""
    number = float(value or 0)
    if number < 1000:
        return str(int(round(number)))
    thousands = number / 1000
    if abs(thousands - round(thousands)) < 0.05:
        return f"{thousands:.0f}k"
    return f"{thousands:.1f}k"


def _format_size(size: int | float) -> str:
    """字节数转 KB / MB。"""
    value = float(size or 0)
    if value >= 1024 * 1024:
        return f"{value / 1048576:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{int(value)} B"

