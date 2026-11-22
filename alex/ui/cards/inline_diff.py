"""整轮 diff 卡：默认收起，展开后封顶滚动。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from alex.core.snapshots import Checkpoint
from alex.screens.diff import diff_palette, parse_diff

from alex.ui.cards.base import HeadToggleMixin


class InlineDiffCard(HeadToggleMixin, Vertical):
    """一轮回复后的整轮 diff（历史回放用）：默认收起，展开后封顶滚动。"""

    can_focus = True

    BINDINGS = [
        Binding("space", "toggle", "Expand", show=False),
        Binding("enter", "toggle", "Expand", show=False),
    ]

    DEFAULT_CSS = """
    InlineDiffCard {
        height: auto;
        margin: 0 0 1 2;
        padding: 0 1;
        background: transparent;
        border: round $primary 35%;
    }
    InlineDiffCard .diff-head { height: 1; }
    InlineDiffCard .diff-head:hover { background: $boost; }
    InlineDiffCard .diff-title { width: 1fr; }
    InlineDiffCard .diff-count { width: auto; }
    InlineDiffCard .diff-hint { width: auto; color: $text-muted; }
    InlineDiffCard .diff-wrap { height: auto; max-height: 11; display: none; }
    InlineDiffCard.-open .diff-wrap { display: block; }
    InlineDiffCard .diff-legend { height: 1; color: $text-muted; }
    InlineDiffCard .diff-body { height: auto; padding: 0 0 1 0; }
    InlineDiffCard:focus, InlineDiffCard:focus-within { border: round $primary; }
    """

    def __init__(self, checkpoint: Checkpoint, theme, **kwargs) -> None:
        super().__init__(classes="inline-diff-card", **kwargs)
        self._checkpoint = checkpoint
        self._theme = theme
        self._open = False
        self._title = Static(classes="diff-title")
        self._count = Static(classes="diff-count")
        self._hint = Static(self._hint_text(), classes="diff-hint")
        self._head = Horizontal(
            self._title, self._count, self._hint, classes="diff-head"
        )
        self._wrap = VerticalScroll(classes="diff-wrap")
        self._body = Static(
            Text("Loading diff…", style=self._theme.secondary), classes="diff-body"
        )
        self._refresh_header()

    def _hint_text(self) -> Text:
        label = "Space Collapse" if self._open else "Space Expand"
        return Text(label, style=self._theme.secondary)

    def compose(self) -> ComposeResult:
        palette = diff_palette(self._theme.dark)
        yield self._head
        with self._wrap:
            legend = Text()
            legend.append("− deleted", style=palette.del_fg)
            legend.append("   ")
            legend.append("+ added", style=palette.add_fg)
            legend.append("   ·   ", style=palette.meta)
            legend.append("line numbers: before / after", style=palette.meta)
            yield Static(legend, classes="diff-legend")
            yield self._body

    def _refresh_header(self) -> None:
        checkpoint = self._checkpoint
        count = len(checkpoint.files)
        title = Text("▤  File changes", style=f"bold {self._theme.primary}")
        title.append(
            f" · {count} file{'s' if count != 1 else ''}",
            style=self._theme.secondary,
        )
        counts = Text()
        counts.append(f"+{checkpoint.additions}", style=self._theme.success)
        counts.append("  ")
        counts.append(f"−{checkpoint.deletions}", style=self._theme.error)
        self._title.update(title)
        self._count.update(counts)

    def set_diff(self, text: str) -> None:
        files = parse_diff(text)
        if not files:
            self._body.update(
                Text("No text changes (binary or empty).", style=self._theme.secondary)
            )
            return

        result = Text()
        palette = diff_palette(self._theme.dark)
        for file_index, item in enumerate(files):
            if file_index:
                result.append("\n")

            file_head = Text()
            file_head.append(f"▤  {item.path or '(unknown)'}", style=f"bold {self._theme.primary}")
            file_head.append(
                f"   +{item.additions}  −{item.deletions}", style=palette.meta
            )
            result.append_text(file_head)

            if item.binary:
                result.append("\n")
                result.append(Text("  binary file", style=f"italic {palette.meta}"))
                continue

            max_no = max(
                (
                    max(entry.old_no or 0, entry.new_no or 0)
                    for entry in item.lines
                ),
                default=1,
            )
            number_width = max(3, len(str(max_no)))
            for entry in item.lines:
                result.append("\n")
                if entry.kind == "hunk":
                    line = Text(entry.text, style=f"bold {palette.hunk}")
                elif entry.kind == "meta":
                    line = Text(entry.text, style=f"italic {palette.meta}")
                else:
                    old = (
                        f"{entry.old_no:>{number_width}}"
                        if entry.old_no is not None
                        else " " * number_width
                    )
                    new = (
                        f"{entry.new_no:>{number_width}}"
                        if entry.new_no is not None
                        else " " * number_width
                    )
                    if entry.kind == "add":
                        line = Text(style=f"{palette.add_fg} on {palette.add_bg}")
                        line.append(
                            f"{old} {new}  + ",
                            style=f"{palette.gutter} on {palette.add_bg}",
                        )
                        line.append(
                            entry.text,
                            style=f"{palette.add_fg} on {palette.add_bg}",
                        )
                    elif entry.kind == "del":
                        line = Text(style=f"{palette.del_fg} on {palette.del_bg}")
                        line.append(
                            f"{old} {new}  − ",
                            style=f"{palette.gutter} on {palette.del_bg}",
                        )
                        line.append(
                            entry.text,
                            style=f"{palette.del_fg} on {palette.del_bg}",
                        )
                    else:
                        line = Text(style=palette.context)
                        line.append(f"{old} {new}    {entry.text}")
                result.append_text(line)

        self._body.update(result)

    def set_error(self, message: str) -> None:
        self._body.update(Text(f"Could not load diff: {message}", style=self._theme.error))

    def action_toggle(self) -> None:
        self._open = not self._open
        self.set_class(self._open, "-open")
        self._hint.update(self._hint_text())
        if self._open:
            self._wrap.scroll_home(animate=False)
            self._wrap.focus()
        else:
            self.focus()
