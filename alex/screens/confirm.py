"""TUI 权限确认弹窗。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from alex.screens.modal_base import OptionListModal
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from alex.screens.diff import render_diff

ALLOW = "allow"
ALWAYS = "always"
DENY = "deny"


class ConfirmScreen(OptionListModal):
    """要求用户确认一次敏感操作。返回 allow / always / deny。"""

    BINDINGS = [
        Binding("escape", "deny", "Deny", priority=True),
        Binding("ctrl+c", "deny", "Deny", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
        background: black 60%;
    }
    ConfirmScreen #confirm {
        width: 72;
        max-width: 92%;
        height: auto;
        max-height: 80%;
        border: round $warning;
        background: $background;
        padding: 1 1;
    }
    ConfirmScreen #confirm-title {
        color: $warning;
        text-style: bold;
        height: 1;
    }
    ConfirmScreen #confirm-tool {
        color: $primary;
        text-style: bold;
        height: 1;
    }
    ConfirmScreen #confirm-detail {
        height: auto;
        margin: 1 0;
        color: $foreground;
    }
    ConfirmScreen #confirm-diff {
        height: auto;
        max-height: 18;
        margin: 0 0 1 0;
        background: $background;
        scrollbar-size-vertical: 1;
    }
    ConfirmScreen #confirm-reason {
        height: 1;
        color: $text-muted;
    }
    ConfirmScreen #confirm-options {
        height: auto;
        background: $background;
    }
    ConfirmScreen #confirm-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        agent_name: str,
        tool_name: str,
        detail: str,
        reason: str = "",
        *,
        allow_always: bool = True,
        diff_text: str = "",
    ) -> None:
        super().__init__()
        self._agent_name = agent_name
        self._tool_name = tool_name
        self._detail = detail
        self._reason = reason
        self._allow_always = allow_always
        self._diff_text = diff_text

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static("Permission required", id="confirm-title")
            yield Static(
                f"{self._agent_name} · {self._tool_name}", id="confirm-tool"
            )
            yield Static(self._detail, id="confirm-detail")
            if self._diff_text:
                theme = getattr(self.app, "current_theme", None)
                dark = bool(getattr(theme, "dark", True))
                with VerticalScroll(id="confirm-diff"):
                    yield Static(render_diff(self._diff_text, dark=dark))
            if self._reason:
                yield Static(self._reason, id="confirm-reason")
            yield OptionList(id="confirm-options")
            yield Static(
                "↑/↓ move · Enter select · Esc deny", id="confirm-hint"
            )

    def on_mount(self) -> None:
        options = self.query_one("#confirm-options", OptionList)
        options.add_option(Option("Allow once", id=ALLOW))
        if self._allow_always:
            options.add_option(
                Option("Always allow this pattern (session)", id=ALWAYS)
            )
        options.add_option(Option("Deny", id=DENY))
        self._focus_list(options)

    def action_deny(self) -> None:
        self.dismiss(DENY)
