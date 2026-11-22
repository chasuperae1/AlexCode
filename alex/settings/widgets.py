"""设置界面的小部件：状态圆点与记忆行。"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static

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

class ModeSwitch(Static):
    """左右切换的显示模式开关：两段文字，当前段高亮，整块可点击。"""

    can_focus = True

    BINDINGS = [
        Binding("space", "toggle_mode", "Toggle", show=False),
        Binding("enter", "toggle_mode", "Toggle", show=False),
    ]

    class Changed(Message):
        def __init__(self, switch: "ModeSwitch", value: str) -> None:
            super().__init__()
            self.switch = switch
            self.value = value

    def __init__(self, value: str = "developer", **kwargs) -> None:
        super().__init__("", classes="settings-mode-switch", **kwargs)
        self.value = "concise" if str(value) == "concise" else "developer"

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        theme = self.app.current_theme
        primary = str(getattr(theme, "primary", "#d31145"))
        secondary = str(getattr(theme, "secondary", "#8a929b"))
        foreground = str(getattr(theme, "foreground", "#edeff2"))
        text = Text()
        for index, (label, mode) in enumerate(
            (("Developer", "developer"), ("Concise", "concise"))
        ):
            if index:
                text.append(" ")
            if self.value == mode:
                text.append(
                    f" {label} ", style=f"bold {foreground} on {primary}"
                )
            else:
                text.append(f" {label} ", style=secondary)
        self.update(text)

    def set_value(self, value: str, *, notify: bool = False) -> None:
        value = "concise" if str(value) == "concise" else "developer"
        if value == self.value:
            self._refresh()
            return
        self.value = value
        self._refresh()
        if notify:
            self.post_message(self.Changed(self, value))

    def toggle(self) -> None:
        self.set_value(
            "developer" if self.value == "concise" else "concise",
            notify=True,
        )

    def action_toggle_mode(self) -> None:
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
