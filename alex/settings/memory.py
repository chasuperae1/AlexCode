"""设置界面分包：记忆分区：统计行、条目/课程/日志行，以及打开的详情。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from alex.settings.constants import _format_size
from alex.settings.widgets import MemoryRow


class MemoryMixin:
    """记忆分区：统计行、条目/课程/日志行，以及打开的详情。"""

    def _memory_stats(self) -> ComposeResult:
        long_term = self._memory.get("long_term") or {}
        lessons = self._memory.get("lessons") or {}
        daily = self._memory.get("daily") or {}

        if long_term.get("exists"):
            lt_value = f"{int(long_term.get('count') or 0)} entries"
            lt_sub = f"MEMORY.md · {_format_size(long_term.get('size'))}"
            if long_term.get("updated"):
                lt_sub += f" · updated {long_term['updated']}"
        else:
            lt_value = "Empty"
            lt_sub = "MEMORY.md not found"

        if lessons.get("exists"):
            ls_value = f"{int(lessons.get('count') or 0)} lessons"
            ls_sub = f"LESSONS.md · {_format_size(lessons.get('size'))}"
            if lessons.get("updated"):
                ls_sub += f" · updated {lessons['updated']}"
        else:
            ls_value = "Empty"
            ls_sub = "LESSONS.md not created"

        days = int(daily.get("days") or 0)
        if days:
            d_value = f"{days} days"
            d_sub = (
                f"latest {daily.get('latest') or '—'} · "
                f"{int(daily.get('latest_count') or 0)} entries"
            )
        else:
            d_value = "Empty"
            d_sub = "No daily logs yet"

        stats = (
            ("LONG-TERM MEMORY", lt_value, lt_sub, not long_term.get("exists")),
            ("LESSONS", ls_value, ls_sub, not lessons.get("exists")),
            ("DAILY LOGS", d_value, d_sub, not days),
        )
        for label, value, sub, missing in stats:
            classes = "settings-memory-stat"
            if missing:
                classes += " -missing"
            with Vertical(classes=classes):
                yield Static(label, classes="settings-memory-label")
                yield Static(value, classes="settings-memory-value")
                yield Static(sub, classes="settings-memory-sub")

    def _memory_entry_row(self, index: int, entry: dict) -> ComposeResult:
        with MemoryRow(id=f"memory-entry-{index}", classes="settings-memory-row"):
            with Vertical(classes="settings-memory-text"):
                yield Static(
                    str(entry.get("title") or "Untitled"),
                    classes="settings-memory-name",
                )
                yield Static(
                    str(entry.get("snippet") or "—"),
                    classes="settings-memory-snip",
                )
            yield Static(
                f"{entry.get('date') or '—'} · {_format_size(entry.get('size'))}",
                classes="settings-memory-meta",
            )
            yield Static("⛶", classes="settings-memory-view")

    def _memory_log_row(self, index: int, log: dict) -> ComposeResult:
        with MemoryRow(id=f"memory-log-{index}", classes="settings-memory-row"):
            with Vertical(classes="settings-memory-text"):
                yield Static(
                    str(log.get("date") or log.get("name") or "Log"),
                    classes="settings-memory-name",
                )
                yield Static(
                    f"{int(log.get('count') or 0)} entries",
                    classes="settings-memory-snip",
                )
            yield Static(
                _format_size(log.get("size")),
                classes="settings-memory-meta",
            )
            yield Static("⛶", classes="settings-memory-view")

    def _memory_entries_label(self) -> str:
        total = len(self._memory.get("entries") or [])
        return f"{total} entries"

    def _memory_lesson_row(self, index: int, lesson: dict) -> ComposeResult:
        with MemoryRow(id=f"memory-lesson-{index}", classes="settings-memory-row"):
            with Vertical(classes="settings-memory-text"):
                yield Static(
                    str(lesson.get("title") or "Untitled"),
                    classes="settings-memory-name",
                )
                yield Static(
                    str(lesson.get("snippet") or "—"),
                    classes="settings-memory-snip",
                )
            yield Static(
                f"{lesson.get('date') or '—'} · {_format_size(lesson.get('size'))}",
                classes="settings-memory-meta",
            )
            yield Static("⛶", classes="settings-memory-view")

    def _lessons_label(self) -> str:
        lessons = self._memory.get("lessons") or {}
        total = len(lessons.get("items") or []) or int(lessons.get("count") or 0)
        return f"LESSONS.md · {total} lessons"

    def _memory_logs_label(self) -> str:
        return f"memory/ · {len(self._memory.get('logs') or [])} files"

    def _open_memory_entry(self, index: int) -> None:
        entries = self._memory.get("entries") or []
        if not 0 <= index < len(entries):
            return
        entry = entries[index]
        name = str(entry.get("title") or "Untitled")
        status = Text(
            f"{entry.get('date') or '—'} · {_format_size(entry.get('size'))}",
            style="dim",
        )
        self._detail_open = ("memory", index)
        self._show_doc(
            "Memory", name, "MEMORY.md", status, str(entry.get("content") or "")
        )

    def _open_memory_lesson(self, index: int) -> None:
        items = (self._memory.get("lessons") or {}).get("items") or []
        if not 0 <= index < len(items):
            return
        lesson = items[index]
        name = str(lesson.get("title") or "Lesson")
        status = Text(
            f"{lesson.get('date') or '—'} · {_format_size(lesson.get('size'))}",
            style="dim",
        )
        self._detail_open = ("lesson", index)
        self._show_doc(
            "Memory", name, "LESSONS.md", status, str(lesson.get("content") or "")
        )

    def _open_memory_log(self, index: int) -> None:
        logs = self._memory.get("logs") or []
        if not 0 <= index < len(logs):
            return
        log = logs[index]
        name = str(log.get("date") or log.get("name") or "Daily log")
        status = Text(
            f"{int(log.get('count') or 0)} entries · {_format_size(log.get('size'))}",
            style="dim",
        )
        self._detail_open = ("log", index)
        self._show_doc(
            "Memory",
            name,
            str(log.get("name") or "log.md"),
            status,
            str(log.get("content") or "") or "_No content._",
        )

    def on_memory_row_opened(self, event: MemoryRow.Opened) -> None:
        widget_id = event.row.id or ""
        if widget_id.startswith("memory-entry-"):
            self._open_memory_entry(
                int(widget_id.removeprefix("memory-entry-"))
            )
        elif widget_id.startswith("memory-log-"):
            self._open_memory_log(int(widget_id.removeprefix("memory-log-")))
        elif widget_id.startswith("memory-lesson-"):
            self._open_memory_lesson(
                int(widget_id.removeprefix("memory-lesson-"))
            )

