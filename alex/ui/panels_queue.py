"""队列卡片与按钮。"""

from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, Static

from alex.ui.constants import QUEUE_CARD_LIMIT
from alex.ui.sources import format_bytes
from alex.ui.widgets import ChangesSummary, UserMessage



class QueueButton(Static):
    """队列卡片底部的可点击动作。"""

    class Clicked(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, label: str, action: str, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._action = action

    def on_click(self, event) -> None:
        self.post_message(self.Clicked(self._action))
        event.stop()


class QueueCard(Vertical):
    """任务队列卡片：确认、留档、结果三种形态共用一套布局。"""

    DEFAULT_CSS = """
    QueueCard {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        background: $background;
        border: round $primary 35%;
    }
    QueueCard .q-head { height: 1; }
    QueueCard .q-title { width: auto; }
    QueueCard .q-gap { width: 1fr; height: 1; }
    QueueCard .q-meta { width: auto; }
    QueueCard .q-hint { height: 1; color: $text-muted; padding-left: 3; }
    QueueCard .q-rows { height: auto; }
    QueueCard .q-warn { height: auto; }
    QueueCard .q-foot { height: 1; margin-top: 1; }
    QueueCard .q-act { width: auto; }
    QueueCard .q-key { width: auto; color: $text-muted; }
    QueueCard .q-sep { width: auto; color: $text-muted; }
    QueueCard .q-note { width: auto; color: $text-muted; }
    QueueCard .q-start { color: $primary; text-style: underline; }
    QueueCard .q-cancel { color: $text-muted; text-style: underline; }
    """

    def __init__(
        self,
        tasks: list[str],
        state: str = "confirming",
        results: list | None = None,
        seconds: int = 0,
        theme=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._tasks = [str(item) for item in tasks]
        self._results = results
        self._state = state
        self._seconds = seconds
        self._theme = theme
        self._offset = 0
        self._limit = QUEUE_CARD_LIMIT
        self._armed = False
        self._title = Static(classes="q-title")
        self._meta = Static(classes="q-meta")
        self._top = Static(classes="q-hint")
        self._rows = Static(classes="q-rows")
        self._bottom = Static(classes="q-hint")
        self._warn = Static(classes="q-warn")
        self._start = QueueButton("Start", "start", classes="q-act q-start")
        self._start_key = Static("(enter)", classes="q-key")
        self._sep = Static("·", classes="q-sep")
        self._cancel = QueueButton("Cancel", "cancel", classes="q-act q-cancel")
        self._cancel_key = Static("(esc)", classes="q-key")
        self._note = Static("", classes="q-note")
        self._foot = Horizontal(classes="q-foot")
        self._refresh()

    @property
    def state(self) -> str:
        return self._state

    def compose(self) -> ComposeResult:
        with Horizontal(classes="q-head"):
            yield self._title
            yield Static("", classes="q-gap")
            yield self._meta
        yield self._top
        yield self._rows
        yield self._bottom
        yield self._warn
        with self._foot:
            yield self._start
            yield self._start_key
            yield self._sep
            yield self._cancel
            yield self._cancel_key
            yield Static("", classes="q-gap")
            yield self._note

    def set_state(self, state: str) -> None:
        self._state = state
        if state != "confirming":
            self._armed = False
        self._refresh()

    def set_armed(self, armed: bool) -> None:
        if self._state != "confirming" or self._results is not None:
            return
        self._armed = armed
        self._refresh()

    def shift(self, delta: int) -> None:
        if self._results is not None or len(self._tasks) <= self._limit:
            return
        limit = max(0, len(self._tasks) - self._limit)
        offset = max(0, min(limit, self._offset + delta))
        if offset == self._offset:
            return
        self._offset = offset
        self._render_rows()

    def on_mouse_scroll_down(self, event) -> None:
        self.shift(1)
        event.stop()

    def on_mouse_scroll_up(self, event) -> None:
        self.shift(-1)
        event.stop()

    # ---------------------------------------------------------------- 渲染
    def _refresh(self) -> None:
        theme = self._theme
        count = len(self._results) if self._results is not None else len(self._tasks)
        suffix = "task" if count == 1 else "tasks"
        self._title.update(
            Text(f"Queue · {count} {suffix}", style=f"bold {theme.primary}")
        )
        self._meta.update(Text(self._meta_text(), style=theme.secondary))
        self._render_rows()
        self._render_footer()

    def _meta_text(self) -> str:
        if self._results is not None:
            done = sum(1 for item in self._results if item[1] == "done")
            failed = sum(1 for item in self._results if item[1] == "failed")
            skipped = sum(1 for item in self._results if item[1] == "skipped")
            text = f"FINISHED · {done} DONE"
            if failed:
                text += f" · {failed} FAILED"
            if skipped:
                text += f" · {skipped} SKIPPED"
            return text
        if self._state == "confirming":
            return "CANCEL REQUESTED" if self._armed else "AWAITING CONFIRMATION"
        return {
            "started": "STARTED",
            "cancelled": "NOT STARTED",
            "done": "DONE",
        }.get(self._state, "")

    def _render_rows(self) -> None:
        theme = self._theme
        if self._results is not None:
            text = Text()
            for index, item in enumerate(self._results, 1):
                subject = str(item[0])
                status = str(item[1])
                reason = str(item[2]) if len(item) > 2 else ""
                if index > 1:
                    text.append("\n")
                if status == "done":
                    text.append("✓ ", style=f"bold {theme.success}")
                    subject_style = theme.foreground
                elif status == "failed":
                    text.append("✗ ", style=f"bold {theme.error}")
                    subject_style = theme.error
                else:
                    text.append("– ", style=theme.secondary)
                    subject_style = theme.secondary
                text.append(f"{index:>2}  ", style=theme.secondary)
                text.append(subject, style=subject_style)
                if reason and status == "failed":
                    text.append(f" — {reason}", style=theme.secondary)
            self._rows.update(text)
            self._top.display = False
            self._bottom.display = False
            return

        total = len(self._tasks)
        scrollable = total > self._limit
        start = self._offset if scrollable else 0
        visible = (
            self._tasks[start : start + self._limit]
            if scrollable
            else self._tasks
        )
        text = Text()
        for index, subject in enumerate(visible, start=start + 1):
            if index > start + 1:
                text.append("\n")
            text.append(f"{index:>2}  ", style=theme.secondary)
            text.append(subject, style=theme.foreground)
        self._rows.update(text)

        above = start
        below = total - (start + len(visible))
        self._top.display = scrollable and above > 0
        self._bottom.display = scrollable and below > 0
        self._top.update(
            Text(f"↑ {above} more above", style=theme.secondary)
            if above
            else Text("")
        )
        self._bottom.update(
            Text(f"↓ {below} more below", style=theme.secondary)
            if below
            else Text("")
        )

    def _render_footer(self) -> None:
        theme = self._theme
        confirming = self._state == "confirming" and self._results is None
        for widget in (
            self._start,
            self._start_key,
            self._sep,
            self._cancel,
            self._cancel_key,
        ):
            widget.display = confirming
        self._warn.display = confirming and self._armed
        if confirming:
            self._warn.update(
                Text(
                    f"Discarding clears these {len(self._tasks)} tasks. "
                    "They cannot be resumed.",
                    style=theme.warning,
                )
            )
            if self._armed:
                self._cancel.update(Text("Discard", style=f"underline {theme.warning}"))
                self._cancel_key.update("(esc again)")
            else:
                self._cancel.update(Text("Cancel", style=f"underline {theme.secondary}"))
                self._cancel_key.update("(esc)")

        note = ""
        if self._results is not None:
            note = f"total {self._format_seconds(self._seconds)}"
        elif self._state == "started":
            note = "esc to stop"
        elif self._state == "cancelled":
            note = "nothing was run"
        self._note.update(note)
        self._foot.display = confirming or bool(note)

    @staticmethod
    def _format_seconds(total: int) -> str:
        total = max(0, int(total))
        if total < 60:
            return f"{total}s"
        minutes, seconds = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m {seconds:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"
