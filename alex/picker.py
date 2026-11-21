"""通用单选弹窗：模型列表与思考档位共用。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class PickerScreen(ModalScreen[str | None]):
    """标题 + 单选列表 + 一行快捷键提示，关闭时返回选中项 id。"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PickerScreen {
        align: center middle;
        background: black 60%;
    }
    PickerScreen #picker {
        width: 56;
        max-width: 92%;
        height: auto;
        border: round $primary;
        background: $background;
        padding: 1 1;
    }
    PickerScreen #picker-title {
        color: $primary;
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }
    PickerScreen #picker-list {
        height: auto;
        max-height: 14;
        background: $background;
    }
    PickerScreen #picker-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        title: str,
        rows: list[tuple[str, bool]],
        hint: str,
    ) -> None:
        super().__init__()
        self._title = title
        self._rows = rows
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self._title, id="picker-title")
            yield OptionList(id="picker-list")
            yield Static(self._hint, id="picker-hint")

    def on_mount(self) -> None:
        options = self.query_one("#picker-list", OptionList)
        highlight = 0
        for index, (name, current) in enumerate(self._rows):
            label = Text(name)
            if current:
                label.append("   current", style="dim")
                highlight = index
            options.add_option(Option(label, id=name))
        options.highlighted = highlight
        options.focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        self.dismiss(str(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)
