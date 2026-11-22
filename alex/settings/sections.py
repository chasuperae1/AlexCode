"""设置界面分包：分类内容：技能/规则/工具开关、徽标刷新、详情与文档弹层。"""

from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Select, Static

from alex.config import LOCKED_SKILLS
from alex.settings.constants import _mode_value
from alex.settings.widgets import ModeSwitch, StatusDot


class SectionsMixin:
    """分类内容：技能/规则/工具开关、徽标刷新、详情与文档弹层。"""

    _BADGE_SECTIONS: dict[str, str] = {
        "skills": "_badge_skills",
        "rules": "_badge_rules",
        "memory": "_badge_memory",
        "mcp": "_badge_mcp",
        "tokens": "_badge_tokens",
        "preference": "_badge_preference",
    }

    def _update_badges(self, section_id: str) -> None:
        """按当前分区刷新顶部徽标；没有徽标的分区直接隐藏。"""
        primary = self.query_one("#settings-badge-primary", Static)
        secondary = self.query_one("#settings-badge-secondary", Static)
        badges = self.query_one("#settings-badges")
        builder = getattr(self, self._BADGE_SECTIONS.get(section_id, ""), None)
        if builder is None:
            badges.display = False
            return
        builder(primary, secondary)
        badges.display = True

    def _badge_skills(self, primary: Static, secondary: Static) -> None:
        enabled = sum(1 for _name, _desc, on in self._skills if on)
        primary.update(
            Text.assemble(
                ("● ", "green"),
                (f"{len(self._skills)} skills loaded", "dim"),
            )
        )
        secondary.update(f"{enabled} / {len(self._skills)} enabled")

    def _badge_rules(self, primary: Static, secondary: Static) -> None:
        total = len(self._rules)
        enabled = sum(1 for _name, _desc, on in self._rules if on)
        noun = "rule" if total == 1 else "rules"
        primary.update(
            Text.assemble(("● ", "green"), (f"{total} {noun} loaded", "dim"))
        )
        secondary.update(f"{enabled} / {total} enabled")

    def _badge_memory(self, primary: Static, secondary: Static) -> None:
        long_term = self._memory.get("long_term") or {}
        lessons = self._memory.get("lessons") or {}
        daily = self._memory.get("daily") or {}
        sources = 1 if long_term.get("exists") else 0
        sources += 1 if lessons.get("exists") else 0
        sources += 1 if int(daily.get("days") or 0) else 0
        primary.update(
            Text.assemble(("● ", "green"), (f"{sources} sources", "dim"))
        )
        secondary.update(f"{int(long_term.get('count') or 0)} entries")

    def _badge_mcp(self, primary: Static, secondary: Static) -> None:
        connected = sum(1 for _name, _kind, on in self._mcp_servers if on)
        enabled = sum(1 for _s, _n, _d, on in self._mcp_tools if on)
        noun = "server" if connected == 1 else "servers"
        primary.update(
            Text.assemble(
                ("● ", "green"),
                (f"{connected} {noun} connected", "dim"),
            )
        )
        secondary.update(f"{enabled} / {len(self._mcp_tools)} enabled")

    def _badge_tokens(self, primary: Static, secondary: Static) -> None:
        label = "Snapshot" if self._tokens.get("available") else "Unavailable"
        primary.update(Text.assemble(("● ", "green"), (label, "dim")))
        secondary.update(str(self._tokens.get("model") or "—"))

    def _badge_preference(self, primary: Static, secondary: Static) -> None:
        mode = "Concise" if self._display_mode == "concise" else "Developer"
        primary.update(Text.assemble(("● ", "green"), (mode, "dim")))
        secondary.update("next turn")

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
            if isinstance(node, (Select, StatusDot, ModeSwitch)):
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

