"""单次工具 diff 卡：按文件列出改动行，可折叠。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import RichLog, Static

from alex.ui.constants import _TDC_MAX_LINES

from alex.ui.cards.base import HeadToggleMixin

if TYPE_CHECKING:
    from alex.ui.panels_chat import ChatLog


class ToolDiffCard(HeadToggleMixin, Vertical):
    """单个 Edit / Write 完成后的 diff：默认收起一行，展开限高、超出内部滚动。"""

    can_focus = True

    BINDINGS = [
        Binding("space", "toggle", "Expand", show=False),
        Binding("enter", "toggle", "Expand", show=False),
    ]

    DEFAULT_CSS = """
    ToolDiffCard {
        height: auto;
        margin: 0 0 1 2;
        padding: 0 1;
        background: transparent;
        border: round $primary 35%;
    }
    ToolDiffCard .tdc-head { height: 1; }
    ToolDiffCard .tdc-head:hover { background: $boost; }
    ToolDiffCard .tdc-path { width: 1fr; }
    ToolDiffCard .tdc-counts { width: auto; }
    ToolDiffCard .tdc-hint { width: auto; padding: 0 1 0 2; color: $text-muted; }
    ToolDiffCard .tdc-body { height: auto; display: none; }
    ToolDiffCard.-open .tdc-body { display: block; }
    ToolDiffCard .tdc-legend { height: 1; color: $text-muted; }
    ToolDiffCard .tdc-code {
        height: 1;
        background: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    ToolDiffCard .tdc-foot { height: 1; }
    ToolDiffCard .tdc-foot-text { width: 1fr; color: $text-muted; }
    ToolDiffCard .tdc-foot-hint { width: auto; color: $text-muted; }
    ToolDiffCard:focus, ToolDiffCard:focus-within { border: round $primary; }
    """

    def __init__(
        self,
        path: str,
        counts: tuple[int, int],
        lines: list[Text],
        clock: str,
        theme,
        **kwargs,
    ) -> None:
        super().__init__(classes="tool-diff", **kwargs)
        self._path = path
        self._additions, self._deletions = counts
        self._lines = lines
        self._clock = clock
        self._theme = theme
        self._open = False
        self._max_lines = _TDC_MAX_LINES
        self._path_static = Static(self._header_path(), classes="tdc-path")
        self._counts_static = Static(self._header_counts(), classes="tdc-counts")
        self._hint_static = Static(self._hint_text(), classes="tdc-hint")
        self._head = Horizontal(
            self._path_static,
            self._counts_static,
            self._hint_static,
            classes="tdc-head",
        )
        self._code = RichLog(
            wrap=False,
            markup=False,
            highlight=False,
            auto_scroll=False,
            classes="tdc-code",
        )
        self._foot_text = Static(self._foot_label(), classes="tdc-foot-text")
        self._foot_hint = Static("Space Collapse · Esc Back", classes="tdc-foot-hint")

    # ------------------------------------------------------------ 文案
    def _header_path(self) -> Text:
        text = Text()
        text.append("▤ ", style=f"bold {self._theme.primary}")
        text.append(self._path, style=self._theme.foreground)
        return text

    def _header_counts(self) -> Text:
        text = Text()
        text.append(f"+{self._additions}", style=self._theme.success)
        text.append("  ")
        text.append(f"−{self._deletions}", style=self._theme.error)
        return text

    def _hint_text(self) -> Text:
        label = "Space Collapse" if self._open else "Space Expand"
        return Text(label, style=self._theme.secondary)

    def _foot_label(self) -> Text:
        text = Text()
        text.append(f"Changes complete {self._clock} · ", style=self._theme.secondary)
        text.append(f"+{self._additions}", style=self._theme.success)
        text.append(" added  ", style=self._theme.secondary)
        text.append(f"−{self._deletions}", style=self._theme.error)
        text.append(" deleted", style=self._theme.secondary)
        return text

    def _legend(self) -> Text:
        text = Text()
        text.append("−", style=self._theme.error)
        text.append(" deleted   ", style=self._theme.secondary)
        text.append("+", style=self._theme.success)
        text.append(" added   ", style=self._theme.secondary)
        text.append("line numbers: before / after", style=self._theme.secondary)
        return text

    # ------------------------------------------------------------ 组装
    def compose(self) -> ComposeResult:
        yield self._head
        with Vertical(classes="tdc-body"):
            yield Static(self._legend(), classes="tdc-legend")
            yield self._code
            with Horizontal(classes="tdc-foot"):
                yield self._foot_text
                yield self._foot_hint

    def on_mount(self) -> None:
        self._write_lines()

    def set_lines(self, lines: list[Text]) -> None:
        """异步取回 diff 后回填卡片，主题切换重建时也会走这里。"""
        self._lines = list(lines)
        self._path_static.update(self._header_path())
        self._counts_static.update(self._header_counts())
        self._foot_text.update(self._foot_label())
        self._write_lines()
        if self._open:
            self._code.styles.height = min(max(1, len(self._lines)), self._max_lines)

    def _write_lines(self) -> None:
        self._code.clear()
        if not self._lines:
            self._code.write(
                Text("No text changes (binary or empty).", style=self._theme.secondary)
            )
            return
        for line in self._lines:
            self._code.write(line)

    # ------------------------------------------------------------ 交互
    def action_toggle(self) -> None:
        self._open = not self._open
        self.set_class(self._open, "-open")
        self._hint_static.update(self._hint_text())
        if self._open:
            height = min(max(1, len(self._lines)), self._max_lines)
            self._code.styles.height = height
            self._code.scroll_home(animate=False)
            self._code.focus()
            # 展开后把卡片拉回视野：ChatLog 里还套了一层容器，直接 scroll_visible 无效
            try:
                chat = self.app.query_one("#chat")
                # 高度刚变，等布局刷两帧再滚，否则读到的是旧位置
                self.call_after_refresh(
                    self.call_after_refresh, self._reveal_in_chat, chat
                )
            except Exception:  # 聊天区不存在时不做滚动
                pass
        else:
            self._code.styles.height = 1
            self.focus()

    def _reveal_in_chat(self, chat: "ChatLog") -> None:
        """展开后让整张卡片落在视野内：能放下就最小滚动，放不下则顶对齐。"""
        content_top = chat.content_region.y
        content_bottom = chat.content_region.bottom
        delta = 0
        if self.region.bottom > content_bottom:
            delta = self.region.bottom - content_bottom
        if self.region.y - delta < content_top:
            delta = self.region.y - content_top
        if delta:
            chat.scroll_to(y=chat.scroll_offset.y + delta, animate=False)
