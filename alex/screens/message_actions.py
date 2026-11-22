"""点击用户消息后弹出的操作菜单。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from alex.screens.modal_base import OPTION_HINT, OptionListModal

REVERT = "revert"
COPY = "copy"
CANCEL = "cancel"


class MessageActionsScreen(OptionListModal):
    """用户消息操作：回到这条消息发送之前，或复制内容。"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
        Binding("c", "copy", "Copy message", show=False),
    ]

    DEFAULT_CSS = """
    MessageActionsScreen {
        align: center middle;
        background: black 60%;
    }
    MessageActionsScreen #message-actions {
        width: 72;
        max-width: 92%;
        height: auto;
        border: round $primary;
        background: $background;
        padding: 1 1;
    }
    MessageActionsScreen #message-actions-title {
        color: $primary;
        text-style: bold;
        height: 1;
    }
    MessageActionsScreen #message-actions-note {
        height: auto;
        color: $text-muted;
        margin: 1 0;
    }
    MessageActionsScreen #message-actions-list {
        height: auto;
        background: $background;
    }
    MessageActionsScreen #message-actions-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, *, can_revert: bool) -> None:
        super().__init__()
        self._can_revert = can_revert

    def compose(self) -> ComposeResult:
        with Vertical(id="message-actions"):
            yield Static("Restore to this message", id="message-actions-title")
            if self._can_revert:
                yield Static(
                    "Files return to the state before this message was sent; "
                    "this message and everything after it are removed from the "
                    "conversation. Its text goes back into the prompt.",
                    id="message-actions-note",
                )
            else:
                yield Static(
                    "No checkpoint is available for this message.",
                    id="message-actions-note",
                )
            yield OptionList(id="message-actions-list")
            yield Static(OPTION_HINT, id="message-actions-hint")

    def on_mount(self) -> None:
        options = self.query_one("#message-actions-list", OptionList)
        if self._can_revert:
            options.add_option(
                Option("Restore and edit this message", id=REVERT)
            )
        options.add_option(Option("Copy message", id=COPY))
        options.add_option(Option("Cancel", id=CANCEL))
        self._focus_list(options)

    def action_copy(self) -> None:
        self.dismiss(COPY)

    def action_cancel(self) -> None:
        self.dismiss(None)
