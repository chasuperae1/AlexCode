"""层级菜单：树形结构、键盘展开/折叠、平滑滚动。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip


@dataclass
class MenuNode:
    """菜单节点。有 children 时为分支，否则为叶子。"""

    label: str
    value: Any = None
    children: list["MenuNode"] = field(default_factory=list)
    expanded: bool = False

    @property
    def is_branch(self) -> bool:
        return bool(self.children)


class HierarchyMenu(ScrollView, can_focus=True):
    """层级菜单。

    - ↑/↓ 移动，→ 展开，← 折叠或回到父级
    - 回车：分支切换展开状态，叶子发出 `Activated`
    """

    COMPONENT_CLASSES = {
        "menu--cursor",
        "menu--label",
        "menu--branch",
        "menu--value",
    }

    DEFAULT_CSS = """
    HierarchyMenu {
        height: 1fr;
        min-height: 3;
        background: $surface;
        border: round $primary 25%;
        padding: 0 1;
        scrollbar-size-vertical: 1;
    }
    HierarchyMenu:focus {
        border: round $primary;
    }
    HierarchyMenu > .menu--cursor {
        background: $primary 25%;
        text-style: bold;
    }
    HierarchyMenu > .menu--label {
        color: $foreground;
    }
    HierarchyMenu > .menu--branch {
        color: $accent;
    }
    HierarchyMenu > .menu--value {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("up", "move(-1)", "Move up", show=False),
        Binding("down", "move(1)", "Move down", show=False),
        Binding("right", "expand", "Expand", show=False),
        Binding("left", "collapse", "Collapse", show=False),
        Binding("enter", "activate", "Activate", show=False),
        Binding("home", "move_to(0)", "First", show=False),
        Binding("end", "move_to(-1)", "Last", show=False),
    ]

    class Activated(Message):
        """叶子节点被激活。"""

        def __init__(self, path: tuple[str, ...], node: MenuNode) -> None:
            self.path = path
            self.node = node
            super().__init__()

    class Toggled(Message):
        """分支展开状态变化。"""

        def __init__(self, path: tuple[str, ...], node: MenuNode) -> None:
            self.path = path
            self.node = node
            super().__init__()

    def __init__(
        self,
        roots: list[MenuNode] | None = None,
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._roots = roots or []
        self._rows: list[tuple[MenuNode, int, tuple[str, ...]]] = []
        self.cursor_index = 0
        self._rebuild()

    # ------------------------------------------------------------------ 结构
    def _rebuild(self) -> None:
        rows: list[tuple[MenuNode, int, tuple[str, ...]]] = []

        def walk(nodes: list[MenuNode], depth: int, prefix: tuple[str, ...]) -> None:
            for node in nodes:
                path = prefix + (node.label,)
                rows.append((node, depth, path))
                if node.is_branch and node.expanded:
                    walk(node.children, depth + 1, path)

        walk(self._roots, 0, ())
        self._rows = rows
        self.cursor_index = max(0, min(self.cursor_index, len(rows) - 1))
        self._sync_virtual_size()

    def _sync_virtual_size(self) -> None:
        self.virtual_size = Size(max(self.size.width, 1), max(len(self._rows), 1))

    def expand_all(self) -> None:
        def walk(nodes: list[MenuNode]) -> None:
            for node in nodes:
                node.expanded = node.is_branch
                walk(node.children)

        walk(self._roots)
        self._rebuild()
        self.refresh()

    def collapse_all(self) -> None:
        def walk(nodes: list[MenuNode]) -> None:
            for node in nodes:
                node.expanded = False
                walk(node.children)

        walk(self._roots)
        self._rebuild()
        self.refresh()

    # ------------------------------------------------------------------ 渲染
    def on_mount(self) -> None:
        self._sync_virtual_size()

    def on_resize(self) -> None:
        self._sync_virtual_size()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        index = y + int(self.scroll_offset.y)
        if index >= len(self._rows):
            return Strip([], width)

        node, depth, _ = self._rows[index]
        is_cursor = index == self.cursor_index

        cursor_style = self.get_component_rich_style("menu--cursor", partial=True)
        label_style = self.get_component_rich_style("menu--label", partial=True)
        branch_style = self.get_component_rich_style("menu--branch", partial=True)
        value_style = self.get_component_rich_style("menu--value", partial=True)

        glyph = ("▾ " if node.expanded else "▸ ") if node.is_branch else "· "
        text = Text(no_wrap=True, overflow="ellipsis")
        if is_cursor:
            text.stylize(cursor_style)
        text.append("  " * depth + ("▌ " if is_cursor else "  "), style=label_style)
        text.append(glyph, style=branch_style)
        text.append(node.label, style=branch_style if node.is_branch else label_style)
        if not node.is_branch and node.value is not None:
            text.append(f"  {node.value}", style=value_style)

        text.truncate(width, overflow="ellipsis")
        text.pad_right(width - text.cell_len)
        return Strip(text.render(self.app.console), width)

    # ------------------------------------------------------------ 光标与操作
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

    def _move_to(self, index: int) -> None:
        if not self._rows:
            return
        self.cursor_index = max(0, min(index, len(self._rows) - 1))
        self._ensure_visible()
        self.refresh()

    @property
    def current(self) -> tuple[MenuNode, int, tuple[str, ...]] | None:
        return self._rows[self.cursor_index] if self._rows else None

    def action_move(self, delta: str) -> None:
        self._move_to(self.cursor_index + int(delta))

    def action_move_to(self, index: str) -> None:
        target = int(index)
        self._move_to(len(self._rows) - 1 if target < 0 else target)

    def action_expand(self) -> None:
        node = self.current
        if node is None:
            return
        current, _, path = node
        if current.is_branch and not current.expanded:
            current.expanded = True
            self._rebuild()
            self.refresh()
            self.post_message(self.Toggled(path, current))

    def action_collapse(self) -> None:
        node = self.current
        if node is None:
            return
        current, depth, path = node
        if current.is_branch and current.expanded:
            current.expanded = False
            self._rebuild()
            self.refresh()
            self.post_message(self.Toggled(path, current))
        elif depth > 0:
            for row_index in range(self.cursor_index - 1, -1, -1):
                if self._rows[row_index][1] == depth - 1:
                    self._move_to(row_index)
                    break

    def action_activate(self) -> None:
        node = self.current
        if node is None:
            return
        current, _, path = node
        if current.is_branch:
            current.expanded = not current.expanded
            self._rebuild()
            self.refresh()
            self.post_message(self.Toggled(path, current))
        else:
            self.post_message(self.Activated(path, current))

    def on_click(self, event: events.Click) -> None:
        index = int(event.y + self.scroll_offset.y)
        if 0 <= index < len(self._rows):
            self.focus()
            self._move_to(index)
            self.action_activate()
