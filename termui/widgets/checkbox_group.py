"""多选框：方向键导航、空格切换、A 全选、回车提交。"""

from __future__ import annotations

from typing import Any, Sequence

from rich.text import Text
from textual import events
from textual.app import RenderResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget


class CheckboxGroup(Widget, can_focus=True):
    """多选框组。

    选项可以是字符串，或 `(label, value)` 元组，也可以带第三项表示禁用。
    """

    COMPONENT_CLASSES = {
        "checkbox--cursor",
        "checkbox--label",
        "checkbox--checked",
        "checkbox--disabled",
        "checkbox--mark",
    }

    DEFAULT_CSS = """
    CheckboxGroup {
        height: auto;
        min-height: 3;
        background: $surface;
        border: round $primary 25%;
        padding: 0 1;
    }
    CheckboxGroup:focus {
        border: round $primary;
    }
    CheckboxGroup > .checkbox--cursor {
        background: $primary 20%;
    }
    CheckboxGroup > .checkbox--label {
        color: $foreground;
    }
    CheckboxGroup > .checkbox--checked {
        color: $accent;
        text-style: bold;
    }
    CheckboxGroup > .checkbox--mark {
        color: $success;
    }
    CheckboxGroup > .checkbox--disabled {
        color: $text-disabled;
    }
    """

    BINDINGS = [
        Binding("up", "move(-1)", "Move up", show=False),
        Binding("down", "move(1)", "Move down", show=False),
        Binding("space", "toggle", "Toggle", show=False),
        Binding("a", "toggle_all", "Select all / none", show=False),
        Binding("enter", "submit", "Submit", show=False),
    ]

    class SelectionChanged(Message):
        """选中集合变化。"""

        def __init__(self, values: list[Any]) -> None:
            self.values = values
            super().__init__()

    class Submitted(Message):
        """回车提交当前选中集合。"""

        def __init__(self, values: list[Any]) -> None:
            self.values = values
            super().__init__()

    def __init__(
        self,
        options: Sequence[str | tuple],
        *,
        title: str = "",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.title = title
        self._labels: list[str] = []
        self._values: list[Any] = []
        self._disabled: list[bool] = []
        for option in options:
            if isinstance(option, str):
                self._labels.append(option)
                self._values.append(option)
                self._disabled.append(False)
            else:
                self._labels.append(str(option[0]))
                self._values.append(option[1] if len(option) > 1 else option[0])
                self._disabled.append(bool(option[2]) if len(option) > 2 else False)
        self.checked: set[int] = set()
        self.cursor_index = 0

    # ------------------------------------------------------------------ 数据
    @property
    def selected_values(self) -> list[Any]:
        return [self._values[i] for i in sorted(self.checked)]

    def set_checked(self, indexes: Sequence[int]) -> None:
        self.checked = {
            i for i in indexes if 0 <= i < len(self._labels) and not self._disabled[i]
        }
        self.refresh()
        self.post_message(self.SelectionChanged(self.selected_values))

    # ------------------------------------------------------------------ 渲染
    def render(self) -> RenderResult:
        text = Text(no_wrap=True, overflow="ellipsis")
        if self.title:
            text.append(self.title + "\n", style="bold")
        cursor_style = self.get_component_rich_style("checkbox--cursor", partial=True)
        label_style = self.get_component_rich_style("checkbox--label", partial=True)
        checked_style = self.get_component_rich_style("checkbox--checked", partial=True)
        mark_style = self.get_component_rich_style("checkbox--mark", partial=True)
        disabled_style = self.get_component_rich_style(
            "checkbox--disabled", partial=True
        )

        for index, label in enumerate(self._labels):
            is_cursor = index == self.cursor_index and self.has_focus
            is_checked = index in self.checked
            is_disabled = self._disabled[index]

            base = disabled_style if is_disabled else label_style
            line_style = base + cursor_style if is_cursor else base
            box_style = disabled_style if is_disabled else mark_style

            text.append("▌ " if is_cursor else "  ", style=line_style)
            text.append("[x] " if is_checked else "[ ] ", style=box_style)
            text.append(
                label,
                style=checked_style if is_checked and not is_disabled else line_style,
            )
            if index < len(self._labels) - 1:
                text.append("\n")
        return text

    # ------------------------------------------------------------ 键盘与鼠标
    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def action_move(self, delta: str) -> None:
        if not self._labels:
            return
        self.cursor_index = max(
            0, min(self.cursor_index + int(delta), len(self._labels) - 1)
        )
        self.refresh()

    def action_toggle(self) -> None:
        index = self.cursor_index
        if index >= len(self._labels) or self._disabled[index]:
            return
        if index in self.checked:
            self.checked.discard(index)
        else:
            self.checked.add(index)
        self.refresh()
        self.post_message(self.SelectionChanged(self.selected_values))

    def action_toggle_all(self) -> None:
        selectable = {i for i, disabled in enumerate(self._disabled) if not disabled}
        self.checked = set() if self.checked == selectable else selectable
        self.refresh()
        self.post_message(self.SelectionChanged(self.selected_values))

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.selected_values))

    def on_click(self, event: events.Click) -> None:
        offset = 1 if self.title else 0
        index = int(event.y) - offset
        if 0 <= index < len(self._labels):
            self.focus()
            self.cursor_index = index
            self.action_toggle()
