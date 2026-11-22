"""设置界面主屏：只用 Mixin 组装，具体分区实现在同包其他模块。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Markdown, Select, Static

from alex.config import LOCKED_SKILLS

from alex.settings.constants import (
    MODE_OPTIONS,
    SECTION_META,
    _format_size,
    _mode_value,
    _short_tokens,
)
from alex.settings.memory import MemoryMixin
from alex.settings.sections import SectionsMixin
from alex.settings.style import SETTINGS_CSS
from alex.settings.widgets import MemoryRow, ModeSwitch, StatusDot




class SettingsLayoutMixin:
    """设置页的 compose 与布局构件。"""

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-panel"):
            with Horizontal(id="settings-header"):
                yield Static("Settings", id="settings-title")
            with Horizontal(id="settings-main"):
                with Vertical(id="settings-workspace"):
                    with Horizontal(id="settings-nav"):
                        yield Static("CATEGORIES", id="settings-heading")
                        for section_id, label, _body in self._sections:
                            yield Static(
                                label,
                                id=f"settings-option-{section_id}",
                                classes="settings-option",
                            )
                    with VerticalScroll(id="settings-content"):
                        with Horizontal(id="settings-section-header"):
                            with Vertical(id="settings-section-heading"):
                                yield Static("", id="settings-section-title")
                                yield Static("", id="settings-section-subtitle")
                            with Horizontal(id="settings-badges"):
                                yield Static(
                                    "", id="settings-badge-primary",
                                    classes="settings-badge",
                                )
                                yield Static(
                                    "", id="settings-badge-secondary",
                                    classes="settings-badge",
                                )
                        yield Static("", id="settings-body")
                        with Vertical(id="settings-memory"):
                            with Horizontal(id="settings-memory-stats"):
                                yield from self._memory_stats()
                            with Vertical(
                                classes="settings-card",
                                id="settings-memory-entries",
                            ):
                                with Horizontal(classes="settings-card-head"):
                                    yield Static(
                                        "MEMORY ENTRIES",
                                        classes="settings-memory-label",
                                    )
                                    yield Static(
                                        self._memory_entries_label(),
                                        id="settings-memory-entries-count",
                                        classes="settings-memory-count",
                                    )
                                entries = self._memory.get("entries") or []
                                if not entries:
                                    yield Static(
                                        "No memory entries yet.",
                                        classes="settings-memory-empty",
                                    )
                                with VerticalScroll(
                                    classes="settings-memory-list",
                                    id="settings-memory-entries-list",
                                ):
                                    for index, entry in enumerate(entries):
                                        yield from self._memory_entry_row(
                                            index, entry
                                        )
                            with Vertical(
                                classes="settings-card",
                                id="settings-memory-lessons",
                            ):
                                with Horizontal(classes="settings-card-head"):
                                    yield Static(
                                        "LESSONS",
                                        classes="settings-memory-label",
                                    )
                                    yield Static(
                                        self._lessons_label(),
                                        id="settings-lessons-count",
                                        classes="settings-memory-count",
                                    )
                                lessons = (
                                    self._memory.get("lessons") or {}
                                ).get("items") or []
                                if not lessons:
                                    yield Static(
                                        "No lessons yet.",
                                        classes="settings-memory-empty",
                                    )
                                with VerticalScroll(
                                    classes="settings-memory-list",
                                    id="settings-lessons-list",
                                ):
                                    for index, lesson in enumerate(lessons):
                                        yield from self._memory_lesson_row(
                                            index, lesson
                                        )
                            with Vertical(
                                classes="settings-card",
                                id="settings-memory-logs",
                            ):
                                with Horizontal(classes="settings-card-head"):
                                    yield Static(
                                        "DAILY LOGS",
                                        classes="settings-memory-label",
                                    )
                                    yield Static(
                                        self._memory_logs_label(),
                                        id="settings-memory-logs-count",
                                        classes="settings-memory-count",
                                    )
                                logs = self._memory.get("logs") or []
                                if not logs:
                                    yield Static(
                                        "No daily logs yet.",
                                        classes="settings-memory-empty",
                                    )
                                for index, log in enumerate(logs):
                                    yield from self._memory_log_row(index, log)
                        with Vertical(id="settings-skill-list"):
                            for index, (name, description, enabled) in enumerate(
                                self._skills
                            ):
                                with Horizontal(
                                    classes="settings-card",
                                    id=f"skill-card-{index}",
                                ):
                                    with Horizontal(classes="settings-item"):
                                        yield from self._item_row(
                                            "skill", index, name, description,
                                            enabled, view=True,
                                            locked=name in LOCKED_SKILLS,
                                        )
                        with Vertical(id="settings-rule-list"):
                            for index, (name, description, enabled) in enumerate(
                                self._rules
                            ):
                                with Horizontal(
                                    classes="settings-card",
                                    id=f"rule-card-{index}",
                                ):
                                    with Horizontal(classes="settings-item"):
                                        yield from self._item_row(
                                            "rule", index, name, description,
                                            enabled, view=True,
                                        )
                        with Vertical(id="settings-mcp-list"):
                            for group_index, (server, tool_indices) in enumerate(
                                self._mcp_groups
                            ):
                                enabled_count = sum(
                                    self._mcp_tools[i][3] for i in tool_indices
                                )
                                kind = next(
                                    (
                                        server_type
                                        for name, server_type, _connected
                                        in self._mcp_servers
                                        if name == server
                                    ),
                                    "stdio",
                                )
                                with Vertical(classes="settings-card"):
                                    with Horizontal(
                                        id=f"server-head-{group_index}",
                                        classes="settings-card-head",
                                    ):
                                        yield StatusDot(
                                            enabled_count > 0,
                                            id=f"server-dot-{group_index}",
                                        )
                                        yield Static(
                                            "▸",
                                            id=f"server-arrow-{group_index}",
                                            classes="settings-arrow",
                                        )
                                        with Vertical(
                                            classes="settings-card-head-text"
                                        ):
                                            yield Static(
                                                server,
                                                classes="settings-server-name",
                                            )
                                            yield Static(
                                                kind.upper(),
                                                classes="settings-pill",
                                            )
                                        yield Static(
                                            f"{enabled_count} / {len(tool_indices)} enabled",
                                            id=f"mcp-count-{group_index}",
                                            classes="settings-server-count",
                                        )
                                        yield Select(
                                            MODE_OPTIONS,
                                            value=_mode_value(enabled_count > 0),
                                            allow_blank=False,
                                            compact=True,
                                            id=f"server-mode-{group_index}",
                                            classes="settings-mode",
                                        )
                                    with Vertical(
                                        id=f"mcp-tools-{group_index}",
                                        classes="settings-tool-list",
                                    ):
                                        for index in tool_indices:
                                            _server, name, description, enabled = (
                                                self._mcp_tools[index]
                                            )
                                            with Horizontal(
                                                classes="settings-item settings-item-separated"
                                            ):
                                                yield from self._item_row(
                                                    "mcp", index, name, description, enabled
                                                )
                        with Vertical(id="settings-token-list"):
                            yield from self._token_cards()
                        with Vertical(id="settings-preference"):
                            with Vertical(
                                classes="settings-card",
                                id="settings-pref-card",
                            ):
                                with Horizontal(classes="settings-card-head"):
                                    yield Static(
                                        "DISPLAY MODE",
                                        classes="settings-memory-label",
                                    )
                                    yield Static(
                                        "chat area",
                                        classes="settings-memory-count",
                                    )
                                with Horizontal(classes="settings-pref-row"):
                                    with Vertical(classes="settings-item-text"):
                                        yield Static(
                                            "Process display",
                                            classes="settings-item-name",
                                        )
                                        yield Static(
                                            "",
                                            id="settings-pref-desc",
                                            classes="settings-item-description",
                                        )
                                    with Horizontal(
                                        classes="settings-switch-wrap"
                                    ):
                                        yield ModeSwitch(
                                            self._display_mode,
                                            id="display-mode-switch",
                                        )
                                yield Static(
                                    "Chat area only. Permissions, tool calls and "
                                    "verification are unaffected. Applies from the "
                                    "next turn.",
                                    id="settings-pref-note",
                                    classes="settings-pref-note",
                                )
                    with Vertical(id="settings-doc"):
                        yield Static("", id="settings-doc-crumbs")
                        with Horizontal(id="settings-doc-head"):
                            yield Static("", id="settings-doc-name")
                            yield Static("", id="settings-doc-pill")
                            yield Static("", id="settings-doc-status")
                            yield Static("✕", id="settings-doc-close")
                        with VerticalScroll(id="settings-doc-scroll"):
                            yield Markdown("", id="settings-doc-md")
            with Horizontal(id="settings-actions"):
                yield Static("", id="settings-hint")
                yield Static("Close", id="settings-close")

    def _bar(self, ratio: float, cells: int = 32) -> Text:
        """用 ASCII 字符画进度条；方块字符在部分终端是双宽，会撑破单行布局。"""
        ratio = max(0.0, min(1.0, ratio))
        filled = int(round(cells * ratio))
        theme = getattr(self.app, "current_theme", None)
        on = str(getattr(theme, "primary", "") or "bold")
        off = str(getattr(theme, "secondary", "") or "dim")
        bar = Text()
        bar.append("=" * filled, style=f"bold {on}")
        bar.append("-" * (cells - filled), style=off)
        return bar

    def _token_cards(self) -> ComposeResult:
        tokens = self._tokens
        available = bool(tokens.get("available"))
        total = int(tokens.get("total") or 0)
        used = int(tokens.get("used") or 0)
        trigger = float(tokens.get("ratio") or 0.8)
        ratio = used / total if total else 0.0
        self._bar_ratio = ratio
        limit = int(total * trigger)
        remain = max(0, limit - used)

        with Vertical(classes="settings-card settings-token-card"):
            yield Static("CONTEXT WINDOW", classes="settings-token-title")
            if not available:
                yield Static(
                    "Context stats unavailable.", classes="settings-token-note"
                )
            else:
                with Horizontal(classes="settings-token-metric"):
                    yield Static(_short_tokens(used), classes="settings-token-used")
                    yield Static(
                        f"/ {_short_tokens(total)} tokens",
                        classes="settings-token-total",
                    )
                    yield Static(
                        f"{ratio:.0%} used", classes="settings-token-pill"
                    )
                yield Static(
                    self._bar(ratio, 44),
                    id="settings-token-bar",
                    classes="settings-token-bar",
                )
                with Horizontal(classes="settings-token-legend"):
                    yield Static(f"compact at {_short_tokens(limit)} ({trigger:.0%})")
                    yield Static(f"{_short_tokens(remain)} left before compact")

        with Horizontal(id="settings-token-grid"):
            with Vertical(classes="settings-card settings-token-card"):
                yield Static("WHERE IT GOES", classes="settings-token-title")
                if not available:
                    yield Static("Unavailable.", classes="settings-token-note")
                else:
                    rows = (
                        ("Messages", int(tokens.get("messages") or 0)),
                        ("Tools", int(tokens.get("tools") or 0)),
                        ("Free", max(0, total - used)),
                    )
                    for label, value in rows:
                        with Horizontal(classes="settings-token-row"):
                            yield Static(
                                label, classes="settings-token-row-label"
                            )
                            yield Static(
                                self._bar(value / total if total else 0, 12),
                                classes="settings-token-row-bar",
                            )
                            yield Static(
                                _short_tokens(value),
                                classes="settings-token-row-value",
                            )

            with Vertical(classes="settings-card settings-token-card"):
                yield Static("THIS SESSION", classes="settings-token-title")
                session = tokens.get("session") or {}
                replies = int(session.get("replies") or 0)
                sent = int(session.get("input") or 0)
                received = int(session.get("output") or 0)
                cached = int(session.get("cached") or 0)
                last = int(session.get("last") or 0)
                for label, value in (
                    ("Replies", str(replies)),
                    ("Input", _short_tokens(sent)),
                    ("Output", _short_tokens(received)),
                    ("Cached", _short_tokens(cached)),
                ):
                    with Horizontal(classes="settings-token-row"):
                        yield Static(
                            label, classes="settings-token-row-label"
                        )
                        yield Static(
                            value, classes="settings-token-row-value"
                        )
                with Horizontal(classes="settings-token-legend"):
                    average = sent / replies if replies else 0
                    yield Static(f"avg {_short_tokens(average)} / reply")
                    yield Static(f"last {_short_tokens(last)}")

        with Vertical(classes="settings-card settings-token-card"):
            yield Static("RUNTIME", classes="settings-token-title")
            with Horizontal(classes="settings-token-kv-line"):
                yield Static("Model", classes="settings-token-kv-key")
                yield Static(
                    str(tokens.get("model") or "?"),
                    classes="settings-token-kv-val",
                )
                yield Static("Context limit", classes="settings-token-kv-key")
                yield Static(
                    _short_tokens(total), classes="settings-token-kv-val"
                )
            with Horizontal(classes="settings-token-kv-line"):
                yield Static("Permission", classes="settings-token-kv-key")
                yield Static(
                    str(tokens.get("permission") or "?"),
                    classes="settings-token-kv-val",
                )
                yield Static("Compact trigger", classes="settings-token-kv-key")
                yield Static(
                    f"{trigger:.0%} · auto", classes="settings-token-kv-val"
                )
            with Horizontal(classes="settings-token-kv-line"):
                yield Static("Workspace", classes="settings-token-kv-key")
                yield Static(
                    str(tokens.get("workspace") or "?"),
                    classes="settings-token-kv-val",
                )

    def _item_row(
        self,
        prefix: str,
        index: int,
        name: str,
        description: str,
        enabled: bool,
        *,
        view: bool = False,
        locked: bool = False,
    ) -> ComposeResult:
        """一条可切换条目：圆点开关 + 名称/说明 + 全屏符号 + Enabled/Excluded 下拉框。

        ``locked`` 用于内置只读技能：仍可查看，但开关不可操作。
        """
        yield StatusDot(enabled, id=f"{prefix}-dot-{index}", disabled=locked)
        with Vertical(classes="settings-item-text"):
            yield Static(name, classes="settings-item-name")
            yield Static(
                description or "No description.",
                classes="settings-item-description",
            )
        if view:
            yield Static("⛶", id=f"{prefix}-view-{index}", classes="settings-view")
        yield Select(
            MODE_OPTIONS,
            value=_mode_value(enabled),
            allow_blank=False,
            compact=True,
            id=f"{prefix}-mode-{index}",
            classes="settings-mode",
            disabled=locked,
        )
