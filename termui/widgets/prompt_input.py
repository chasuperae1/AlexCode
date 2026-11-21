"""输入框：多行编辑、历史记录、实时校验、快捷键。"""

from __future__ import annotations

from typing import Callable, Sequence

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea

Validator = Callable[[str], str | None]


class PromptInput(TextArea):
    """支持多行输入的提示框。

    - Enter 发送，Shift+Enter / Ctrl+J 换行
    - ↑ / ↓ 在首行/末行时回翻历史，其余情况移动光标
    - Ctrl+L 清空
    - 校验失败会标红并发出 `Rejected`，不会提交
    """

    DEFAULT_CSS = """
    PromptInput {
        width: 1fr;
        height: 3;
        max-height: 8;
        border: round $primary 40%;
        background: $surface;
        color: $foreground;
        padding: 0 1;
    }
    PromptInput:focus {
        border: round $primary;
    }
    PromptInput.-invalid {
        border: round $error;
    }
    """

    BINDINGS = [
        Binding("up", "history_prev", "Previous", show=False),
        Binding("down", "history_next", "Next", show=False),
        Binding("ctrl+l", "clear_input", "Clear", show=False),
    ]

    class Rejected(Message):
        """输入未通过校验。"""

        def __init__(self, value: str, reason: str) -> None:
            self.value = value
            self.reason = reason
            super().__init__()

    class Submitted(Message):
        """输入通过校验并提交。"""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(
        self,
        *,
        placeholder: str = "",
        validator: Validator | None = None,
        history: Sequence[str] = (),
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            soft_wrap=True,
            highlight_cursor_line=False,
            id=id,
            classes=classes,
        )
        # 关闭闪烁光标，避免部分 Windows 终端持续全窗口重绘
        self.cursor_blink = False
        self.validator = validator
        self._history: list[str] = [h for h in history if h]
        self._history_index = len(self._history)
        self._sync_height()

    @property
    def history_entries(self) -> list[str]:
        return list(self._history)

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)
        self._sync_height()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_height()

    def _sync_height(self) -> None:
        """按行数显式设置高度，避免 auto 高度在部分终端反复重排。"""
        lines = min(6, max(1, self.text.count("\n") + 1))
        height = lines + 2  # 上下边框
        if self.styles.height != height:
            self.styles.height = height

    async def _on_key(self, event) -> None:
        """TextArea 默认把 Enter 当换行，这里改为发送。"""
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_submit()
            return
        if event.key in {"shift+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    def action_history_prev(self) -> None:
        # 多行编辑时先让光标回到上一行
        if self.cursor_location[0] > 0:
            self.action_cursor_up()
            return
        if not self._history:
            return
        self._history_index = max(0, self._history_index - 1)
        self.value = self._history[self._history_index]

    def action_history_next(self) -> None:
        if not self.cursor_at_last_line:
            self.action_cursor_down()
            return
        if not self._history:
            return
        self._history_index = min(len(self._history), self._history_index + 1)
        if self._history_index == len(self._history):
            self.value = ""
        else:
            self.value = self._history[self._history_index]

    def action_newline(self) -> None:
        self.insert("\n")

    def action_clear_input(self) -> None:
        self.value = ""
        self.remove_class("-invalid")

    def action_submit(self) -> None:
        value = self.text.strip()
        reason = self.validator(value) if self.validator else None
        if reason:
            self.add_class("-invalid")
            self.post_message(self.Rejected(value, reason))
            return
        self.remove_class("-invalid")
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)
        self._history_index = len(self._history)
        self.post_message(self.Submitted(value))

    def clear_history(self) -> None:
        self._history.clear()
        self._history_index = 0
