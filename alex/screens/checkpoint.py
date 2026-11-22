"""选择 checkpoint 的 TUI 弹窗。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from alex.core.snapshots import Checkpoint


class CheckpointScreen(ModalScreen[int | None]):
    """列出历史 checkpoint，选择后返回其索引。"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    CheckpointScreen {
        align: center middle;
        background: black 60%;
    }
    CheckpointScreen #checkpoints {
        width: 86;
        max-width: 94%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $background;
        padding: 1 1;
    }
    CheckpointScreen #checkpoints-title {
        color: $primary;
        text-style: bold;
        height: 1;
    }
    CheckpointScreen #checkpoints-list {
        height: auto;
        max-height: 20;
        background: $background;
    }
    CheckpointScreen #checkpoints-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, checkpoints: list[Checkpoint]) -> None:
        super().__init__()
        self._checkpoints = checkpoints

    def compose(self) -> ComposeResult:
        with Vertical(id="checkpoints"):
            yield Static("Restore checkpoint", id="checkpoints-title")
            yield OptionList(id="checkpoints-list")
            yield Static(
                "↑/↓ move · Enter restore · Esc cancel",
                id="checkpoints-hint",
            )

    def on_mount(self) -> None:
        options = self.query_one("#checkpoints-list", OptionList)
        for index in range(len(self._checkpoints) - 1, -1, -1):
            item = self._checkpoints[index]
            prompt = item.prompt.replace("\n", " ").strip() or "(empty)"
            if len(prompt) > 52:
                prompt = prompt[:51] + "…"
            label = (
                f"{item.created_at} · {prompt} · "
                f"{len(item.files)} file(s)"
            )
            options.add_option(Option(label, id=str(index)))
        if self._checkpoints:
            options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        self.dismiss(int(str(event.option.id)))

    def action_cancel(self) -> None:
        self.dismiss(None)
