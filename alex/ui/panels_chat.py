"""聊天滚动容器。"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static
from termui.widgets import SPINNER_FRAMES

from alex.ui.widgets import ChangesSummary, UserMessage


class CompactionStatus(Static):
    """上下文压缩中的单行状态：复用 Settings 的盲文 spinner。"""

    def __init__(self) -> None:
        super().__init__(classes="compacting")
        self._frame = 0

    def on_mount(self) -> None:
        self._render_frame()
        self.set_interval(0.12, self._tick)

    def _tick(self) -> None:
        self._frame += 1
        self._render_frame()

    def _render_frame(self) -> None:
        theme = self.app.current_theme
        frame = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        line = Text()
        line.append(frame, style=f"bold {theme.accent}")
        line.append(" Compacting context…", style=theme.secondary)
        self.update(line)


class ChatLog(VerticalScroll):
    """聊天区：增量挂载消息控件（Markdown 渲染），自动滚到底部。

    只更新正在流式输出的那一个控件，避免整段对话反复重排。
    """

    # 一次性挂载整段历史后，Markdown 高度要分几帧、按实际宽度重排才稳定，
    # 所以贴底不能只做一次：短时间跟随内容变化，高度稳定后再停。
    FOLLOW_INTERVAL = 0.1
    FOLLOW_MAX_SECONDS = 3.0
    _suspend_scroll = False
    _follow_timer = None
    _follow_last_max = -1
    _follow_stable = 0
    _follow_last_y = 0
    _follow_deadline = 0.0

    DEFAULT_CSS = """
    ChatLog {
        width: 1fr;
        height: 1fr;
        background: $background;
        padding: 1 1 0 1;
        scrollbar-size-vertical: 0;
    }
    ChatLog #chat-body {
        height: auto;
        width: 1fr;
    }
    ChatLog .md {
        background: $background;
        margin: 0 0 1 0;
        padding: 0;
    }
    ChatLog .role {
        color: $primary;
        text-style: bold;
        margin-top: 1;
    }
    ChatLog .role-user {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }
    ChatLog .user-bubble {
        background: $primary 18%;
        border: round $primary 18%;
        padding: 0 1;
        margin: 0;
    }
    ChatLog .user-bubble:hover {
        background: $primary 32%;
        border: round $primary 32%;
    }
    ChatLog .changes-summary {
        background: $background;
        color: $text-muted;
        padding: 0 2;
        margin: 0 0 1 0;
    }
    ChatLog .changes-summary:hover {
        color: $primary;
        text-style: underline;
    }
    ChatLog .compaction-title {
        margin-top: 1;
        color: $secondary;
        text-style: bold;
    }
    ChatLog .compaction-block {
        width: 1fr;
        height: auto;
    }
    ChatLog .compaction-body {
        border-left: thick $secondary 40%;
        padding-left: 1;
    }
    ChatLog .turn-stats {
        background: $background;
        color: $text-muted;
        padding: 0;
        margin: 0 0 1 0;
        text-align: right;
    }
    ChatLog .ask-history {
        height: auto;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    ChatLog .compacting {
        height: 1;
        margin: 0 0 1 0;
        padding: 0;
    }
    ChatLog .debug {
        background: $background;
        border: none;
        padding-left: 0;
        margin: 1 2 0 0;
    }
    ChatLog .debug:focus-within {
        background-tint: $background 0%;
    }
    ChatLog .debug CollapsibleTitle {
        background: $background;
    }
    ChatLog .debug CollapsibleTitle:hover,
    ChatLog .debug CollapsibleTitle:focus {
        background: $background;
        color: $foreground;
    }
    ChatLog .debug-body {
        height: auto;
        padding: 0 1;
    }
    ChatLog .rule-summary CollapsibleTitle {
        color: $text-muted;
    }
    ChatLog .rule-summary CollapsibleTitle:hover,
    ChatLog .rule-summary CollapsibleTitle:focus {
        color: $foreground;
    }
    ChatLog .turn-summary CollapsibleTitle {
        color: $text-muted;
    }
    ChatLog .turn-summary CollapsibleTitle:hover,
    ChatLog .turn-summary CollapsibleTitle:focus {
        color: $foreground;
    }
    ChatLog .tool-line {
        height: auto;
        padding: 0 1;
        margin: 0;
    }
    ChatLog .tool-line-verifier {
        padding: 0 1 0 3;
    }
    ChatLog .role-verifier {
        color: $secondary;
        text-style: bold;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(id="chat-body")

    def clear(self, keep=None) -> None:
        """清空聊天区；``keep`` 指定的控件原地保留（未决权限卡等）。"""
        body = self.query_one("#chat-body")
        for child in list(body.children):
            if keep is not None and child is keep:
                continue
            child.remove()

    def on_click(self, event) -> None:
        if isinstance(event.widget, UserMessage):
            event.stop()
            handler = getattr(self.app, "open_message_actions", None)
            if handler is not None:
                handler(event.widget)
        elif isinstance(event.widget, ChangesSummary):
            event.stop()
            handler = getattr(self.app, "open_change_diff", None)
            if handler is not None:
                handler(event.widget)

    def add_static(self, renderable, classes: str = "") -> Static:
        widget = Static(renderable, classes=classes)
        self._mount(widget)
        return widget

    def add_markdown(self, content: str) -> Markdown:
        widget = Markdown(content, classes="md")
        self._mount(widget)
        return widget

    def add_widget(self, widget) -> None:
        self._mount(widget)

    def _mount(self, widget) -> None:
        self.query_one("#chat-body").mount(widget)
        if not self._suspend_scroll:
            self.call_after_refresh(self._pin_to_bottom)

    def _pin_to_bottom(self) -> None:
        """新控件挂上后贴底：高度要等布局完才算准，所以刷两帧。"""
        self.scroll_end(animate=False)
        self.call_after_refresh(self.scroll_end, animate=False)

    def pin_bottom(self) -> None:
        """外部（历史重建等）请求贴底，复用挂载时的两帧逻辑。"""
        self._pin_to_bottom()

    def follow_bottom(self, seconds: float = FOLLOW_MAX_SECONDS) -> None:
        """贴底并在内容高度稳定前持续跟随。

        历史整体重建时 ``mount_all`` 一次挂上几十上百个 Markdown 控件，首帧
        只算出了部分高度；只贴一次会停在半途，重启后视口仍不在最新消息处。
        这里每帧贴一次，连续两帧最大滚动值不再变化（或超时）就停止。
        """
        self._follow_deadline = time.monotonic() + seconds
        self._follow_last_max = -1
        self._follow_stable = 0
        self._pin_to_bottom()
        self._follow_last_y = self.max_scroll_y
        if self._follow_timer is None:
            self._follow_timer = self.set_interval(
                self.FOLLOW_INTERVAL, self._follow_tick
            )

    def _follow_tick(self) -> None:
        if self._follow_last_y and self.scroll_y + 3 < self._follow_last_y:
            # 用户自己往上翻了：立即停止跟随，别把人拽回底部
            self._stop_follow()
            return
        max_y = self.max_scroll_y
        if max_y == self._follow_last_max:
            self._follow_stable += 1
        else:
            self._follow_last_max = max_y
            self._follow_stable = 0
        self.scroll_end(animate=False)
        self._follow_last_y = max_y
        if (
            self._follow_stable >= 2
            or time.monotonic() >= self._follow_deadline
        ):
            self._stop_follow()

    def _stop_follow(self) -> None:
        self._follow_stable = 0
        self._follow_last_max = -1
        self._follow_last_y = 0
        if self._follow_timer is not None:
            self._follow_timer.stop()
            self._follow_timer = None
