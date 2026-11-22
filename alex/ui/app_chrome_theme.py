"""AlexApp 的界面外围：主题、任务与进度、目标卡、模型选择、命令面板。"""

from __future__ import annotations

from textual.widgets import Static

from alex.screens.picker import PickerScreen
from alex.ui.state import _load_ui_state, _save_ui_state
from alex.ui.themes import THEMES, next_theme, theme_label, theme_name


class ChromeThemeMixin:
    """主题切换与主题选择器。"""

    def watch_theme(self) -> None:
        # Markdown 与事件行都按主题变量着色，切换主题后整体重建；
        # 正在展示的权限卡留在原处就地换色（见 _render_history），不会消失
        card = getattr(self, "_permission_card", None)
        if card is not None and hasattr(card, "set_theme"):
            card.set_theme(self.current_theme)
        self._render_history()

    def action_cycle_theme(self) -> None:
        """Ctrl+T 按注册顺序循环切换五套主题。"""
        self._set_theme(next_theme(self.theme))

    def _set_theme(self, name: str, *, notify: bool = True) -> None:
        if name not in THEMES:
            return
        self.theme = name
        if _load_ui_state().get("theme") != name:
            _save_ui_state(theme=name)
        if notify:
            self.notify(f"Theme: {theme_label(name)}", timeout=2)
        try:
            self.query_one("#workspace-line", Static).update(self._workspace_line())
        except Exception:  # 控件未挂载时跳过
            pass

    def _open_theme_picker(self) -> None:
        rows = [
            (theme_label(name), name == self.theme) for name in THEMES
        ]
        self.push_screen(
            PickerScreen(
                "Choose theme",
                rows,
                "↑/↓ move · Enter apply · Esc cancel",
            ),
            self._on_theme_picked,
        )

    def _on_theme_picked(self, label: str | None) -> None:
        if label:
            self._set_theme(theme_name(label))
