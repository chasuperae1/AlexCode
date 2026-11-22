"""设置页的数据快照：一次 describe 的结果，直接交给 SettingsScreen 渲染。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SettingsData:
    """设置页所需的全部只读数据，避免构造器长出十几个参数。"""

    sections: list[tuple[str, str, str]] = field(default_factory=list)
    skills: list[tuple[str, str, bool]] = field(default_factory=list)
    rules: list[tuple[str, str, bool]] = field(default_factory=list)
    skill_details: dict[str, str] = field(default_factory=dict)
    rule_details: dict[str, str] = field(default_factory=dict)
    mcp_servers: list[tuple[str, str, bool]] = field(default_factory=list)
    mcp_tools: list[tuple[str, str, str, bool]] = field(default_factory=list)
    tokens: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    display_mode: str = "developer"

    @classmethod
    def from_payload(
        cls, data: dict[str, Any], *, display_mode: str = "developer"
    ) -> "SettingsData":
        """把 ``describe()`` 的原始 dict 转成带类型的快照。"""
        return cls(
            sections=[tuple(item) for item in data.get("sections", [])],
            skills=[tuple(item) for item in data.get("skills", [])],
            rules=[tuple(item) for item in data.get("rules", [])],
            skill_details=dict(data.get("skill_details") or {}),
            rule_details=dict(data.get("rule_details") or {}),
            mcp_servers=[tuple(item) for item in data.get("mcp_servers", [])],
            mcp_tools=[tuple(item) for item in data.get("mcp_tools", [])],
            tokens=dict(data.get("tokens") or {}),
            memory=dict(data.get("memory") or {}),
            display_mode=display_mode,
        )
