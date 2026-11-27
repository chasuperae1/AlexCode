"""设置界面主屏：只用 Mixin 组装，具体分区实现在同包其他模块。"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Select, Static

from alex.config import CONFIG
from alex.settings.constants import SECTION_META
from alex.settings.data import SettingsData
from alex.settings.memory import MemoryMixin
from alex.settings.sections import SectionsMixin
from alex.settings.style import SETTINGS_CSS
from alex.settings.widgets import (
    MemoryRow,
    ModeSwitch,
    StatusDot,
    ThemeSwitch,
)
from alex.sound import set_enabled
from alex.ui.themes import DEFAULT_THEME, THEMES

from alex.settings.layout import SettingsLayoutMixin


class SettingsScreen(SettingsLayoutMixin, SectionsMixin, MemoryMixin, ModalScreen):
    """设置弹层：顶部分类导航，下方分类内容。"""

    DEFAULT_CSS = SETTINGS_CSS

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

    def __init__(
        self,
        data: SettingsData,
        *,
        on_visibility_change=None,
        on_display_mode_change=None,
        theme: str = DEFAULT_THEME,
    ) -> None:
        super().__init__()
        self._sections = data.sections or [("empty", "Settings", "No settings.")]
        self._bodies = {item[0]: item[2] for item in self._sections}
        self._skills = list(data.skills)
        self._rules = list(data.rules)
        self._skill_details = dict(data.skill_details)
        self._rule_details = dict(data.rule_details)
        self._mcp_servers = list(data.mcp_servers)
        self._mcp_tools = list(data.mcp_tools)
        self._tokens = dict(data.tokens)
        self._memory = dict(data.memory)
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
        self._on_display_mode_change = on_display_mode_change
        self._display_mode = (
            "concise" if str(data.display_mode) == "concise" else "developer"
        )
        notify = CONFIG.get("notify") or {}
        self._notify_enabled = bool(notify.get("enabled", True))
        self._theme = str(theme) if str(theme) in THEMES else DEFAULT_THEME
        self._active_index = 0
        self._dirty = False
        self._changed_kinds: set[str] = set()
        self._detail_open: tuple[str, int] | None = None
        # 记录每个下拉框最后设定的值：Select 的 Changed 消息可能滞后，
        # 只把与记录不同的消息当成用户操作，避免程序化赋值造成回环。
        self._select_values: dict[str, str] = {}
        # 主题套用推到下一帧做，期间在卡片头上转一个小加载指示
        self._pending_theme = ""
        self._theme_apply_timer = None
        self._theme_applying = False
        self._theme_spinner_frame = 0
        self._theme_spinner_timer = None


    def on_mount(self) -> None:
        for select in self.query(Select):
            self._select_values[select.id or ""] = str(select.value)
        self._refresh_display_mode()
        self._refresh_notify_switch()
        self._refresh_theme_switch()
        self._set_active(self._sections[0][0])
        self._show(self._sections[0][0])
        # 主题可能在别处被换掉（Ctrl+T 仍可用），换完要重刷这一页开关的配色
        self.app.theme_changed_signal.subscribe(self, self._on_app_theme_changed)

    def on_unmount(self) -> None:
        self._flush_theme()
        self._end_theme_apply()
        self.app.theme_changed_signal.unsubscribe(self)

    def _on_app_theme_changed(self, theme) -> None:
        """主题真正生效之后再重刷：此刻 ``current_theme`` 已是新配色。"""
        name = str(getattr(theme, "name", "") or "")
        if name in THEMES:
            self._theme = name
        self._refresh_display_mode()
        self._refresh_notify_switch()
        self._refresh_theme_switch()
        self._end_theme_apply()

    def _refresh_display_mode(self) -> None:
        """同步显示模式开关与说明文字。

        ``set_value`` 在值没变时也会重刷配色——换主题后必须走一遍，
        否则高亮块还留着上一个主题的颜色。
        """
        concise = self._display_mode == "concise"
        self.query_one("#display-mode-switch", ModeSwitch).set_value(
            self._display_mode
        )
        self.query_one("#settings-pref-desc", Static).update(
            "Concise: a whole turn folded into one expandable summary."
            if concise
            else "Developer: every thinking step, tool call and diff."
        )

    def _set_display_mode(self, mode: str) -> None:
        mode = "concise" if str(mode) == "concise" else "developer"
        switch = self.query_one("#display-mode-switch", ModeSwitch)
        if switch.value != mode:
            switch.set_value(mode)
        if mode == self._display_mode:
            self._refresh_display_mode()
            return
        self._display_mode = mode
        self._dirty = True
        self._changed_kinds.add("preference")
        self._refresh_display_mode()
        self._update_badges(self._current_section())
        if self._on_display_mode_change is not None:
            self._on_display_mode_change(mode)

    def on_mode_switch_changed(self, event: ModeSwitch.Changed) -> None:
        switch_id = event.switch.id or ""
        if switch_id == "display-mode-switch":
            self._set_display_mode(event.value)
        elif switch_id == "notify-switch":
            self._set_notify_enabled(event.value == "on")

    def on_theme_switch_changed(self, event: ThemeSwitch.Changed) -> None:
        self._set_theme(event.value)

    def _set_theme(self, name: str) -> None:
        """切主题交给 AlexApp：写盘、重绘和权限拦截都在那边做。

        套用主题要重刷整套 CSS，同步执行会连这一帧的点击反馈一起卡住，
        所以先让开关亮到点中的那段并转起加载指示，再在下一帧真正套用。
        成功时由 ``theme_changed_signal`` 回调统一收尾；被拒（有未处理的
        权限卡）时主题没变，这里把开关拨回实际主题。
        """
        self._pending_theme = name
        self._begin_theme_apply()
        if self._theme_apply_timer is not None:
            self._theme_apply_timer.stop()
        self._theme_apply_timer = self.set_timer(0.02, self._flush_theme)

    def _flush_theme(self) -> None:
        """真正套用待办主题；关页或定时器到点都会走这里。"""
        self._theme_apply_timer = None
        name = self._pending_theme
        self._pending_theme = ""
        if not name:
            return
        before = str(getattr(self.app, "theme", self._theme))
        apply_theme = getattr(self.app, "_set_theme", None)
        if apply_theme is not None:
            apply_theme(name)
        if str(getattr(self.app, "theme", before)) == before:
            if self.is_mounted:
                self._refresh_theme_switch()
            self._end_theme_apply()

    def _begin_theme_apply(self) -> None:
        self._theme_applying = True
        self._theme_spinner_frame = 0
        self._render_theme_spinner()
        if self._theme_spinner_timer is None:
            self._theme_spinner_timer = self.set_interval(
                0.12, self._tick_theme_spinner
            )

    def _tick_theme_spinner(self) -> None:
        self._theme_spinner_frame += 1
        self._render_theme_spinner()

    def _render_theme_spinner(self) -> None:
        try:
            spinner = self.query_one("#settings-theme-spinner", Static)
        except Exception:  # 还没挂载 / 已卸载
            return
        if not self._theme_applying:
            spinner.update("")
            return
        frames = getattr(self.app, "_spinner_frames", None) or (
            "◐",
            "◓",
            "◑",
            "◒",
        )
        frame = frames[self._theme_spinner_frame % len(frames)]
        theme = self.app.current_theme
        muted = str(getattr(theme, "secondary", None) or "#8a929b")
        spinner.update(Text(f"{frame} applying", style=muted))

    def _end_theme_apply(self) -> None:
        self._theme_applying = False
        if self._theme_spinner_timer is not None:
            self._theme_spinner_timer.stop()
            self._theme_spinner_timer = None
        self._render_theme_spinner()

    def _refresh_theme_switch(self) -> None:
        """让主题开关与 ``self._theme`` 一致；配色由控件自己按当前主题画。"""
        self.query_one("#theme-switch", ThemeSwitch).set_value(self._theme)

    def _set_notify_enabled(self, enabled: bool) -> None:
        """铃声开关：界面立即跟随，同时写盘，让正在跑的这轮就生效。"""
        if enabled == self._notify_enabled:
            self._refresh_notify_switch()
            return
        self._notify_enabled = enabled
        self._refresh_notify_switch()
        try:
            set_enabled(enabled)
        except OSError as exc:
            self.app.notify(
                f"Could not save chime setting: {exc}",
                severity="error",
                timeout=4,
            )

    def _refresh_notify_switch(self) -> None:
        """按开关状态改写铃声说明；配色每次都要重刷，换主题才跟得上。"""
        enabled = self._notify_enabled
        want = "on" if enabled else "off"
        self.query_one("#notify-switch", ModeSwitch).set_value(want)
        self.query_one("#settings-notify-desc", Static).update(
            "A short tone plays when a reply finishes, or when the "
            "agent waits for approval or an answer."
            if enabled
            else "Silent: no tone when a reply finishes or the agent needs you."
        )

    @property
    def display_mode(self) -> str:
        return self._display_mode

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
        if self._current_section() == "preference":
            items = [
                self.query_one("#display-mode-switch", ModeSwitch),
                self.query_one("#notify-switch", ModeSwitch),
                self.query_one("#theme-switch", ThemeSwitch),
            ]
            focused = self.app.focused
            if focused in items:
                index = (items.index(focused) + step) % len(items)
            else:
                index = 0 if step > 0 else len(items) - 1
            items[index].focus()
            items[index].scroll_visible(animate=False)
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
            "skills", "rules", "mcp", "tokens", "memory", "preference"
        }
        self.query_one("#settings-memory").display = section_id == "memory"
        self.query_one("#settings-skill-list").display = section_id == "skills"
        self.query_one("#settings-rule-list").display = section_id == "rules"
        self.query_one("#settings-mcp-list").display = section_id == "mcp"
        self.query_one("#settings-token-list").display = section_id == "tokens"
        self.query_one("#settings-preference").display = (
            section_id == "preference"
        )
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
        if section_id == "preference":
            return "Space toggle · ←/→ category · Esc close"
        return "←/→ switch category · Esc close"

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

