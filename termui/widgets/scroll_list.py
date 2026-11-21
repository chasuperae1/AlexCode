"""可滚动列表：虚拟化渲染 + 键盘导航 + 平滑滚动。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from rich.text import Text
from textual import events
from textual.app import RenderResult
from textual.binding import Binding
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip


@dataclass
class ListItem:
    """列表项。"""

    label: str
    description: str = ""
    value: Any = None
    disabled: bool = False


class ScrollList(ScrollView, can_focus=True):
    """虚拟化可滚动列表。

    只渲染可视区域的行，条目数量对性能几乎无影响；
    光标移动通过 `scroll_to(animate=True)` 产生平滑过渡。
    """

    COMPONENT_CLASSES = {
        "scrolllist--cursor",
        "scrolllist--label",
        "scrolllist--description",
        "scrolllist--disabled",
        "scrolllist--index",
    }

    DEFAULT_CSS = """
    ScrollList {
        height: 1fr;
        min-height: 3;
        background: $surface;
        border: round $primary 25%;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $primary 40%;
        scrollbar-color-hover: $primary 70%;
        scrollbar-background: $surface;
    }
    ScrollList:focus {
        border: round $primary;
    }
    ScrollList > .scrolllist--cursor {
        background: $primary 28%;
        text-style: bold;
    }
    ScrollList > .scrolllist--label {
        color: $foreground;
    }
    ScrollList > .scrolllist--description {
        color: $text-muted;
    }
    ScrollList > .scrolllist--disabled {
        color: $text-disabled;
    }
    ScrollList > .scrolllist--index {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("up", "move(-1)", "Move up", show=False),
        Binding("down", "move(1)", "Move down", show=False),
        Binding("pageup", "move(-10)", "Page up", show=False),
        Binding("pagedown", "move(10)", "Page down", show=False),
        Binding("home", "move_to(0)", "First", show=False),
        Binding("end", "move_to(-1)", "Last", show=False),
        Binding("enter", "select", "Select", show=False),
    ]

    class Highlighted(Message):
        """光标移动到新条目。"""

        def __init__(self, index: int, item: ListItem) -> None:
            self.index = index
            self.item = item
            super().__init__()

    class Selected(Message):
        """回车选中条目。"""

        def __init__(self, index: int, item: ListItem) -> None:
            self.index = index
            self.item = item
            super().__init__()

    def __init__(
        self,
        items: Sequence[ListItem | str] = (),
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._items: list[ListItem] = [
            item if isinstance(item, ListItem) else ListItem(str(item))
            for item in items
        ]
        self.cursor_index = 0

    # ------------------------------------------------------------------ 数据
    @property
    def items(self) -> list[ListItem]:
        return self._items

    @property
    def current(self) -> ListItem | None:
        return self._items[self.cursor_index] if self._items else None

    def set_items(self, items: Sequence[ListItem | str]) -> None:
        self._items = [
            item if isinstance(item, ListItem) else ListItem(str(item))
            for item in items
        ]
        self.cursor_index = min(self.cursor_index, max(len(self._items) - 1, 0))
        self._sync_virtual_size()
        self.refresh()

    # ------------------------------------------------------------ 虚拟化尺寸
    def on_mount(self) -> None:
        self._sync_virtual_size()

    def on_resize(self) -> None:
        self._sync_virtual_size()

    def _sync_virtual_size(self) -> None:
        self.virtual_size = Size(max(self.size.width, 1), max(len(self._items), 1))

    # ------------------------------------------------------------------ 渲染
    def render_line(self, y: int) -> Strip:
        width = self.size.width
        index = y + int(self.scroll_offset.y)
        if index >= len(self._items):
            return Strip([], width)

        item = self._items[index]
        is_cursor = index == self.cursor_index
        row_style = (
            self.get_component_rich_style("scrolllist--cursor", partial=True)
            if is_cursor
            else None
        )
        label_style = self.get_component_rich_style(
            "scrolllist--disabled" if item.disabled else "scrolllist--label",
            partial=True,
        )
        desc_style = self.get_component_rich_style(
            "scrolllist--description", partial=True
        )
        index_style = self.get_component_rich_style(
            "scrolllist--index", partial=True
        )

        text = Text(no_wrap=True, overflow="ellipsis")
        if row_style:
            text.stylize(row_style)
        text.append(f"{index + 1:>3} ", style=index_style)
        text.append("▌ " if is_cursor else "  ", style=label_style)
        text.append(item.label, style=label_style)
        if item.description:
            text.append("  " + item.description, style=desc_style)

        text.truncate(width, overflow="ellipsis")
        text.pad_right(width - text.cell_len)
        return Strip(text.render(self.app.console), width)

    # ------------------------------------------------------------ 光标与滚动
    def _move_to(self, index: int) -> None:
        if not self._items:
            return
        index = max(0, min(index, len(self._items) - 1))
        if index == self.cursor_index:
            self._ensure_visible()
            return
        self.cursor_index = index
        self._ensure_visible()
        self.refresh()
        self.post_message(self.Highlighted(index, self._items[index]))

    def _ensure_visible(self) -> None:
        height = self.size.height
        top = int(self.scroll_offset.y)
        if self.cursor_index < top:
            target = self.cursor_index
        elif self.cursor_index >= top + height:
            target = self.cursor_index - height + 1
        else:
            return
        self.scroll_to(y=target, animate=True, duration=0.12, easing="out_cubic")

    def action_move(self, delta: str) -> None:
        self._move_to(self.cursor_index + int(delta))

    def action_move_to(self, index: str) -> None:
        target = int(index)
        self._move_to(len(self._items) - 1 if target < 0 else target)

    def action_select(self) -> None:
        item = self.current
        if item is None or item.disabled:
            return
        self.post_message(self.Selected(self.cursor_index, item))

    def on_click(self, event: events.Click) -> None:
        index = int(event.y + self.scroll_offset.y)
        if 0 <= index < len(self._items):
            self.focus()
            self._move_to(index)
