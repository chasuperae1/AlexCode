"""聊天滚动容器。"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static
from termui.widgets import SPINNER_FRAMES

from alex.ui.markdown import MarkdownStatic
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


class WindowEdge(Static):
    """窗口边界行：只在还有更早 / 更晚的历史没挂载时出现。"""

    def __init__(self, label: str, direction: str) -> None:
        super().__init__(label, classes=f"window-edge window-edge-{direction}")
        self.direction = direction


class CompactionBlock(Vertical):
    """上下文压缩卡：默认收成一行标题，点击 / Enter / Space 展开摘要全文。

    摘要动辄十几行，实时挂载时它会把正在追的对话顶走；历史里也占掉半屏，
    所以默认收起，只留“≡ Context compacted · 12k → 4k”这一行。
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "toggle_body", "Expand/collapse", show=False),
        Binding("space", "toggle_body", "Expand/collapse", show=False),
    ]

    def __init__(
        self,
        label: str,
        body: str,
        theme,
        *,
        width: int = 0,
        context=None,
    ) -> None:
        super().__init__(classes="compaction-block")
        self._theme = theme
        self._label = label
        self._collapsed = True
        self._title = Static(classes="compaction-title")
        self._body = (
            MarkdownStatic(
                body,
                classes="md compaction-body",
                app=None,
                width=width,
                context=context,
            )
            if body.strip()
            else None
        )
        self._sync()

    def compose(self) -> ComposeResult:
        yield self._title
        if self._body is not None:
            yield self._body

    def on_mount(self) -> None:
        self._sync()

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def action_toggle_body(self) -> None:
        if self._body is None:
            return
        at_bottom = False
        try:
            chat = self.app.query_one("#chat", ChatLog)
            at_bottom = chat.scroll_y >= chat.max_scroll_y - 2
        except Exception:
            chat = None
        self._collapsed = not self._collapsed
        self._sync()
        # 展开时正文长在标题下方：本来贴着底部的读者，要跟着往下挪一格，
        # 否则新长出来的摘要在屏幕外。
        if not self._collapsed and at_bottom and chat is not None:
            def reveal() -> None:
                chat.scroll_end(animate=False)

            self.call_after_refresh(reveal)

    def on_click(self, event) -> None:
        if self._body is None:
            return
        event.stop()
        self.focus()
        self.action_toggle_body()

    def _sync(self) -> None:
        has_body = self._body is not None
        marker = ("▸ " if self._collapsed else "▾ ") if has_body else ""
        text = Text()
        text.append(f"{marker}{self._label}", style=f"bold {self._theme.secondary}")
        self._title.update(text)
        if has_body:
            self._body.display = not self._collapsed
        self.set_class(not self._collapsed, "-expanded")


class ChatLog(VerticalScroll):
    """聊天区：增量挂载消息控件（Markdown 渲染），自动滚到底部。

    只更新正在流式输出的那一个控件，避免整段对话反复重排。
    """

    # 一次性挂载整段历史后，Markdown 高度要分几帧、按实际宽度重排才稳定，
    # 所以贴底不能只做一次：短时间跟随内容变化，高度稳定后再停。
    FOLLOW_INTERVAL = 0.1
    FOLLOW_MAX_SECONDS = 3.0
    # 高度是按批稳定的：几帧之内 max_scroll_y 可能连续两次不变，随后又长一截。
    # 太早判定“稳定”就会停在半页，所以至少跟随这么久再允许停。
    FOLLOW_MIN_SECONDS = 1.2
    _suspend_scroll = False
    _follow_timer = None
    _follow_last_max = -1
    _follow_stable = 0
    _follow_last_y = 0
    _follow_deadline = 0.0
    _follow_started = 0.0

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
    ChatLog .stream-hidden {
        color: $text-muted;
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
        padding: 0 1;
        color: $secondary;
        text-style: bold;
    }
    ChatLog .compaction-block {
        width: 1fr;
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        border: round $secondary 25%;
    }
    ChatLog .compaction-block.-expanded {
        border: round $secondary 45%;
    }
    ChatLog .compaction-block:hover,
    ChatLog .compaction-block:focus {
        border: round $secondary 65%;
    }
    ChatLog .compaction-body {
        padding: 0 1 1 1;
        margin: 0 0 0 0;
    }
    ChatLog .turn-stats {
        background: $background;
        color: $text-muted;
        padding: 0;
        margin: 0 0 1 0;
        text-align: right;
    }
    ChatLog .window-edge {
        height: 1;
        color: $text-muted;
        background: $background;
        text-align: center;
        padding: 0;
        margin: 0 0 1 0;
    }
    ChatLog .window-edge:hover {
        color: $primary;
        text-style: underline;
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

    def on_resize(self, event) -> None:
        # 聊天区宽度是 Markdown 排版的唯一来源：布局定下来后写进渲染上下文，
        # 块跟着统一重排（同一宽度同一份源文本只会解析一次）。
        self.call_after_refresh(self.sync_render_width)

    def sync_render_width(self) -> None:
        try:
            body = self.query_one("#chat-body")
        except Exception:  # compose 还没完成
            return
        context = getattr(self.app, "render_context", None)
        if context is None:
            return
        context.set_width(int(body.content_region.width or body.size.width or 0))

    def clear(self, keep=None) -> None:
        """清空聊天区；``keep`` 指定的控件原地保留（未决权限卡等）。"""
        body = self.query_one("#chat-body")
        for child in list(body.children):
            if keep is not None and child is keep:
                continue
            child.remove()

    def on_click(self, event) -> None:
        if isinstance(event.widget, WindowEdge):
            event.stop()
            handler = getattr(
                self.app,
                "load_earlier_history"
                if event.widget.direction == "earlier"
                else "load_newer_history",
                None,
            )
            if handler is not None:
                handler()
            # 点击聊天区会把焦点交给 ChatLog，用户接着打字就落不到输入框；
            # 翻页是浏览动作，交还焦点才符合预期。
            focus = getattr(self.app, "_restore_prompt_focus", None)
            if focus is not None:
                self.call_after_refresh(focus)
        elif isinstance(event.widget, UserMessage):
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
        # 正在看历史窗口时不要把人拽到底部；新消息照常挂在窗口下方，
        # 回到最新时整体重建再显示。
        if not self._suspend_scroll and getattr(
            self.app, "_window_start", None
        ) is None:
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
        这里每帧贴一次，到过底部且连续两帧高度不变（并且已经跟随
        ``FOLLOW_MIN_SECONDS``）才停。
        """
        self._follow_deadline = time.monotonic() + seconds
        self._follow_started = time.monotonic()
        self._follow_last_max = -1
        self._follow_stable = 0
        self._pin_to_bottom()
        # 以“当前位置”作为防误判基准：scroll_end 可能还要等一帧布局才生效，
        # 此时直接拿 max_scroll_y 当基准，会把应用自己的贴底当成用户上翻而停掉。
        self._follow_last_y = self.scroll_y
        if self._follow_timer is None:
            self._follow_timer = self.set_interval(
                self.FOLLOW_INTERVAL, self._follow_tick
            )

    def _follow_tick(self) -> None:
        max_y = self.max_scroll_y
        # 高度还在长（max 变大）时，旧的最大值不代表用户上翻，先别下结论。
        grew = max_y > self._follow_last_max
        if (
            not grew
            and self._follow_last_y
            and self.scroll_y + 3 < self._follow_last_y
        ):
            # 用户自己往上翻了：立即停止跟随，别把人拽回底部
            self._stop_follow()
            return
        self.scroll_end(animate=False)
        self._follow_last_y = max_y
        at_bottom = self.scroll_y >= self.max_scroll_y - 1
        if max_y == self._follow_last_max and at_bottom:
            self._follow_stable += 1
        else:
            self._follow_stable = 0
        self._follow_last_max = max_y
        now = time.monotonic()
        if (
            self._follow_stable >= 2
            and now - self._follow_started >= self.FOLLOW_MIN_SECONDS
        ):
            self._stop_follow()
        elif now >= self._follow_deadline:
            self._stop_follow()

    def _stop_follow(self) -> None:
        self._follow_stable = 0
        self._follow_last_max = -1
        self._follow_last_y = 0
        if self._follow_timer is not None:
            self._follow_timer.stop()
            self._follow_timer = None
