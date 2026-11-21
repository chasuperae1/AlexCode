"""Settings screen: category nav on top, section content below."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Markdown, Select, Static

from .config import LOCKED_SKILLS

# 每个可管理分类的标题与说明；其余分类沿用纯文本内容
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


class StatusDot(Static):
    """可聚焦的圆点开关：启用为绿色，排除为灰色。"""

    can_focus = True

    BINDINGS = [
        Binding("space", "toggle_dot", "Toggle", show=False),
        Binding("enter", "toggle_dot", "Toggle", show=False),
    ]

    class Changed(Message):
        def __init__(self, dot: "StatusDot", value: bool) -> None:
            super().__init__()
            self.dot = dot
            self.value = value

    def __init__(
        self, value: bool, *, id: str | None = None, disabled: bool = False
    ) -> None:
        super().__init__("●", id=id, classes="settings-dot", disabled=disabled)
        self.value = value
        self.set_class(value, "-on")

    def set_value(self, value: bool) -> None:
        if value == self.value:
            return
        self.value = value
        self.set_class(value, "-on")

    def toggle(self) -> None:
        self.set_value(not self.value)
        self.post_message(self.Changed(self, self.value))

    def action_toggle_dot(self) -> None:
        self.toggle()

    def _on_click(self, event) -> None:
        event.stop()
        self.focus()
        self.toggle()


class MemoryRow(Horizontal):
    """可聚焦的记忆条目行：Enter、点击或 ⛶ 打开全屏阅读。"""

    can_focus = True

    BINDINGS = [Binding("enter", "open_row", "Open", show=False)]

    class Opened(Message):
        def __init__(self, row: "MemoryRow") -> None:
            super().__init__()
            self.row = row

    def action_open_row(self) -> None:
        self.post_message(self.Opened(self))

    def _on_click(self, event) -> None:
        event.stop()
        self.focus()
        self.post_message(self.Opened(self))


class SettingsScreen(ModalScreen[bool | None]):
    """设置页。

    ``sections`` 为 (id, 标题, 内容) 列表，纯文本内容用于 tokens；
    ``skills``、``rules`` 与 ``mcp_tools`` 提供可切换的条目，
    ``memory`` 提供记忆概览、条目与每日日志，``mcp_servers`` 提供服务器状态。
    关闭时若有改动返回 True，调用方据此重载 agent。
    """

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+c", "close", "Close", show=False),
        Binding("right", "next_section", "Next section", show=False),
        Binding("left", "previous_section", "Previous section", show=False),
        Binding("down", "focus_next_item", "Next item", priority=True, show=False),
        Binding("up", "focus_prev_item", "Previous item", priority=True, show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
        background: $background;
    }
    SettingsScreen #settings-panel {
        width: 100%;
        height: 100%;
        background: $background;
    }
    SettingsScreen #settings-header {
        height: 4;
        padding: 1 2 0 2;
        align-vertical: middle;
        border-bottom: solid $primary 25%;
        background: $background;
    }
    SettingsScreen #settings-title {
        width: 1fr;
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen #settings-main {
        height: 1fr;
    }
    SettingsScreen #settings-workspace {
        width: 1fr;
        height: 1fr;
    }
    SettingsScreen #settings-nav {
        width: 100%;
        height: 3;
        padding: 0 2;
        align-vertical: middle;
        border-bottom: solid $primary 20%;
        background: $background;
    }
    SettingsScreen #settings-heading {
        width: auto;
        height: 1;
        padding: 0 2 0 0;
        color: $text-muted;
        text-style: bold;
    }
    SettingsScreen .settings-option {
        width: auto;
        height: 1;
        padding: 0 2;
        color: $foreground;
    }
    SettingsScreen .settings-option:hover {
        color: $accent;
        text-style: bold;
    }
    SettingsScreen .settings-option-active {
        color: $foreground;
        background: $primary 35%;
        text-style: bold;
    }
    SettingsScreen #settings-content {
        width: 1fr;
        height: 1fr;
        padding: 2 4;
        background: $background;
        scrollbar-size-vertical: 0;
    }
    SettingsScreen #settings-content.-tokens {
        padding: 1 4 0 4;
    }
    SettingsScreen #settings-content.-tokens #settings-section-header {
        margin-bottom: 0;
    }
    SettingsScreen #settings-section-header {
        height: auto;
        margin-bottom: 1;
    }
    SettingsScreen #settings-section-heading {
        width: 1fr;
        height: auto;
    }
    SettingsScreen #settings-section-title {
        height: auto;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen #settings-section-subtitle {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen #settings-badges {
        width: auto;
        height: auto;
    }
    SettingsScreen .settings-badge {
        width: auto;
        height: 3;
        margin-left: 1;
        padding: 0 1;
        border: round $primary 35%;
        color: $text-muted;
    }
    SettingsScreen #settings-body {
        width: 1fr;
        height: auto;
        color: $foreground;
    }
    SettingsScreen #settings-memory {
        display: none;
        width: 1fr;
        height: auto;
    }
    SettingsScreen #settings-memory-stats {
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
        grid-gutter: 0 1;
        height: auto;
        margin-bottom: 1;
    }
    SettingsScreen .settings-memory-stat {
        width: 1fr;
        height: 100%;
        padding: 1 2;
        border: round $primary 22%;
        background: $background;
    }
    SettingsScreen .settings-memory-label {
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    SettingsScreen .settings-memory-value {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-memory-stat.-missing .settings-memory-value {
        color: $text-muted;
    }
    SettingsScreen .settings-memory-sub {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-count {
        dock: right;
        width: auto;
        height: 1;
        margin-left: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-row {
        height: auto;
        padding: 0 1;
        border-top: solid $primary 12%;
    }
    SettingsScreen .settings-memory-row:focus {
        background: $primary 20%;
    }
    SettingsScreen .settings-memory-list {
        height: auto;
        max-height: 15;
        scrollbar-size-vertical: 1;
    }
    SettingsScreen .settings-memory-text {
        width: 1fr;
        height: auto;
    }
    SettingsScreen .settings-memory-name {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-memory-snip {
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
    }
    SettingsScreen .settings-memory-meta {
        width: auto;
        height: auto;
        margin: 0 1;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-view {
        width: 3;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-memory-row:focus .settings-memory-view {
        color: $accent;
    }
    SettingsScreen .settings-memory-empty {
        height: auto;
        padding: 1 1;
        color: $text-muted;
    }
    SettingsScreen #settings-skill-list,
    SettingsScreen #settings-rule-list,
    SettingsScreen #settings-mcp-list,
    SettingsScreen #settings-token-list {
        display: none;
        width: 1fr;
        height: auto;
    }
    SettingsScreen .settings-card {
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        border: round $primary 22%;
        border-left: thick $primary;
        background: $background;
    }
    SettingsScreen .settings-token-card {
        height: auto;
        padding: 0 1;
        margin: 0;
    }
    SettingsScreen .settings-token-title {
        width: auto;
        height: 1;
        color: $text-muted;
        text-style: bold;
    }
    SettingsScreen .settings-token-note {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen .settings-token-metric {
        height: 1;
        align-vertical: middle;
    }
    SettingsScreen .settings-token-used {
        width: auto;
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-token-total {
        width: auto;
        height: 1;
        margin-left: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-token-pill {
        width: auto;
        height: 1;
        margin-left: 2;
        padding: 0 1;
        background: $primary 25%;
        color: $foreground;
    }
    SettingsScreen .settings-token-bar {
        width: 1fr;
        height: 1;
    }
    SettingsScreen .settings-token-legend {
        width: 1fr;
        height: 1;
    }
    SettingsScreen .settings-token-legend Static {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-token-legend Static:last-of-type {
        text-align: right;
    }
    SettingsScreen #settings-token-grid {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-gutter: 0 1;
        height: auto;
        margin: 1 0 0 0;
    }
    SettingsScreen #settings-token-grid .settings-token-card {
        margin: 0;
        height: 100%;
    }
    SettingsScreen .settings-token-row {
        width: 1fr;
        height: 1;
    }
    SettingsScreen .settings-token-row-label {
        width: 1fr;
        height: 1;
        color: $foreground;
    }
    SettingsScreen .settings-token-row-bar {
        width: 14;
        height: 1;
    }
    SettingsScreen .settings-token-row-value {
        width: 9;
        height: 1;
        text-align: right;
        color: $foreground;
    }
    SettingsScreen .settings-token-kv-line {
        width: 1fr;
        height: 1;
        margin-bottom: 1;
    }
    SettingsScreen .settings-token-kv-line:last-of-type {
        margin-bottom: 0;
    }
    SettingsScreen .settings-token-kv-key {
        width: 16;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-token-kv-val {
        width: 1fr;
        height: 1;
        color: $foreground;
    }
    SettingsScreen .settings-card-head {
        height: auto;
        padding: 1 1 0 1;
    }
    SettingsScreen .settings-card-head:hover {
        background: $primary 10%;
    }
    SettingsScreen .settings-card-head-text {
        width: 1fr;
        height: auto;
    }
    SettingsScreen .settings-server-name {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-pill {
        width: auto;
        height: 1;
        padding: 0 1;
        background: $primary 25%;
        color: $text-muted;
    }
    SettingsScreen .settings-server-count {
        width: auto;
        height: 1;
        margin-right: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-arrow {
        width: 2;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen .settings-dot {
        width: 2;
        height: 1;
        color: $text-muted;
        background: $background;
    }
    SettingsScreen .settings-dot.-on {
        color: green;
    }
    SettingsScreen .settings-dot:focus {
        background: $primary 30%;
    }
    SettingsScreen .settings-mode {
        width: 11;
        height: 1;
        margin-left: 1;
        background: $background;
    }
    SettingsScreen .settings-item {
        height: auto;
        padding: 1 1;
    }
    SettingsScreen .settings-item-separated {
        border-top: solid $primary 15%;
    }
    SettingsScreen .settings-item-text {
        width: 1fr;
        height: auto;
    }
    SettingsScreen .settings-item-name {
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen .settings-item-description {
        height: auto;
        color: $text-muted;
    }
    SettingsScreen .settings-tool-list {
        display: none;
        height: auto;
    }
    SettingsScreen .settings-tool-list.-open {
        display: block;
    }
    SettingsScreen .settings-view {
        width: 3;
        height: 1;
        color: $text-muted;
        background: $background;
    }
    SettingsScreen .settings-view:hover {
        color: $accent;
    }
    SettingsScreen #settings-doc {
        display: none;
        width: 1fr;
        height: 1fr;
        padding: 2 4 1 4;
        background: $background;
    }
    SettingsScreen #settings-doc-crumbs {
        height: 1;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-head {
        height: auto;
        padding: 1 0;
        border-bottom: solid $primary 18%;
    }
    SettingsScreen #settings-doc-name {
        width: auto;
        height: 1;
        color: $foreground;
        text-style: bold;
    }
    SettingsScreen #settings-doc-pill {
        width: auto;
        height: 1;
        margin-left: 1;
        padding: 0 1;
        background: $primary 25%;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-status {
        width: 1fr;
        height: 1;
        margin-left: 1;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-close {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    SettingsScreen #settings-doc-close:hover {
        color: $accent;
        text-style: bold;
    }
    SettingsScreen #settings-doc-scroll {
        height: 1fr;
        background: $background;
    }
    SettingsScreen #settings-doc-md {
        width: 1fr;
        height: auto;
        margin: 1 0 1 0;
        background: $background;
    }
    SettingsScreen #settings-actions {
        height: 3;
        padding: 0 2;
        align-vertical: middle;
        border-top: solid $primary 20%;
        background: $background;
    }
    SettingsScreen #settings-hint {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    SettingsScreen #settings-close {
        width: auto;
        height: 1;
        padding: 0 2;
        color: $primary;
        text-style: bold;
    }
    SettingsScreen #settings-close:hover {
        color: $accent;
        text-style: underline;
    }
    """

    def __init__(
        self,
        sections: list[tuple[str, str, str]],
        *,
        skills: list[tuple[str, str, bool]] | None = None,
        rules: list[tuple[str, str, bool]] | None = None,
        skill_details: dict[str, str] | None = None,
        rule_details: dict[str, str] | None = None,
        mcp_servers: list[tuple[str, str, bool]] | None = None,
        mcp_tools: list[tuple[str, str, str, bool]] | None = None,
        tokens: dict | None = None,
        memory: dict | None = None,
        on_visibility_change=None,
    ) -> None:
        super().__init__()
        self._sections = sections or [("empty", "Settings", "No settings.")]
        self._bodies = {item[0]: item[2] for item in self._sections}
        self._skills = list(skills or [])
        self._rules = list(rules or [])
        self._skill_details = dict(skill_details or {})
        self._rule_details = dict(rule_details or {})
        self._mcp_servers = list(mcp_servers or [])
        self._mcp_tools = list(mcp_tools or [])
        self._tokens = dict(tokens or {})
        self._memory = dict(memory or {})
        self._bar_ratio = 0.0
        groups: dict[str, list[int]] = {}
        for index, (server, _name, _description, _enabled) in enumerate(
            self._mcp_tools
        ):
            groups.setdefault(server, []).append(index)
        self._mcp_groups = list(groups.items())
        self._tool_to_group = {
            tool_index: group_index
            for group_index, (_server, indices) in enumerate(self._mcp_groups)
            for tool_index in indices
        }
        self._on_visibility_change = on_visibility_change
        self._active_index = 0
        self._dirty = False
        self._changed_kinds: set[str] = set()
        self._detail_open: tuple[str, int] | None = None
        # 记录每个下拉框最后设定的值：Select 的 Changed 消息可能滞后，
        # 只把与记录不同的消息当成用户操作，避免程序化赋值造成回环。
        self._select_values: dict[str, str] = {}

    # ---------------------------------------------------------------- 布局
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

    # ---------------------------------------------------------------- Tokens
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


    # ---------------------------------------------------------------- Memory
    def _memory_stats(self) -> ComposeResult:
        long_term = self._memory.get("long_term") or {}
        lessons = self._memory.get("lessons") or {}
        daily = self._memory.get("daily") or {}

        if long_term.get("exists"):
            lt_value = f"{int(long_term.get('count') or 0)} entries"
            lt_sub = f"MEMORY.md · {_format_size(long_term.get('size'))}"
            if long_term.get("updated"):
                lt_sub += f" · updated {long_term['updated']}"
        else:
            lt_value = "Empty"
            lt_sub = "MEMORY.md not found"

        if lessons.get("exists"):
            ls_value = f"{int(lessons.get('count') or 0)} lessons"
            ls_sub = f"LESSONS.md · {_format_size(lessons.get('size'))}"
            if lessons.get("updated"):
                ls_sub += f" · updated {lessons['updated']}"
        else:
            ls_value = "Empty"
            ls_sub = "LESSONS.md not created"

        days = int(daily.get("days") or 0)
        if days:
            d_value = f"{days} days"
            d_sub = (
                f"latest {daily.get('latest') or '—'} · "
                f"{int(daily.get('latest_count') or 0)} entries"
            )
        else:
            d_value = "Empty"
            d_sub = "No daily logs yet"

        stats = (
            ("LONG-TERM MEMORY", lt_value, lt_sub, not long_term.get("exists")),
            ("LESSONS", ls_value, ls_sub, not lessons.get("exists")),
            ("DAILY LOGS", d_value, d_sub, not days),
        )
        for label, value, sub, missing in stats:
            classes = "settings-memory-stat"
            if missing:
                classes += " -missing"
            with Vertical(classes=classes):
                yield Static(label, classes="settings-memory-label")
                yield Static(value, classes="settings-memory-value")
                yield Static(sub, classes="settings-memory-sub")

    def _memory_entry_row(self, index: int, entry: dict) -> ComposeResult:
        with MemoryRow(id=f"memory-entry-{index}", classes="settings-memory-row"):
            with Vertical(classes="settings-memory-text"):
                yield Static(
                    str(entry.get("title") or "Untitled"),
                    classes="settings-memory-name",
                )
                yield Static(
                    str(entry.get("snippet") or "—"),
                    classes="settings-memory-snip",
                )
            yield Static(
                f"{entry.get('date') or '—'} · {_format_size(entry.get('size'))}",
                classes="settings-memory-meta",
            )
            yield Static("⛶", classes="settings-memory-view")

    def _memory_log_row(self, index: int, log: dict) -> ComposeResult:
        with MemoryRow(id=f"memory-log-{index}", classes="settings-memory-row"):
            with Vertical(classes="settings-memory-text"):
                yield Static(
                    str(log.get("date") or log.get("name") or "Log"),
                    classes="settings-memory-name",
                )
                yield Static(
                    f"{int(log.get('count') or 0)} entries",
                    classes="settings-memory-snip",
                )
            yield Static(
                _format_size(log.get("size")),
                classes="settings-memory-meta",
            )
            yield Static("⛶", classes="settings-memory-view")

    def _memory_entries_label(self) -> str:
        total = len(self._memory.get("entries") or [])
        return f"{total} entries"

    def _memory_lesson_row(self, index: int, lesson: dict) -> ComposeResult:
        with MemoryRow(id=f"memory-lesson-{index}", classes="settings-memory-row"):
            with Vertical(classes="settings-memory-text"):
                yield Static(
                    str(lesson.get("title") or "Untitled"),
                    classes="settings-memory-name",
                )
                yield Static(
                    str(lesson.get("snippet") or "—"),
                    classes="settings-memory-snip",
                )
            yield Static(
                f"{lesson.get('date') or '—'} · {_format_size(lesson.get('size'))}",
                classes="settings-memory-meta",
            )
            yield Static("⛶", classes="settings-memory-view")

    def _lessons_label(self) -> str:
        lessons = self._memory.get("lessons") or {}
        total = len(lessons.get("items") or []) or int(lessons.get("count") or 0)
        return f"LESSONS.md · {total} lessons"

    def _memory_logs_label(self) -> str:
        return f"memory/ · {len(self._memory.get('logs') or [])} files"

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

    # ---------------------------------------------------------------- 状态
    def on_mount(self) -> None:
        for select in self.query(Select):
            self._select_values[select.id or ""] = str(select.value)
        self._set_active(self._sections[0][0])
        self._show(self._sections[0][0])

    def on_resize(self) -> None:
        self._refresh_token_bar()

    def _refresh_token_bar(self) -> None:
        """按卡片实际宽度重画上下文进度条。"""
        if not self._tokens.get("available"):
            return
        try:
            bar = self.query_one("#settings-token-bar", Static)
        except Exception:
            return
        width = bar.size.width
        if width > 0:
            bar.update(self._bar(self._bar_ratio, width))

    def _set_active(self, section_id: str) -> None:
        for key, _label, _body in self._sections:
            self.query_one(f"#settings-option-{key}", Static).set_class(
                key == section_id, "settings-option-active"
            )

    def _move_section(self, step: int) -> None:
        self._active_index = (self._active_index + step) % len(self._sections)
        section_id = self._sections[self._active_index][0]
        self._set_active(section_id)
        self._show(section_id)

    def action_next_section(self) -> None:
        self._move_section(1)

    def action_previous_section(self) -> None:
        self._move_section(-1)

    def action_focus_next_item(self) -> None:
        self._move_focus(1)

    def action_focus_prev_item(self) -> None:
        self._move_focus(-1)

    def _page_scroll(self, pages: int) -> None:
        selector = (
            "#settings-doc-scroll" if self._detail_open is not None
            else "#settings-content"
        )
        scroll = self.query_one(selector, VerticalScroll)
        if pages > 0:
            scroll.scroll_page_down(animate=False)
        else:
            scroll.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        self._page_scroll(1)

    def action_page_up(self) -> None:
        self._page_scroll(-1)

    def _move_focus(self, step: int) -> None:
        if self._detail_open is not None:
            scroll = self.query_one("#settings-doc-scroll", VerticalScroll)
            if step > 0:
                scroll.scroll_down(animate=False)
            else:
                scroll.scroll_up(animate=False)
            return
        if self._current_section() == "memory":
            rows = [row for row in self.query(MemoryRow) if self._is_visible(row)]
            if rows:
                focused = self.app.focused
                if focused in rows:
                    index = (rows.index(focused) + step) % len(rows)
                else:
                    index = 0 if step > 0 else len(rows) - 1
                rows[index].focus()
                rows[index].scroll_visible(animate=False)
                return
        dots = self._dots()
        content = self.query_one("#settings-content", VerticalScroll)
        if not dots:
            if step > 0:
                content.scroll_down(animate=False)
            else:
                content.scroll_up(animate=False)
            return
        focused = self.app.focused
        if focused in dots:
            index = (dots.index(focused) + step) % len(dots)
        else:
            index = 0 if step > 0 else len(dots) - 1
        dots[index].focus()
        dots[index].scroll_visible(animate=False)

    def _current_section(self) -> str:
        return self._sections[self._active_index][0]

    def _dots(self) -> list[StatusDot]:
        """当前分类下可见的圆点开关。"""
        container_id = {
            "skills": "#settings-skill-list",
            "rules": "#settings-rule-list",
            "mcp": "#settings-mcp-list",
        }
        section_id = self._current_section()
        if section_id not in container_id:
            return []
        container = self.query_one(container_id[section_id])
        return [dot for dot in container.query(StatusDot) if self._is_visible(dot)]

    def _is_visible(self, widget) -> bool:
        node = widget
        while node is not None and node is not self:
            if not node.display:
                return False
            node = node.parent
        return True

    def _show(self, section_id: str) -> None:
        self._detail_open = None
        self.query_one("#settings-doc").display = False
        self.query_one("#settings-content", VerticalScroll).display = True
        self._active_index = next(
            (
                index
                for index, (key, _label, _body) in enumerate(self._sections)
                if key == section_id
            ),
            0,
        )
        label = next(
            (label for key, label, _body in self._sections if key == section_id),
            "Settings",
        )
        title, subtitle = SECTION_META.get(section_id, (label, ""))
        self.query_one("#settings-section-title", Static).update(title)
        self.query_one("#settings-section-subtitle", Static).update(subtitle)
        body = self.query_one("#settings-body", Static)
        body.update(self._bodies.get(section_id, ""))
        body.display = section_id not in {
            "skills", "rules", "mcp", "tokens", "memory"
        }
        self.query_one("#settings-memory").display = section_id == "memory"
        self.query_one("#settings-skill-list").display = section_id == "skills"
        self.query_one("#settings-rule-list").display = section_id == "rules"
        self.query_one("#settings-mcp-list").display = section_id == "mcp"
        self.query_one("#settings-token-list").display = section_id == "tokens"
        content = self.query_one("#settings-content", VerticalScroll)
        content.set_class(section_id == "tokens", "-tokens")
        if section_id == "tokens":
            self.call_after_refresh(self._refresh_token_bar)
        self.query_one("#settings-hint", Static).update(self._hint(section_id))
        self._update_badges(section_id)
        content.scroll_home(animate=False)

    def _hint(self, section_id: str) -> str:
        if section_id == "skills":
            return (
                "↑/↓ select · Space toggle · ⛶ or click card to read · "
                "←/→ category · Esc close"
            )
        if section_id == "rules":
            return (
                "↑/↓ select · Space toggle · ⛶ or click card to read · "
                "←/→ category · Esc close"
            )
        if section_id == "mcp":
            return (
                "↑/↓ select · Space toggle · click a server to expand · "
                "←/→ category · Esc close"
            )
        if section_id == "memory":
            return (
                "↑/↓ select · Enter or ⛶ read · ←/→ category · Esc close"
            )
        if section_id == "tokens":
            return "↑/↓ scroll · ←/→ category · Esc close"
        return "←/→ switch category · Esc close"

    def _update_badges(self, section_id: str) -> None:
        primary = self.query_one("#settings-badge-primary", Static)
        secondary = self.query_one("#settings-badge-secondary", Static)
        badges = self.query_one("#settings-badges")
        if section_id == "skills":
            enabled = sum(1 for _name, _desc, on in self._skills if on)
            primary.update(
                Text.assemble(
                    ("● ", "green"),
                    (f"{len(self._skills)} skills loaded", "dim"),
                )
            )
            secondary.update(f"{enabled} / {len(self._skills)} enabled")
            badges.display = True
        elif section_id == "rules":
            enabled = sum(1 for _name, _desc, on in self._rules if on)
            primary.update(
                Text.assemble(
                    ("● ", "green"),
                    (
                        f"{len(self._rules)} rule"
                        f"{'s' if len(self._rules) != 1 else ''} loaded",
                        "dim",
                    ),
                )
            )
            secondary.update(f"{enabled} / {len(self._rules)} enabled")
            badges.display = True
        elif section_id == "memory":
            long_term = self._memory.get("long_term") or {}
            lessons = self._memory.get("lessons") or {}
            daily = self._memory.get("daily") or {}
            sources = 1 if long_term.get("exists") else 0
            sources += 1 if lessons.get("exists") else 0
            sources += 1 if int(daily.get("days") or 0) else 0
            primary.update(
                Text.assemble(
                    ("● ", "green"),
                    (f"{sources} sources", "dim"),
                )
            )
            secondary.update(f"{int(long_term.get('count') or 0)} entries")
            badges.display = True
        elif section_id == "mcp":
            connected = sum(1 for _name, _kind, on in self._mcp_servers if on)
            enabled = sum(1 for _s, _n, _d, on in self._mcp_tools if on)
            primary.update(
                Text.assemble(
                    ("● ", "green"),
                    (
                        f"{connected} server{'s' if connected != 1 else ''} connected",
                        "dim",
                    ),
                )
            )
            secondary.update(f"{enabled} / {len(self._mcp_tools)} enabled")
            badges.display = True
        elif section_id == "tokens":
            primary.update(
                Text.assemble(
                    ("● ", "green"),
                    (
                        "Snapshot"
                        if self._tokens.get("available")
                        else "Unavailable",
                        "dim",
                    ),
                )
            )
            secondary.update(str(self._tokens.get("model") or "—"))
            badges.display = True
        else:
            badges.display = False

    # ---------------------------------------------------------------- 交互
    def on_status_dot_changed(self, event: StatusDot.Changed) -> None:
        widget_id = event.dot.id or ""
        if widget_id.startswith("skill-dot-"):
            index = int(widget_id.removeprefix("skill-dot-"))
            self._set_skill(index, event.value)
        elif widget_id.startswith("rule-dot-"):
            index = int(widget_id.removeprefix("rule-dot-"))
            self._set_rule(index, event.value)
        elif widget_id.startswith("server-dot-"):
            self._set_server(int(widget_id.removeprefix("server-dot-")), event.value)
        elif widget_id.startswith("mcp-dot-"):
            self._set_tool(int(widget_id.removeprefix("mcp-dot-")), event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        selector = event.select.id or ""
        value = str(event.value)
        if event.value == Select.BLANK or self._select_values.get(selector) == value:
            return
        self._select_values[selector] = value
        enabled = value == "enabled"
        if selector.startswith("skill-mode-"):
            self._set_skill(int(selector.removeprefix("skill-mode-")), enabled)
        elif selector.startswith("rule-mode-"):
            self._set_rule(int(selector.removeprefix("rule-mode-")), enabled)
        elif selector.startswith("server-mode-"):
            self._set_server(int(selector.removeprefix("server-mode-")), enabled)
        elif selector.startswith("mcp-mode-"):
            self._set_tool(int(selector.removeprefix("mcp-mode-")), enabled)

    def _set_skill(self, index: int, enabled: bool) -> None:
        name, description, current = self._skills[index]
        if name in LOCKED_SKILLS or current == enabled:
            return
        self._skills[index] = (name, description, enabled)
        self.query_one(f"#skill-dot-{index}", StatusDot).set_value(enabled)
        self._sync_select(f"#skill-mode-{index}", enabled)
        self._update_badges(self._current_section())
        self._notify_change("skill", name, enabled)

    def _set_rule(self, index: int, enabled: bool) -> None:
        name, description, current = self._rules[index]
        if current == enabled:
            return
        self._rules[index] = (name, description, enabled)
        self.query_one(f"#rule-dot-{index}", StatusDot).set_value(enabled)
        self._sync_select(f"#rule-mode-{index}", enabled)
        self._update_badges(self._current_section())
        self._notify_change("rule", name, enabled)

    def _set_tool(self, index: int, enabled: bool, *, notify: bool = True) -> None:
        server, name, description, current = self._mcp_tools[index]
        if current == enabled:
            return
        self._mcp_tools[index] = (server, name, description, enabled)
        self.query_one(f"#mcp-dot-{index}", StatusDot).set_value(enabled)
        self._sync_select(f"#mcp-mode-{index}", enabled)
        self._update_group(index)
        self._update_badges(self._current_section())
        if notify:
            self._notify_change("mcp", f"{server}/{name}", enabled)

    def _set_server(self, group_index: int, enabled: bool) -> None:
        server, tool_indices = self._mcp_groups[group_index]
        for index in tool_indices:
            self._set_tool(index, enabled, notify=False)
        if tool_indices:
            self._update_group(tool_indices[0])
        self._notify_change("server", server, enabled)

    def _sync_select(self, selector: str, enabled: bool) -> None:
        select = self.query_one(selector, Select)
        value = _mode_value(enabled)
        if select.value != value:
            select.value = value
        self._select_values[select.id or ""] = value

    def _update_group(self, tool_index: int) -> None:
        group_index = self._tool_to_group[tool_index]
        _server, tool_indices = self._mcp_groups[group_index]
        enabled = sum(self._mcp_tools[i][3] for i in tool_indices)
        self.query_one(f"#mcp-count-{group_index}", Static).update(
            f"{enabled} / {len(tool_indices)} enabled"
        )
        any_enabled = enabled > 0
        self.query_one(f"#server-dot-{group_index}", StatusDot).set_value(any_enabled)
        self._sync_select(f"#server-mode-{group_index}", any_enabled)

    def _notify_change(self, kind: str, item: str, visible: bool) -> None:
        self._dirty = True
        self._changed_kinds.add(kind)
        if self._on_visibility_change is not None:
            self._on_visibility_change(kind, item, visible)

    @property
    def changed_kinds(self) -> set[str]:
        """本次打开的设置里改动过的类别（skill / rule / mcp / server）。"""
        return set(self._changed_kinds)

    def _toggle_server(self, group_index: int) -> None:
        _server, tool_indices = self._mcp_groups[group_index]
        any_enabled = any(self._mcp_tools[i][3] for i in tool_indices)
        self._set_server(group_index, not any_enabled)

    def _toggle_tool_list(self, group_index: int) -> None:
        tools = self.query_one(f"#mcp-tools-{group_index}")
        opened = not tools.has_class("-open")
        tools.set_class(opened, "-open")
        self.query_one(f"#server-arrow-{group_index}", Static).update(
            "▾" if opened else "▸"
        )

    def _group_of_widget(self, widget) -> int | None:
        node = widget
        while node is not None and node is not self:
            widget_id = getattr(node, "id", None) or ""
            if widget_id.startswith("server-head-"):
                return int(widget_id.removeprefix("server-head-"))
            node = node.parent
        return None

    def _inside_control(self, widget) -> bool:
        """点击落在开关或下拉框上时，交给控件自己处理。"""
        node = widget
        while node is not None and node is not self:
            if isinstance(node, (Select, StatusDot)):
                return True
            node = node.parent
        return False

    def _toggle_detail(self, prefix: str, index: int) -> None:
        if self._detail_open == (prefix, index):
            self._close_detail()
        else:
            self._open_detail(prefix, index)

    def _open_detail(self, prefix: str, index: int) -> None:
        """进入全屏文档视图，展示技能/规则的完整 Markdown 内容。"""
        items = self._skills if prefix == "skill" else self._rules
        details = self._skill_details if prefix == "skill" else self._rule_details
        if not 0 <= index < len(items):
            return
        name, _description, enabled = items[index]
        section = "Skills" if prefix == "skill" else "Rules"
        pill = "SKILL.md" if prefix == "skill" else "RULE.md"
        content = (details.get(name) or "").strip() or "_No content._"
        status = Text.assemble(
            ("● ", "green" if enabled else "dim"),
            ("Enabled" if enabled else "Excluded", "dim"),
        )
        self._detail_open = (prefix, index)
        self._show_doc(section, name, pill, status, content)

    def _open_memory_entry(self, index: int) -> None:
        entries = self._memory.get("entries") or []
        if not 0 <= index < len(entries):
            return
        entry = entries[index]
        name = str(entry.get("title") or "Untitled")
        status = Text(
            f"{entry.get('date') or '—'} · {_format_size(entry.get('size'))}",
            style="dim",
        )
        self._detail_open = ("memory", index)
        self._show_doc(
            "Memory", name, "MEMORY.md", status, str(entry.get("content") or "")
        )

    def _open_memory_lesson(self, index: int) -> None:
        items = (self._memory.get("lessons") or {}).get("items") or []
        if not 0 <= index < len(items):
            return
        lesson = items[index]
        name = str(lesson.get("title") or "Lesson")
        status = Text(
            f"{lesson.get('date') or '—'} · {_format_size(lesson.get('size'))}",
            style="dim",
        )
        self._detail_open = ("lesson", index)
        self._show_doc(
            "Memory", name, "LESSONS.md", status, str(lesson.get("content") or "")
        )

    def _open_memory_log(self, index: int) -> None:
        logs = self._memory.get("logs") or []
        if not 0 <= index < len(logs):
            return
        log = logs[index]
        name = str(log.get("date") or log.get("name") or "Daily log")
        status = Text(
            f"{int(log.get('count') or 0)} entries · {_format_size(log.get('size'))}",
            style="dim",
        )
        self._detail_open = ("log", index)
        self._show_doc(
            "Memory",
            name,
            str(log.get("name") or "log.md"),
            status,
            str(log.get("content") or "") or "_No content._",
        )

    def _show_doc(
        self,
        section: str,
        name: str,
        pill: str,
        status: Text,
        content: str,
    ) -> None:
        """填充并打开全屏文档视图。"""
        self.set_focus(None)
        self.query_one("#settings-doc-crumbs", Static).update(
            Text.assemble(
                (f"{section}  /  ", "dim"),
                (name, "bold"),
            )
        )
        self.query_one("#settings-doc-name", Static).update(name)
        self.query_one("#settings-doc-pill", Static).update(pill)
        self.query_one("#settings-doc-status", Static).update(status)
        self.query_one("#settings-doc-md", Markdown).update(content)
        self.query_one("#settings-content", VerticalScroll).display = False
        self.query_one("#settings-doc").display = True
        self.query_one("#settings-hint", Static).update(
            "↑/↓ scroll · PgUp/PgDn page · Esc back · ←/→ category · Esc×2 close"
        )
        self.query_one("#settings-doc-scroll", VerticalScroll).scroll_home(
            animate=False
        )

    def on_memory_row_opened(self, event: MemoryRow.Opened) -> None:
        widget_id = event.row.id or ""
        if widget_id.startswith("memory-entry-"):
            self._open_memory_entry(
                int(widget_id.removeprefix("memory-entry-"))
            )
        elif widget_id.startswith("memory-log-"):
            self._open_memory_log(int(widget_id.removeprefix("memory-log-")))
        elif widget_id.startswith("memory-lesson-"):
            self._open_memory_lesson(
                int(widget_id.removeprefix("memory-lesson-"))
            )

    def _close_detail(self) -> None:
        self.query_one("#settings-doc").display = False
        self.query_one("#settings-content", VerticalScroll).display = True
        self._detail_open = None
        self.query_one("#settings-hint", Static).update(
            self._hint(self._current_section())
        )

    def _card_of_widget(self, widget) -> tuple[str, int] | None:
        """点击技能/规则卡片主体时返回 (prefix, index)。"""
        node = widget
        while node is not None and node is not self:
            widget_id = getattr(node, "id", None) or ""
            if widget_id.startswith("settings-doc-"):
                return None
            for prefix in ("skill", "rule"):
                if widget_id.startswith(f"{prefix}-card-"):
                    return prefix, int(widget_id.removeprefix(f"{prefix}-card-"))
            node = node.parent
        return None

    def on_click(self, event) -> None:
        widget_id = getattr(event.widget, "id", None) or ""
        if widget_id == "settings-close":
            self.dismiss(True if self._dirty else None)
            return
        if widget_id == "settings-doc-close":
            self._close_detail()
            return
        if widget_id.startswith("settings-option-"):
            section_id = widget_id.removeprefix("settings-option-")
            self._set_active(section_id)
            self._show(section_id)
            return
        if self._inside_control(event.widget):
            return
        view = self._card_of_widget(event.widget)
        if view is not None:
            self._toggle_detail(*view)
            return
        group_index = self._group_of_widget(event.widget)
        if group_index is not None:
            self._toggle_tool_list(group_index)

    def action_close(self) -> None:
        if self._detail_open is not None:
            self._close_detail()
            return
        self.dismiss(True if self._dirty else None)
