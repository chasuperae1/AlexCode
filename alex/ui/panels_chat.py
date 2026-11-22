"""聊天滚动容器。"""

from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, Static

from alex.ui.constants import QUEUE_CARD_LIMIT
from alex.ui.sources import format_bytes
from alex.ui.widgets import ChangesSummary, UserMessage



class ChatLog(VerticalScroll):
    """聊天区：增量挂载消息控件（Markdown 渲染），自动滚到底部。

    只更新正在流式输出的那一个控件，避免整段对话反复重排。
    """

    _suspend_scroll = False

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

    def clear(self) -> None:
        body = self.query_one("#chat-body")
        for child in list(body.children):
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
