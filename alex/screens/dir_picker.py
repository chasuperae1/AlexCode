"""Interactive directory picker used by the ``/cd`` command."""

from __future__ import annotations

import os
import string
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from alex.config import workspace_path

_USE = "use"
_DEFAULT = "default"
_PARENT = "parent"
_DIR = "dir:"
_DRIVE = "drive:"


class DirectoryPicker(ModalScreen[Path | None]):
    """Browse one directory level at a time and return the chosen path."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    DirectoryPicker {
        align: center middle;
        background: black 60%;
    }
    DirectoryPicker #picker {
        width: 70;
        max-width: 90%;
        height: 70%;
        max-height: 24;
        border: round $primary;
        background: $background;
        padding: 1 1;
    }
    DirectoryPicker #picker-path {
        height: 1;
        color: $primary;
        text-style: bold;
    }
    DirectoryPicker #picker-dirs {
        height: 1fr;
        background: $background;
    }
    DirectoryPicker #picker-hint {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, start: Path) -> None:
        super().__init__()
        self._path = self._normalize(start)

    @staticmethod
    def _normalize(path: Path) -> Path:
        try:
            path = path.expanduser().resolve()
        except OSError:  # 路径不可解析时保留原路径
            pass
        if path.is_file():
            path = path.parent
        if not path.is_dir():
            path = Path.cwd()
        return path

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(str(self._path), id="picker-path")
            yield OptionList(id="picker-dirs")
            yield Static(
                "↑/↓ move · Enter open · Esc cancel",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        self._refresh()

    def _entries(self) -> list[tuple[str, str]]:
        entries = [
            (_DEFAULT, "⌂ Use default workspace (agent_workspace)"),
            (_USE, "✓ Use this folder"),
        ]
        parent = self._path.parent
        if parent != self._path:
            entries.append((_PARENT, ".. (parent)"))
        try:
            children = sorted(
                (path for path in self._path.iterdir() if path.is_dir()),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            children = []
        entries.extend(
            (f"{_DIR}{child}", f"  {child.name}/") for child in children
        )
        if os.name == "nt" and parent == self._path:
            for letter in string.ascii_uppercase:
                root = Path(f"{letter}:\\")
                try:
                    if root.exists() and root != self._path:
                        entries.append((f"{_DRIVE}{root}", f"  {root}"))
                except OSError:
                    continue
        return entries

    def _refresh(self) -> None:
        self.query_one("#picker-path", Static).update(str(self._path))
        options = self.query_one("#picker-dirs", OptionList)
        options.clear_options()
        for option_id, label in self._entries():
            options.add_option(Option(label, id=option_id))
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        option_id = str(event.option.id)
        if option_id == _DEFAULT:
            self.dismiss(workspace_path(create=True))
        elif option_id == _USE:
            self.dismiss(self._path)
        elif option_id == _PARENT:
            self._path = self._path.parent
            self._refresh()
        elif option_id.startswith(_DIR):
            self._path = Path(option_id[len(_DIR) :])
            self._refresh()
        elif option_id.startswith(_DRIVE):
            self._path = Path(option_id[len(_DRIVE) :])
            self._refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)
