"""Agent 会话内核分包：只读描述数据：describe、记忆分页、设置分区。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from alex.config import (
    CONFIG,
    LOCKED_SKILLS,
    ROOT,
    lock_skill_files,
    project_path,
    reload_config,
    save_config,
    workspace_path,
)
from alex.core.images import build_user_message, display_text
from alex.core import reasoning
from alex.core.snapshots import Checkpoint, SnapshotError, SnapshotStore, now_label

from alex.session.common import (
    ASSISTANT,
    BOOT_STAGES,
    DEFAULT_WELCOME,
    Emit,
    GOAL_RESTATEMENT_PROMPT,
    GoalRestatement,
    READONLY_TOOLS,
    SessionError,
    USER,
    _format_tokens,
)


class InfoMixin:
    """只读描述数据：describe、记忆分页、设置分区。"""

    async def describe(self) -> dict[str, Any]:
        """设置页所需的技能 / 规则 / MCP / token 信息。"""
        await self._wait_ready()
        import frontmatter

        skills_dir = project_path("skills_dir")
        excluded_skills = set(CONFIG.get("excluded_skills", []))
        skills = []
        skill_details: dict[str, str] = {}
        if skills_dir.is_dir():
            for path in sorted(skills_dir.rglob("SKILL.md")):
                try:
                    metadata = frontmatter.loads(
                        path.read_text(encoding="utf-8-sig")
                    )
                    name = str(metadata.get("name") or path.parent.name)
                    description = " ".join(
                        str(metadata.get("description") or "").split()
                    )
                    if len(description) > 150:
                        description = (
                            description[:147].rsplit(" ", 1)[0] + "…"
                        )
                    skills.append(
                        (
                            name,
                            description,
                            name in LOCKED_SKILLS or name not in excluded_skills,
                        )
                    )
                    skill_details[name] = str(metadata.content or "")
                except (OSError, ValueError):
                    continue

        rules_dir = project_path("rules_dir")
        excluded_rules = set(CONFIG.get("excluded_rules", []))
        rules = []
        rule_details: dict[str, str] = {}
        if rules_dir.is_dir():
            for path in sorted(rules_dir.glob("*.md")):
                description = ""
                text = ""
                try:
                    text = path.read_text(encoding="utf-8-sig")
                    for line in text.splitlines():
                        stripped = line.strip()
                        if not stripped:
                            continue
                        description = stripped.lstrip("# ").strip()
                        break
                except OSError:
                    pass
                if len(description) > 150:
                    description = description[:147].rsplit(" ", 1)[0] + "…"
                rules.append(
                    (path.name, description, path.name not in excluded_rules)
                )
                rule_details[path.name] = text

        mcp_path = project_path("mcp_config")
        server_config: dict = {}
        if mcp_path.is_file():
            try:
                server_config = json.loads(
                    mcp_path.read_text(encoding="utf-8")
                ).get("mcpServers", {})
            except (OSError, ValueError):
                server_config = {}
        connected = {
            str(getattr(client, "name", ""))
            for client in self.mcps
            if client.is_connected
        }
        mcp_servers = [
            (name, str(cfg.get("type", "stdio")), name in connected)
            for name, cfg in server_config.items()
        ]
        mcp_tools = []
        tools_by_server: dict[str, list[str]] = {}

        async def _discover(client):
            disabled = set(
                server_config.get(client.name, {}).get("disable_tools", [])
            )
            items = []
            try:
                for tool in await client._discover_tools():
                    description = " ".join(str(tool.description or "").split())
                    items.append(
                        (client.name, tool.name, description, tool.name not in disabled)
                    )
                    tools_by_server.setdefault(client.name, []).append(tool.name)
            except Exception:
                pass
            return items

        results = await asyncio.gather(
            *(_discover(client) for client in self.mcps),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, list):
                mcp_tools.extend(result)
        self._mcp_tools_by_server = tools_by_server

        return {
            "skills": skills,
            "skill_details": skill_details,
            "rules": rules,
            "rule_details": rule_details,
            "mcp_servers": mcp_servers,
            "mcp_tools": mcp_tools,
            "sections": self._settings_sections(),
            "tokens": await self._token_snapshot(),
            "memory": self._memory_page(),
        }

    @staticmethod
    def _parse_memory_entries(text: str) -> list[dict[str, Any]]:
        """按 ``## 标题`` 切分 MEMORY.md，提取标题、日期、摘要与全文。"""
        blocks: list[tuple[str, list[str]]] = []
        heading: str | None = None
        body: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if heading is not None:
                    blocks.append((heading, body))
                heading = line[3:].strip()
                body = []
            elif heading is not None:
                body.append(line)
        if heading is not None:
            blocks.append((heading, body))

        entries: list[dict[str, Any]] = []
        for heading, lines in blocks:
            content = "\n".join(lines).strip()
            match = re.search(r"(\d{4}-\d{2}-\d{2})", heading)
            title = re.sub(
                r"[（(][^）)]*\d{4}-\d{2}-\d{2}[^）)]*[)）]\s*$", "", heading
            ).strip() or heading
            snippet = ""
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("|"):
                    continue
                candidate = stripped.lstrip("- ").replace("**", "").strip()
                if candidate:
                    snippet = candidate[:90] + ("…" if len(candidate) > 90 else "")
                    break
            full = f"## {heading}\n\n{content}".strip()
            entries.append(
                {
                    "title": title,
                    "date": match.group(1) if match else "—",
                    "size": len(full.encode("utf-8")),
                    "snippet": snippet,
                    "content": full,
                }
            )
        return entries

    @staticmethod
    def _parse_lessons(text: str) -> list[dict[str, Any]]:
        """按 ``## 日期 · 标题`` 切分 LESSONS.md，跳过 type/confidence 元数据行。"""
        blocks: list[tuple[str, list[str]]] = []
        heading: str | None = None
        body: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if heading is not None:
                    blocks.append((heading, body))
                heading = line[3:].strip()
                body = []
            elif heading is not None:
                body.append(line)
        if heading is not None:
            blocks.append((heading, body))

        items: list[dict[str, Any]] = []
        for heading, lines in blocks:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", heading)
            title = (
                re.sub(r"^\d{4}-\d{2}-\d{2}\s*[·\-]\s*", "", heading).strip()
                or heading
            )
            snippet = ""
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("- type:"):
                    continue
                candidate = stripped.lstrip("- ").replace("**", "").strip()
                if candidate:
                    snippet = candidate[:90] + ("…" if len(candidate) > 90 else "")
                    break
            content = f"## {heading}\n\n" + "\n".join(lines).strip()
            items.append(
                {
                    "title": title,
                    "date": match.group(1) if match else "—",
                    "size": len(content.encode("utf-8")),
                    "snippet": snippet,
                    "content": content.strip(),
                }
            )
        return items

    def _memory_page(self) -> dict[str, Any]:
        """设置页 Memory 分类的结构化数据：统计卡、长期记忆条目、每日日志。"""
        memory_dir = project_path("memory_dir")
        entries: list[dict[str, Any]] = []

        memory_file = memory_dir / "MEMORY.md"
        memory_exists = memory_file.is_file()
        if memory_exists:
            try:
                entries = self._parse_memory_entries(
                    memory_file.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                entries = []

        def _meta(path: Path) -> tuple[int, str]:
            try:
                stat = path.stat()
            except OSError:
                return 0, ""
            return stat.st_size, datetime.fromtimestamp(stat.st_mtime).strftime(
                "%m-%d %H:%M"
            )

        long_term_size, long_term_updated = (
            _meta(memory_file) if memory_exists else (0, "")
        )

        lessons_file = memory_dir / "LESSONS.md"
        lessons_exists = lessons_file.is_file()
        lessons_count = 0
        lessons_size = 0
        lessons_updated = ""
        lesson_items: list[dict[str, Any]] = []
        if lessons_exists:
            lessons_size, lessons_updated = _meta(lessons_file)
            try:
                text = lessons_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            lesson_items = self._parse_lessons(text)
            bullets = [
                line for line in text.splitlines() if line.startswith("- ")
            ]
            lessons_count = len(lesson_items) or len(bullets)

        logs: list[dict[str, Any]] = []
        daily_dir = memory_dir / "memory"
        if daily_dir.is_dir():
            for path in sorted(daily_dir.glob("*.md"), reverse=True):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                logs.append(
                    {
                        "name": path.name,
                        "date": path.stem,
                        "size": path.stat().st_size,
                        "count": sum(
                            1
                            for line in text.splitlines()
                            if line.startswith("## ")
                        ),
                        "content": text,
                    }
                )

        return {
            "path": str(memory_dir),
            "long_term": {
                "exists": memory_exists,
                "count": len(entries),
                "size": long_term_size,
                "updated": long_term_updated,
            },
            "lessons": {
                "exists": lessons_exists,
                "count": lessons_count,
                "size": lessons_size,
                "updated": lessons_updated,
                "items": lesson_items,
            },
            "daily": {
                "days": len(logs),
                "latest": logs[0]["date"] if logs else "",
                "latest_count": logs[0]["count"] if logs else 0,
            },
            "entries": entries,
            "logs": logs,
        }

    def _settings_sections(self) -> list[tuple[str, str, str]]:
        skills_dir = project_path("skills_dir")
        skills = (
            sorted(
                path.parent.relative_to(skills_dir).as_posix()
                for path in skills_dir.rglob("SKILL.md")
            )
            if skills_dir.is_dir()
            else []
        )
        rules: list[str] = []
        rules_dir = project_path("rules_dir")
        if rules_dir.is_dir():
            for path in sorted(rules_dir.glob("*.md")):
                title = ""
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("#"):
                            title = line.lstrip("# ").strip()
                            break
                except OSError:
                    pass
                rules.append(f"- {path.name}" + (f" — {title}" if title else ""))
        for name in CONFIG.get("extra_rules", []):
            if (Path.cwd() / name).is_file():
                rules.append(f"- {name} (extra)")

        mcp_path = project_path("mcp_config")
        servers: dict = {}
        if mcp_path.is_file():
            try:
                servers = json.loads(mcp_path.read_text(encoding="utf-8")).get(
                    "mcpServers", {}
                )
            except (OSError, ValueError):
                servers = {}
        connected = {str(getattr(client, "name", "")) for client in self.mcps}
        mcp_lines = [f"Connected: {len(self.mcps)} / {len(servers)}"]
        for name, cfg in servers.items():
            kind = str(cfg.get("type", "stdio"))
            suffix = " · connected" if name in connected else ""
            mcp_lines.append(f"- {name} ({kind}){suffix}")

        # 顺序固定为 Tokens / Rules / Skills / MCP / Memory / Preference。
        # Tokens / Memory / Preference 由设置页的卡片单独渲染，这里仅保留占位。
        return [
            ("tokens", "Tokens", ""),
            ("rules", "Rules", "\n".join(rules) or "No rules loaded."),
            (
                "skills",
                "Skills",
                "\n".join(f"- {name}" for name in skills) or "No skills loaded.",
            ),
            (
                "mcp",
                "MCP",
                "\n".join(mcp_lines) if servers else "No MCP servers configured.",
            ),
            ("memory", "Memory", ""),
            ("preference", "Preference", ""),
        ]

