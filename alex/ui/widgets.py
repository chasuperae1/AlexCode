"""聊天流里的基础控件：输入框、消息块、单次 / 整轮 diff 卡片。"""

from __future__ import annotations

import re

from rich.style import Style
from rich.text import Text
from textual import events, on
from textual._wrap import compute_wrap_offsets
from textual.binding import Binding
from textual.widgets import Static
from termui import PromptInput as TermPromptInput

from alex.core.formatting import format_tokens as _format_tokens  # noqa: F401
from alex.core.snapshots import Checkpoint
from alex.ui.constants import _IMAGE_TOKEN


def anchor_terminal_cursor(widget) -> None:
    """把终端光标锚到控件真实位置，避免输入法候选窗跑偏。"""
    try:
        widget.app.cursor_position = widget.cursor_screen_offset
    except Exception:
        return
    widget.refresh()


class PromptInput(TermPromptInput):
    """输入框：图片占位符用独立配色，退格整体删除。"""

    TOKEN_STYLE = Style(color="#c7d0ff", bgcolor="#2b3350", bold=True)
    # 输入框最多显示多少行内容（不含上下边框）
    MAX_VISIBLE_LINES = 6
    _last_height: int | None = None
    _last_highlight_text: str | None = None

    BINDINGS = [
        # 有些终端把 Ctrl+V 自己吃掉做文本粘贴，这条按键能绕过去
        Binding("ctrl+alt+v", "paste_image", "Paste image", show=False),
    ]

    def _sync_height(self) -> None:
        """按实际显示行数（含自动换行）调整高度，最多 6 行。"""
        try:
            width = self.scrollable_content_region.size.width - self.gutter_width
        except Exception:
            width = 0
        if width <= 0:
            super()._sync_height()
            return
        try:
            lines = sum(
                len(compute_wrap_offsets(line, width, tab_size=4)) + 1
                for line in self.document.lines
            )
        except Exception:
            super()._sync_height()
            return
        lines = min(self.MAX_VISIBLE_LINES, max(1, lines))
        height = lines + 2  # 上下边框
        # 高度未变时跳过赋值，避免触发不必要的布局重排
        if height == self._last_height:
            return
        self._last_height = height
        if self.styles.height != height:
            self.styles.height = height

    def on_resize(self) -> None:
        self._last_height = None  # 窗口大小变化时清缓存
        self._sync_height()

    def on_focus(self, event) -> None:
        # 首次聚焦时主动锚定终端光标，避免输入法候选窗先出现在左上角。
        self.call_after_refresh(lambda: anchor_terminal_cursor(self))

    def action_paste(self) -> None:
        # TextArea.action_paste 是同步方法，异步逻辑放 worker 里跑
        self.run_worker(self._paste(), exclusive=True)

    async def _paste(self) -> None:
        handler = getattr(self.app, "paste_clipboard_image", None)
        if handler is not None and await handler():
            return
        super().action_paste()

    def action_paste_image(self) -> None:
        """Ctrl+Alt+V：终端吃掉 Ctrl+V 时的备份通道，直接读剪贴板图片。"""
        handler = getattr(self.app, "paste_clipboard_image", None)
        if handler is not None:
            self.run_worker(handler())

    @on(events.Paste)
    async def _on_paste_image(self, event: events.Paste) -> None:
        """终端送来的粘贴是空的时候，说明图片被它吞了：顺带查一次剪贴板。

        Kitty/Ghostty/Windows Terminal 对纯图片剪贴板只发一个空的 bracketed
        paste（甚至什么都不发）。opencode 也是这么补的：先看剪贴板，再退回
        文本粘贴。
        """
        if (event.text or "").strip():
            return
        handler = getattr(self.app, "paste_clipboard_image", None)
        if handler is None:
            return
        event.prevent_default()
        self.run_worker(handler())

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._apply_token_style()

    def _apply_token_style(self) -> None:
        theme = getattr(self, "_theme", None)
        if theme is not None:
            theme.syntax_styles["image-token"] = self.TOKEN_STYLE

    def on_text_area_changed(self, event) -> None:
        super().on_text_area_changed(event)
        # 文本一变就按出现位置重排占位符：删图后编号立刻回到 1..N，
        # 下一张新图才会重新用 image-1，而不是接着历史序号往上加。
        compact = getattr(self.app, "_compact_pasted_tokens", None)
        if compact is not None:
            compact(self)

    def _set_theme(self, theme: str) -> None:
        super()._set_theme(theme)
        self._apply_token_style()

    def _build_highlight_map(self) -> None:
        super()._build_highlight_map()
        highlights = getattr(self, "_highlights", None)
        document = getattr(self, "document", None)
        if highlights is None or document is None:
            return
        # 文本未变时跳过高亮重算
        current_text = document.text
        if current_text == self._last_highlight_text:
            return
        self._last_highlight_text = current_text
        # 位置按 UTF-8 字节计算，和 TextArea 渲染高亮的方式一致
        for row, text in enumerate(document.lines):
            text = str(text)
            for match in _IMAGE_TOKEN.finditer(text):
                start = len(text[: match.start()].encode("utf-8"))
                end = len(text[: match.end()].encode("utf-8"))
                highlights[row].append((start, end, "image-token"))

    async def _on_key(self, event) -> None:
        # TextArea 自带 F6 = 选中当前行，这里改成粘贴剪贴板图片
        if event.key == "f6":
            event.stop()
            event.prevent_default()
            self.action_paste()
            return
        if event.key in {"backspace", "ctrl+h"} and self._delete_image_token():
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    def _delete_image_token(self) -> bool:
        """光标紧跟在 [image-N] 后面时，整个占位符一次删掉。"""
        selection = self.selection
        if selection.start != selection.end:
            return False
        row, col = self.cursor_location
        line = str(self.document.get_line(row))
        match = re.search(r"\[image-\d+\]$", line[:col])
        if match is None:
            return False
        self.delete((row, match.start()), (row, col))
        return True


class UserMessage(Static):
    """可点击的用户消息，点击后打开消息操作菜单。"""

    def __init__(
        self,
        renderable,
        *,
        exchange_id: int | None = None,
        message_text: str = "",
        **kwargs,
    ) -> None:
        super().__init__(renderable, **kwargs)
        self.exchange_id = exchange_id
        self.message_text = message_text


class ChangesSummary(Static):
    """每轮回复后的文件变更摘要，点击查看 diff。"""

    def __init__(self, checkpoint: Checkpoint) -> None:
        super().__init__(
            _changes_label(checkpoint), classes="changes-summary"
        )
        self.exchange_id = checkpoint.exchange_id


def _changes_text(checkpoint: Checkpoint) -> str:
    count = len(checkpoint.files)
    label = f"{count} file{'s' if count != 1 else ''} changed"
    if checkpoint.additions or checkpoint.deletions:
        label += f" · +{checkpoint.additions} -{checkpoint.deletions}"
    return label


def _changes_label(checkpoint: Checkpoint) -> Text:
    return Text(f"✎ {_changes_text(checkpoint)} · click to view diff")

from alex.ui.cards import (  # noqa: E402,F401
    InlineDiffCard,
    PermissionDiffCard,
    ShellPermissionCard,
    ToolDiffCard,
)
