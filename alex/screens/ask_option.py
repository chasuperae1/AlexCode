"""AskUser 的内联选择题控件：选项行与自定义输入行。

选项行 ``AskOption`` 负责光标、编号、多选框与说明；输入行 ``AskInput``
负责自定义回答。键盘交互由 ``AskChoice`` 统一处理。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from alex.ui.widgets import anchor_terminal_cursor



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

    def on_focus(self, event) -> None:
        # Input 只在光标位置变化时同步 app.cursor_position。刚显示并聚焦时
        # 位置没变，终端光标还停在 (0,0)，首次拼音的候选窗会跑到左上角。
        # 聚焦后等一帧布局稳定，主动把终端光标锚到输入框真实位置。
        self.call_after_refresh(lambda: anchor_terminal_cursor(self))

    def action_paste(self) -> None:
        self.run_worker(self._paste(), exclusive=True)

    async def _paste(self) -> None:
        handler = getattr(self.app, "paste_clipboard_image", None)
        if handler is not None and await handler(self):
            return
        Input.action_paste(self)
