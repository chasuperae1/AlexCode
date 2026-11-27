"""设置界面主屏：只用 Mixin 组装，具体分区实现在同包其他模块。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Markdown, Select, Static

from alex.config import LOCKED_SKILLS
from alex.settings.constants import MODE_OPTIONS, _mode_value, _short_tokens
from alex.settings.widgets import ModeSwitch, StatusDot, ThemeSwitch


@dataclass
class _TokenView:
    """Tokens 页所需的派生数值，避免各卡片重复换算。"""

    raw: dict[str, Any]
    available: bool
    total: int
    used: int
    trigger: float
    ratio: float
    limit: int
    remain: int
    messages: int
    tools: int
    session: dict[str, Any]


class SettingsLayoutMixin:
    """设置页的 compose 与布局构件。"""

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-panel"):
            yield from self._compose_header()
            with Horizontal(id="settings-main"):
                with Vertical(id="settings-workspace"):
                    yield from self._compose_nav()
                    with VerticalScroll(id="settings-content"):
                        yield from self._compose_section_header()
                        yield Static("", id="settings-body")
                        with Vertical(id="settings-memory"):
                            yield from self._compose_memory()
                        with Vertical(id="settings-skill-list"):
                            yield from self._compose_skills()
                        with Vertical(id="settings-rule-list"):
                            yield from self._compose_rules()
                        with Vertical(id="settings-mcp-list"):
                            yield from self._compose_mcp()
                        with Vertical(id="settings-token-list"):
                            yield from self._token_cards()
                        with Vertical(id="settings-preference"):
                            yield from self._compose_preference()
                    with Vertical(id="settings-doc"):
                        yield from self._compose_doc()
            yield from self._compose_actions()

    def _compose_header(self) -> ComposeResult:
        with Horizontal(id="settings-header"):
            yield Static("Settings", id="settings-title")

    def _compose_nav(self) -> ComposeResult:
        with Horizontal(id="settings-nav"):
            yield Static("CATEGORIES", id="settings-heading")
            for section_id, label, _body in self._sections:
                yield Static(
                    label,
                    id=f"settings-option-{section_id}",
                    classes="settings-option",
                )

    def _compose_section_header(self) -> ComposeResult:
        with Horizontal(id="settings-section-header"):
            with Vertical(id="settings-section-heading"):
                yield Static("", id="settings-section-title")
                yield Static("", id="settings-section-subtitle")
            with Horizontal(id="settings-badges"):
                yield Static(
                    "",
                    id="settings-badge-primary",
                    classes="settings-badge",
                )
                yield Static(
                    "",
                    id="settings-badge-secondary",
                    classes="settings-badge",
                )

    def _compose_memory(self) -> ComposeResult:
        with Horizontal(id="settings-memory-stats"):
            yield from self._memory_stats()
        yield from self._compose_memory_entries()
        yield from self._compose_memory_lessons()
        yield from self._compose_memory_logs()

    def _compose_memory_entries(self) -> ComposeResult:
        with Vertical(classes="settings-card", id="settings-memory-entries"):
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
                    yield from self._memory_entry_row(index, entry)

    def _compose_memory_lessons(self) -> ComposeResult:
        with Vertical(classes="settings-card", id="settings-memory-lessons"):
            with Horizontal(classes="settings-card-head"):
                yield Static("LESSONS", classes="settings-memory-label")
                yield Static(
                    self._lessons_label(),
                    id="settings-lessons-count",
                    classes="settings-memory-count",
                )
            lessons = (self._memory.get("lessons") or {}).get("items") or []
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
                    yield from self._memory_lesson_row(index, lesson)

    def _compose_memory_logs(self) -> ComposeResult:
        with Vertical(classes="settings-card", id="settings-memory-logs"):
            with Horizontal(classes="settings-card-head"):
                yield Static("DAILY LOGS", classes="settings-memory-label")
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

    def _compose_skills(self) -> ComposeResult:
        for index, (name, description, enabled) in enumerate(self._skills):
            with Horizontal(classes="settings-card", id=f"skill-card-{index}"):
                with Horizontal(classes="settings-item"):
                    yield from self._item_row(
                        "skill",
                        index,
                        name,
                        description,
                        enabled,
                        view=True,
                        locked=name in LOCKED_SKILLS,
                    )

    def _compose_rules(self) -> ComposeResult:
        for index, (name, description, enabled) in enumerate(self._rules):
            with Horizontal(classes="settings-card", id=f"rule-card-{index}"):
                with Horizontal(classes="settings-item"):
                    yield from self._item_row(
                        "rule", index, name, description, enabled, view=True
                    )

    def _compose_mcp(self) -> ComposeResult:
        for group_index, (server, tool_indices) in enumerate(self._mcp_groups):
            yield from self._compose_mcp_server(group_index, server, tool_indices)

    def _compose_mcp_server(
        self, group_index: int, server: str, tool_indices: list[int]
    ) -> ComposeResult:
        enabled_count = sum(self._mcp_tools[i][3] for i in tool_indices)
        kind = next(
            (
                server_type
                for name, server_type, _connected in self._mcp_servers
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
                with Vertical(classes="settings-card-head-text"):
                    yield Static(server, classes="settings-server-name")
                    yield Static(kind.upper(), classes="settings-pill")
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
                    _server, name, description, enabled = self._mcp_tools[index]
                    with Horizontal(
                        classes="settings-item settings-item-separated"
                    ):
                        yield from self._item_row(
                            "mcp", index, name, description, enabled
                        )

    def _compose_preference(self) -> ComposeResult:
        with Vertical(classes="settings-card", id="settings-pref-card"):
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
                with Horizontal(classes="settings-switch-wrap"):
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
        with Vertical(classes="settings-card", id="settings-notify-card"):
            with Horizontal(classes="settings-card-head"):
                yield Static(
                    "NOTIFICATION",
                    classes="settings-memory-label",
                )
                yield Static(
                    "desktop",
                    classes="settings-memory-count",
                )
            with Horizontal(classes="settings-pref-row"):
                with Vertical(classes="settings-item-text"):
                    yield Static(
                        "Reply chime",
                        classes="settings-item-name",
                    )
                    yield Static(
                        "",
                        id="settings-notify-desc",
                        classes="settings-item-description",
                    )
                with Horizontal(classes="settings-switch-wrap"):
                    yield ModeSwitch(
                        "on"
                        if getattr(self, "_notify_enabled", True)
                        else "off",
                        options=(("Off", "off"), ("On", "on")),
                        id="notify-switch",
                    )
            yield Static(
                "Tone and per-event switches live in "
                "config/config.json → notify. Windows only.",
                id="settings-notify-note",
                classes="settings-pref-note",
            )
        with Vertical(classes="settings-card", id="settings-theme-card"):
            with Horizontal(classes="settings-card-head"):
                yield Static(
                    "THEME",
                    classes="settings-memory-label",
                )
                yield Static(
                    "",
                    id="settings-theme-spinner",
                    classes="settings-theme-spinner",
                )
                yield Static(
                    "interface",
                    classes="settings-memory-count",
                )
            with Horizontal(classes="settings-pref-row"):
                with Vertical(classes="settings-item-text"):
                    yield Static(
                        "Colour theme",
                        classes="settings-item-name",
                    )
                    yield Static(
                        "Pick a palette; the whole app recolours "
                        "immediately. The same five as Ctrl+T cycles.",
                        id="settings-theme-desc",
                        classes="settings-item-description",
                    )
                with Horizontal(classes="settings-switch-wrap"):
                    yield ThemeSwitch(
                        self._theme,
                        id="theme-switch",
                    )
            yield Static(
                "Applies at once and is remembered in "
                "data/tui.json for the next launch.",
                id="settings-theme-note",
                classes="settings-pref-note",
            )

    def _compose_doc(self) -> ComposeResult:
        yield Static("", id="settings-doc-crumbs")
        with Horizontal(id="settings-doc-head"):
            yield Static("", id="settings-doc-name")
            yield Static("", id="settings-doc-pill")
            yield Static("", id="settings-doc-status")
            yield Static("✕", id="settings-doc-close")
        with VerticalScroll(id="settings-doc-scroll"):
            yield Markdown("", id="settings-doc-md")

    def _compose_actions(self) -> ComposeResult:
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

    def _token_metrics(self) -> _TokenView:
        tokens = self._tokens
        total = int(tokens.get("total") or 0)
        used = int(tokens.get("used") or 0)
        ratio = used / total if total else 0.0
        self._bar_ratio = ratio
        trigger = float(tokens.get("ratio") or 0.8)
        limit = int(total * trigger)
        return _TokenView(
            raw=tokens,
            available=bool(tokens.get("available")),
            total=total,
            used=used,
            trigger=trigger,
            ratio=ratio,
            limit=limit,
            remain=max(0, limit - used),
            messages=int(tokens.get("messages") or 0),
            tools=int(tokens.get("tools") or 0),
            session=tokens.get("session") or {},
        )

    def _token_cards(self) -> ComposeResult:
        view = self._token_metrics()
        yield from self._token_context_card(view)
        with Horizontal(id="settings-token-grid"):
            yield from self._token_where_card(view)
            yield from self._token_session_card(view)
        yield from self._token_runtime_card(view)

    def _token_context_card(self, view: _TokenView) -> ComposeResult:
        with Vertical(classes="settings-card settings-token-card"):
            yield Static("CONTEXT WINDOW", classes="settings-token-title")
            if not view.available:
                yield Static(
                    "Context stats unavailable.", classes="settings-token-note"
                )
                return
            with Horizontal(classes="settings-token-metric"):
                yield Static(_short_tokens(view.used), classes="settings-token-used")
                yield Static(
                    f"/ {_short_tokens(view.total)} tokens",
                    classes="settings-token-total",
                )
                yield Static(
                    f"{view.ratio:.0%} used", classes="settings-token-pill"
                )
            yield Static(
                self._bar(view.ratio, 44),
                id="settings-token-bar",
                classes="settings-token-bar",
            )
            with Horizontal(classes="settings-token-legend"):
                yield Static(
                    f"compact at {_short_tokens(view.limit)} "
                    f"({view.trigger:.0%})"
                )
                yield Static(
                    f"{_short_tokens(view.remain)} left before compact"
                )

    def _token_where_card(self, view: _TokenView) -> ComposeResult:
        with Vertical(classes="settings-card settings-token-card"):
            yield Static("WHERE IT GOES", classes="settings-token-title")
            if not view.available:
                yield Static("Unavailable.", classes="settings-token-note")
                return
            rows = (
                ("Messages", view.messages),
                ("Tools", view.tools),
                ("Free", max(0, view.total - view.used)),
            )
            for label, value in rows:
                with Horizontal(classes="settings-token-row"):
                    yield Static(label, classes="settings-token-row-label")
                    yield Static(
                        self._bar(
                            value / view.total if view.total else 0, 12
                        ),
                        classes="settings-token-row-bar",
                    )
                    yield Static(
                        _short_tokens(value),
                        classes="settings-token-row-value",
                    )

    def _token_session_card(self, view: _TokenView) -> ComposeResult:
        session = view.session
        replies = int(session.get("replies") or 0)
        sent = int(session.get("input") or 0)
        received = int(session.get("output") or 0)
        cached = int(session.get("cached") or 0)
        last = int(session.get("last") or 0)
        with Vertical(classes="settings-card settings-token-card"):
            yield Static("THIS SESSION", classes="settings-token-title")
            for label, value in (
                ("Replies", str(replies)),
                ("Input", _short_tokens(sent)),
                ("Output", _short_tokens(received)),
                ("Cached", _short_tokens(cached)),
            ):
                with Horizontal(classes="settings-token-row"):
                    yield Static(label, classes="settings-token-row-label")
                    yield Static(value, classes="settings-token-row-value")
            with Horizontal(classes="settings-token-legend"):
                average = sent / replies if replies else 0
                yield Static(f"avg {_short_tokens(average)} / reply")
                yield Static(f"last {_short_tokens(last)}")

    def _token_runtime_card(self, view: _TokenView) -> ComposeResult:
        tokens = view.raw
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
                    _short_tokens(view.total), classes="settings-token-kv-val"
                )
            with Horizontal(classes="settings-token-kv-line"):
                yield Static("Permission", classes="settings-token-kv-key")
                yield Static(
                    str(tokens.get("permission") or "?"),
                    classes="settings-token-kv-val",
                )
                yield Static("Compact trigger", classes="settings-token-kv-key")
                yield Static(
                    f"{view.trigger:.0%} · auto", classes="settings-token-kv-val"
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
