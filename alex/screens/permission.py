"""交互式权限模式选择框。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

CANCEL = ""


class PermissionScreen(ModalScreen[str | None]):
    """列出三种权限模式，返回选中的模式名。"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PermissionScreen {
        align: center middle;
        background: black 60%;
    }
    PermissionScreen #permission-panel {
        width: 78;
        max-width: 94%;
        height: auto;
        border: round $warning;
        background: $background;
        padding: 1 1;
    }
    PermissionScreen #permission-title {
        color: $warning;
        text-style: bold;
        height: 1;
    }
    PermissionScreen #permission-desc {
        color: $text-muted;
        height: auto;
        margin: 0 0 1 0;
    }
    PermissionScreen #permission-options {
        height: auto;
        background: $background;
    }
    PermissionScreen #permission-hint {
        width: 1fr;
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    PermissionScreen #permission-cancel {
        width: auto;
        height: 1;
        margin-top: 1;
        padding: 0 1;
        color: $text-muted;
    }
    PermissionScreen #permission-cancel:hover {
        color: $error;
        text-style: underline;
    }
    """

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-panel"):
            yield Static("Permission mode", id="permission-title")
            yield Static(
                "Choose how Alex handles file changes and commands.",
                id="permission-desc",
            )
            yield OptionList(id="permission-options")
            with Horizontal(id="permission-actions"):
                yield Static(
                    "↑/↓ move · Enter select · Esc cancel",
                    id="permission-hint",
                )
                yield Static("Cancel", id="permission-cancel")

    def on_mount(self) -> None:
        from alex.core.permissions import MODE_DESCRIPTIONS, MODE_NAMES

        options = self.query_one("#permission-options", OptionList)
        current_index = 0
        for index, name in enumerate(MODE_NAMES):
            marker = "●" if name == self._current else "○"
            options.add_option(
                Option(
                    f"{marker} {name:<8} {MODE_DESCRIPTIONS[name]}",
                    id=name,
                )
            )
            if name == self._current:
                current_index = index
        options.highlighted = current_index
        options.focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        self.dismiss(str(event.option.id))

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "permission-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
