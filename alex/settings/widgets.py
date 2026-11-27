"""设置界面的小部件：状态圆点、两段开关、主题开关与记忆行。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static

from alex.ui.themes import DEFAULT_THEME, THEMES, theme_label


class _SwitchValue(Message):
    """开关值变更的公共载荷：哪个开关、变成什么值。"""

    def __init__(self, switch, value: str) -> None:
        super().__init__()
        self.switch = switch
        self.value = value


class _ClickFocusMixin:
    """点击开关时聚焦并切换；子类提供 ``toggle``。"""

    def _on_click(self, event) -> None:
        event.stop()
        self.focus()
        self.toggle()


class StatusDot(_ClickFocusMixin, Static):
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


class ModeSwitch(_ClickFocusMixin, Static):
    """左右切换的两段开关：两段文字，当前段高亮，整块可点击。

    默认是显示模式（Developer / Concise）；传入 ``options`` 可以换成别的
    二元开关，比如铃声的 Off / On。
    """

    can_focus = True
    DEFAULT_OPTIONS = (("Developer", "developer"), ("Concise", "concise"))

    BINDINGS = [
        Binding("space", "toggle_mode", "Toggle", show=False),
        Binding("enter", "toggle_mode", "Toggle", show=False),
    ]

    class Changed(_SwitchValue):
        """显示模式一类的二元开关换了值。"""

    def __init__(
        self,
        value: str = "developer",
        *,
        options: tuple[tuple[str, str], ...] | None = None,
        **kwargs,
    ) -> None:
        super().__init__("", classes="settings-mode-switch", **kwargs)
        self.options = tuple(options or self.DEFAULT_OPTIONS)
        self.value = self._normalize(value)

    def _values(self) -> list[str]:
        return [value for _label, value in self.options]

    def _normalize(self, value: str) -> str:
        values = self._values()
        return str(value) if str(value) in values else values[0]

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        theme = self.app.current_theme
        primary = str(getattr(theme, "primary", "#d31145"))
        secondary = str(getattr(theme, "secondary", "#8a929b"))
        foreground = str(getattr(theme, "foreground", "#edeff2"))
        text = Text()
        for index, (label, value) in enumerate(self.options):
            if index:
                text.append(" ")
            if self.value == value:
                text.append(
                    f" {label} ", style=f"bold {foreground} on {primary}"
                )
            else:
                text.append(f" {label} ", style=secondary)
        self.update(text)

    def set_value(self, value: str, *, notify: bool = False) -> None:
        value = self._normalize(value)
        if value == self.value:
            self._refresh()
            return
        self.value = value
        self._refresh()
        if notify:
            self.post_message(self.Changed(self, value))

    def toggle(self) -> None:
        values = self._values()
        if len(values) < 2:
            return
        index = values.index(self.value)
        self.set_value(values[(index + 1) % len(values)], notify=True)

    def action_toggle_mode(self) -> None:
        self.toggle()


class _ThemeSegment(Static):
    """主题开关里的一段文字，只负责呈现与被点中。"""

    def __init__(self, label: str, value: str) -> None:
        super().__init__("", classes="settings-theme-segment")
        self.label = label
        self.value = value


class ThemeSwitch(Horizontal):
    """五段文字的主题开关：选中段反白，点哪段就切到哪段。

    视觉沿用 :class:`ModeSwitch` 的高亮块，只因为选项多于两个，点击的语义
    从"切到另一段"变成"切到我点的这段"，所以单独实现。
    """

    can_focus = True

    BINDINGS = [
        Binding("space", "pick_next", "Next theme", show=False),
        Binding("enter", "pick_next", "Next theme", show=False),
    ]

    class Changed(_SwitchValue):
        """选中的主题换了。"""

    def __init__(
        self,
        value: str = DEFAULT_THEME,
        *,
        options: tuple[tuple[str, str], ...] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(classes="settings-theme-switch", **kwargs)
        self.options = tuple(
            options
            or tuple((theme_label(name), name) for name in THEMES)
        )
        self.value = self._normalize(value)
        self.styles.width = self._width()

    def _width(self) -> int:
        """每段占「一空格 + 标签 + 一空格」，宽度一次算准，省掉 auto 尺寸。"""
        return sum(len(label) + 2 for label, _value in self.options)

    def compose(self) -> ComposeResult:
        for label, value in self.options:
            yield _ThemeSegment(label, value)

    def _values(self) -> list[str]:
        return [value for _label, value in self.options]

    def _normalize(self, value: str) -> str:
        values = self._values()
        return str(value) if str(value) in values else values[0]

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if not self.is_mounted:
            return
        theme = self.app.current_theme
        primary = str(getattr(theme, "primary", "#d31145"))
        secondary = str(getattr(theme, "secondary", "#8a929b"))
        foreground = str(getattr(theme, "foreground", "#edeff2"))
        for segment in self.query(_ThemeSegment):
            segment.styles.width = len(segment.label) + 2
            selected = segment.value == self.value
            style = f"bold {foreground} on {primary}" if selected else secondary
            segment.update(Text(f" {segment.label} ", style=style))

    def set_value(self, value: str, *, notify: bool = False) -> None:
        value = self._normalize(value)
        changed = value != self.value
        self.value = value
        self._refresh()
        if changed and notify:
            self.post_message(self.Changed(self, value))

    def action_pick_next(self) -> None:
        values = self._values()
        index = values.index(self.value)
        self.set_value(values[(index + 1) % len(values)], notify=True)

    def _value_at(self, x: int, y: int) -> str | None:
        for segment in self.query(_ThemeSegment):
            if segment.region.contains(x, y):
                return segment.value
        return None

    def _on_click(self, event) -> None:
        value = self._value_at(event.screen_x, event.screen_y)
        if value is None:
            return
        event.stop()
        self.focus()
        self.set_value(value, notify=True)


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
