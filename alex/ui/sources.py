"""Rules / skills 来源状态：只统计本次会话读取了什么，不把正文塞进上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from alex.config import CONFIG, LOCKED_SKILLS, project_path


@dataclass(frozen=True)
class SourceFile:
    name: str
    size: int
    path: Path


@dataclass
class SourceState:
    rules: list[SourceFile] = field(default_factory=list)
    skills: dict[str, SourceFile] = field(default_factory=dict)
    read_skills: dict[str, float] = field(default_factory=dict)

    @property
    def rules_size(self) -> int:
        return sum(item.size for item in self.rules)

    @property
    def skill_names(self) -> list[str]:
        return list(self.read_skills)

    def skill_size(self, name: str) -> int:
        item = self.skills.get(name)
        return item.size if item is not None else 0

    def mark_skill(self, name: str, elapsed: float = 0.0) -> SourceFile | None:
        name = (name or "").strip()
        if not name:
            return None
        item = self.skills.get(name)
        self.read_skills[name] = max(0.0, float(elapsed))
        return item


def format_bytes(size: int) -> str:
    size = max(0, int(size))
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def load_sources() -> SourceState:
    """按当前配置读取已启用的 rules 与 skills 元信息。"""
    excluded_rules = set(CONFIG.get("excluded_rules", []))
    excluded_skills = set(CONFIG.get("excluded_skills", [])) - set(LOCKED_SKILLS)
    state = SourceState()

    rules_dir = project_path("rules_dir")
    if rules_dir.is_dir():
        for path in sorted(rules_dir.glob("*.md")):
            if path.name in excluded_rules:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            state.rules.append(SourceFile(path.name, size, path))

    skills_dir = project_path("skills_dir")
    if skills_dir.is_dir():
        for path in sorted(skills_dir.rglob("SKILL.md")):
            name = path.parent.name
            if name in excluded_skills or name in state.skills:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            state.skills[name] = SourceFile(name, size, path)

    return state
