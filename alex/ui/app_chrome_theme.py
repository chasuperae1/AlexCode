"""AlexApp 的界面外围：主题、任务与进度、目标卡、模型选择、命令面板。"""

from __future__ import annotations

from textual.widgets import Static

from alex.ui.state import _load_ui_state, _save_ui_state
from alex.ui.themes import THEMES, next_theme, theme_label


class ChromeThemeMixin:
    """主题切换。"""

    def _on_theme_changed(self, theme) -> None:
        """Textual 应用完主题后再重渲染。

        ``theme_changed_signal`` 发出时 ``theme_variables`` 已经换成新主题；
        而 ``watch_theme`` 那一 tick 里 ``app.theme_variables`` 还是旧值，
        在里面即时重排会把旧配色再套一遍（切主题看起来"没生效"）。
        """
        context = getattr(self, "render_context", None)
        theme_id = getattr(theme, "name", "") or str(self.theme)
        settings_open = bool(getattr(self, "_settings_open", False))
        if context is not None:
            # 换过的主题渲染结果留在 LRU 里，切回去是零解析。
            # 设置页盖住聊天区时不必让每个块各排一遍：关页会整体重建。
            context.set_theme(str(theme_id), broadcast=not settings_open)
        if self._permission_pending:
            # 重建会清空聊天区，把尚未回应的权限卡一起抹掉；改为原地换色
            card = getattr(self, "_permission_card", None)
            set_theme = getattr(card, "set_theme", None)
            if set_theme is not None:
                set_theme(self.current_theme)
            return
        if settings_open:
            # 设置页盖住了聊天区：在这里重建用户根本看不见，
            # 记一笔，等关页返回时一次性重建。
            self._theme_rebuild_pending = True
            return
        self._render_history()
        if getattr(self, "_subagent_view_open", False):
            self._retheme_subagent_board()

    def action_cycle_theme(self) -> None:
        """Ctrl+T 按注册顺序循环切换五套主题。"""
        self._set_theme(next_theme(self.theme))

    def _set_theme(self, name: str, *, notify: bool = True) -> None:
        if name not in THEMES:
            return
        if self._permission_pending and name != self.theme:
            self.notify(
                "Permission required — resolve it before changing theme",
                severity="warning",
                timeout=3,
            )
            return
        self.theme = name
        if _load_ui_state().get("theme") != name:
            _save_ui_state(theme=name)
        if notify:
            self.notify(f"Theme: {theme_label(name)}", timeout=2)
        try:
            self.query_one("#workspace-line", Static).update(self._workspace_line())
        except Exception:
            pass
