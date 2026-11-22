"""AskUser 的内联选择题控件。

按设计稿 v2：无卡片边框，只保留 ``?`` 提问行、缩进选项和一行快捷键提示。
键盘操作由控件自身处理（焦点在控件上时）：
- ↑↓ / j k：移动高亮
- 1-9：单选直接提交，多选切换勾选
- Space / a：多选勾选 / 全选
- Enter：确认或提交
- Tab：进入自定义输入
- Esc：自定义输入时返回选项，否则跳过（由 App 的 escape 动作转发）
"""

from __future__ import annotations

from typing import Any, Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static



class AskOption(Horizontal):
    """一行选项：光标 + 编号 +（多选复选框）+ 标题 + 右侧说明。"""

    DEFAULT_CSS = """
    AskOption {
        width: 1fr;
        height: auto;
        padding: 0 1;
    }
    AskOption .ask-car {
        width: 2;
        height: 1;
        color: transparent;
    }
    AskOption .ask-num {
        width: 3;
        height: 1;
        color: $text-muted;
    }
    AskOption .ask-box {
        width: 4;
        height: 1;
        color: $text-muted;
    }
    AskOption .ask-box.-on {
        color: $success;
    }
    AskOption .ask-body {
        width: 1fr;
        height: auto;
    }
    AskOption .ask-label {
        width: 1fr;
        height: auto;
        color: $foreground;
    }
    AskOption .ask-desc {
        width: 1fr;
        height: auto;
        color: $text-muted;
        padding: 0 1 0 0;
    }
    AskOption.-active {
        background: $primary 10%;
    }
    AskOption.-active .ask-car,
    AskOption.-active .ask-num {
        color: $accent;
    }
    AskOption.-active .ask-label {
        text-style: bold;
    }
    AskOption.-other .ask-label {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        index: int,
        label: str,
        description: str = "",
        *,
        multi: bool = False,
        other: bool = False,
    ) -> None:
        super().__init__(classes="ask-opt" + (" -other" if other else ""))
        self.index = index
        self.label = label
        self.description = description
        self.multi = multi
        self.other = other

    def compose(self) -> ComposeResult:
        yield Static("❯", classes="ask-car")
        yield Static(str(self.index + 1), classes="ask-num")
        if self.multi:
            yield Static("[ ]", classes="ask-box", markup=False)
        # 标题和说明纵向排列并自动换行，避免长文本把编号挤掉或横向截断
        with Vertical(classes="ask-body"):
            yield Static(self.label, classes="ask-label", markup=False)
            if self.description:
                yield Static(
                    self.description, classes="ask-desc", markup=False
                )

    def set_active(self, active: bool) -> None:
        self.set_class(active, "-active")

    def set_checked(self, checked: bool) -> None:
        boxes = self.query(".ask-box")
        if not boxes:
            return
        box = boxes.first()
        box.update("[x]" if checked else "[ ]")
        box.set_class(checked, "-on")


class AskInput(Input):
    """自定义回答输入框：Ctrl+V / Alt+V / F6 优先尝试粘贴剪贴板里的图片。"""

    def action_paste(self) -> None:
        self.run_worker(self._paste(), exclusive=True)

    async def _paste(self) -> None:
        handler = getattr(self.app, "paste_clipboard_image", None)
        if handler is not None and await handler(self):
            return
        Input.action_paste(self)
