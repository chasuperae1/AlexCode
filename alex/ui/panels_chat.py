"""聊天滚动容器。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static
from termui.widgets import SPINNER_FRAMES

from alex.ui.markdown import MarkdownStatic
from alex.ui.widgets import ChangesSummary, UserMessage
from alex.ui.activity import SubagentNotice, SubagentRow


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

    # 一格滚轮的步长与动画：终端滚轮事件是离散的，一次挪 2 行且不带动画，
    # 连续滚动时看起来就是“一格一格跳”。3 行 + 80ms 缓动，连续触发时
    # 目标位置会累加，视觉上是连续滑动而不是瞬移。
    WHEEL_STEP = 3
    WHEEL_DURATION = 0.08
    WHEEL_EASING = "out_cubic"
    # 历史重建 / 补页期间由应用接管视口，这里不要再触发“回到底部就跟随”。
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

    def on_mount(self) -> None:
        # 贴底交给框架的 scroll anchor：布局每长高一次就自动把视口钉到底部，
        # 用户一旦自己滚动（scroll_to 会 release_anchor）就不再跟随。
        # 之前自研的 100ms 跟随定时器会在用户上翻时反复把视口拽回底部。
        self.anchor(True)

    @property
    def is_following(self) -> bool:
        """是否仍在“新内容长出来就跟到底部”的跟随状态。"""
        return self.is_anchored and not self._anchor_released

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        # 用户自己滚回底部（或程序把视口放回底部）时重新挂上跟随，
        # 之后新内容继续自动贴底；停在中途则保持不动。
        super().watch_scroll_y(old_value, new_value)
        if (
            self.is_anchored
            and self._anchor_released
            and not self._suspend_scroll
            and new_value >= self.max_scroll_y - 1
        ):
            self._anchor_released = False

    def _scroll_down_for_pointer(self, **_kwargs) -> bool:
        return self._wheel(1)

    def _scroll_up_for_pointer(self, **_kwargs) -> bool:
        return self._wheel(-1)

    def _wheel(self, direction: int) -> bool:
        """滚轮：按目标位置累加并做短缓动，连续滚动是连续位移。

        ``immediate=True`` 很关键：容器版的 ``scroll_to`` 默认延后到下一次
        刷新才真正滚动，每个滚轮事件都白白多等一帧，滑起来就是拖沓。
        """
        if not self.allow_vertical_scroll:
            return False
        target = self.scroll_target_y + direction * self.WHEEL_STEP
        self.scroll_to(
            y=target,
            animate=True,
            duration=self.WHEEL_DURATION,
            easing=self.WHEEL_EASING,
            immediate=True,
        )
        if direction > 0 and target >= self.max_scroll_y:
            # 滚到底就重新挂上跟随：之后新内容继续自动贴底。已经贴底时
            # scroll_to 不会改 scroll_y，watch_scroll_y 也就不会触发。
            self._anchor_released = False
        return True

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
        doomed = [
            child
            for child in body.children
            if not (keep is not None and child is keep)
        ]
        if doomed:
            # 一块一块 remove 会让容器重排 N 次；成批摘掉，重建时少卡几十毫秒。
            body.remove_children(doomed)

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
        elif isinstance(event.widget, (SubagentRow, SubagentNotice)):
            event.stop()
            handler = getattr(self.app, "open_subagent", None)
            if handler is not None:
                handler(event.widget.subagent_id)
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
        # 贴底不需要在这里手动做：ChatLog 还处于 anchor 状态时，框架在
        # 下一次布局就把视口钉到新的底部；用户已经滚开（anchor 被 release）
        # 时则保持当前位置，不会像以前那样每挂一块控件就把读者拽回底部。
        self.query_one("#chat-body").mount(widget)

    def pin_bottom(self) -> None:
        """外部（历史重建等）请求贴底。"""
        self.anchor(True)

    def follow_bottom(self) -> None:
        """贴底并持续跟随：交给框架 anchor，高度分几帧长出来也会跟上。"""
        self._stop_follow()
        self.anchor(True)

    def _stop_follow(self) -> None:
        """解除贴底跟随，把视口交给调用方。"""
        self.release_anchor()

    def stop_follow(self) -> None:
        """外部要自己摆弄历史窗口时先停掉贴底跟随。

        跟随状态下每帧布局都会把视口钉回底部；翻历史时一边前插补滚动、
        一边被拽回底部，读者就会感觉“划不动、一松手又被拉走”。
        """
        self._stop_follow()

