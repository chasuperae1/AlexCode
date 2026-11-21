"""基础 App：注册主题、提供全局快捷键与状态面板入口。"""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from .theme import next_theme, register_themes
from .widgets.status_panel import StatusPanel


class TermUIApp(App):
    """带主题切换能力的 App 基类。

    子类只需实现 `compose()`，主题与 Ctrl+T 切换已就绪。
    """

    BINDINGS = [
        Binding("ctrl+t", "cycle_theme", "Cycle theme"),
    ]

    def on_mount(self) -> None:
        register_themes(self)

    def action_cycle_theme(self) -> None:
        self.theme = next_theme(self.theme)
        for status in self.query(StatusPanel):
            status.notify(f"Theme: {self.theme}", level="success", timeout=2)
