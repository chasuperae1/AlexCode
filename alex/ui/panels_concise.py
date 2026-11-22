"""简洁模式的单轮汇总卡。"""

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



class ConciseSummary(Vertical):
    """简洁模式的一轮汇总卡：运行中一行，结束折叠成可展开的摘要。"""

    DEFAULT_CSS = """
    ConciseSummary {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        background: transparent;
        border: round $primary 22%;
    }
    ConciseSummary.-fail {
        border: round $error 45%;
    }
    ConciseSummary .concise-head {
        height: 1;
        width: 1fr;
    }
    ConciseSummary .concise-chev {
        width: 2;
        color: $text-muted;
    }
    ConciseSummary .concise-title {
        width: auto;
        text-style: bold;
    }
    ConciseSummary .concise-meta {
        width: 1fr;
        padding: 0 1;
        color: $text-muted;
    }
    ConciseSummary .concise-state {
        width: auto;
        text-style: bold;
    }
    ConciseSummary .concise-body {
        height: auto;
        padding: 1 0 0 2;
    }
    """

    _BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    @property
    def state(self) -> str:
        return self._state

    def __init__(self, theme, **kwargs) -> None:
        super().__init__(**kwargs)
        self._theme = theme
        self._state = "running"
        self._steps = 0
        self._seconds = 0.0
        self._files = 0
        self._thinking_segments = 0
        self._thinking_chars = 0
        self._tools: dict[str, int] = {}
        self._diffs: list[tuple[str, int, int]] = []
        self._changes: tuple[int, int, int] | None = None
        self._errors: list[str] = []
        self._rules_count = 0
        self._rules_size = 0
        self._skills: dict[str, tuple[int, float]] = {}
        self._activity = ""
        self._expanded = False
        self._started = time.monotonic()
        self._timer = None
        self._chev = Static("▸", classes="concise-chev")
        self._title = Static("", classes="concise-title")
        self._meta = Static("", classes="concise-meta")
        self._state_label = Static("running", classes="concise-state")
        self._head = Horizontal(
            self._chev,
            self._title,
            self._meta,
            self._state_label,
            classes="concise-head",
        )
        self._body = Static("", classes="concise-body")
        self._body.display = False
        self._refresh()

    def compose(self) -> ComposeResult:
        yield self._head
        yield self._body

    def on_mount(self) -> None:
        self._sync_timer()
        self._refresh()

    def on_unmount(self) -> None:
        self._stop_timer()

    # ------------------------------------------------------------ 数据更新
    def step(self, activity: str = "") -> None:
        self._steps += 1
        if activity:
            self._activity = activity
        self._refresh()

    def set_activity(self, activity: str) -> None:
        self._activity = activity
        self._refresh()

    def add_thinking(self, chars: int, *, count_step: bool = False) -> None:
        self._thinking_segments += 1
        self._thinking_chars += max(0, int(chars))
        if count_step:
            self._steps += 1
        self._refresh()

    def add_tool(self, name: str, ok: bool, text: str) -> None:
        name = (name or "Tool").strip() or "Tool"
        self._tools[name] = self._tools.get(name, 0) + 1
        if not ok and text:
            self._errors.append(text)
            self._expanded = True
        self._refresh()

    def add_diff(
        self, path: str, additions: int, deletions: int, files: int = 1
    ) -> None:
        self._diffs.append((path, additions, deletions))
        self._files += max(0, int(files))
        self._activity = f"editing {Path(path).name}"
        self._refresh()

    def finish(self, duration: float) -> None:
        if self._errors:
            self._state = "failed"
        elif self._state != "failed":
            self._state = "done"
        self._seconds = max(0.0, float(duration))
        self._expanded = bool(self._errors)
        self._stop_timer()
        self._refresh()

    def set_changes(self, files: int, additions: int, deletions: int) -> None:
        """整轮改动汇总：文件数 + 增删行，写进 Changes 行。"""
        self._changes = (int(files), int(additions), int(deletions))
        self._files = max(self._files, int(files))
        self._refresh()

    def set_rules(self, count: int, size: int) -> None:
        """本次会话已载入的规则数量与总大小。"""
        self._rules_count = max(0, int(count))
        self._rules_size = max(0, int(size))
        self._refresh()

    def add_skill(self, name: str, size: int, seconds: float = 0.0) -> None:
        """记录一次 Skill 正文读取；同一技能重复读取只保留最后一次耗时。"""
        name = (name or "").strip()
        if not name:
            return
        self._skills[name] = (max(0, int(size)), max(0.0, float(seconds)))
        self._refresh()

    def fail(self, error: str = "") -> None:
        self._state = "failed"
        if error:
            self._errors.append(error)
        self._expanded = True
        self._stop_timer()
        self._refresh()

    # ------------------------------------------------------------ 渲染
    def _sync_timer(self) -> None:
        if self._state == "running" and self._timer is None:
            self._timer = self.set_interval(0.1, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def _elapsed(self) -> float:
        if self._state == "done":
            return self._seconds
        return max(0.0, time.monotonic() - self._started)

    def _sources_meta(self) -> str:
        bits: list[str] = []
        if self._rules_count:
            suffix = "s" if self._rules_count != 1 else ""
            bits.append(f"{self._rules_count} rule{suffix}")
        if self._skills:
            bits.append(", ".join(self._skills))
        return " · ".join(bits)

    def _tick(self) -> None:
        if self._state != "running":
            self._stop_timer()
            return
        self._refresh()

    def toggle(self) -> None:
        if not self._body_text():
            return
        self._expanded = not self._expanded
        self._refresh()

    def _refresh(self) -> None:
        theme = self._theme
        elapsed = self._elapsed()
        title = f"{self._steps} step{'s' if self._steps != 1 else ''}"
        source_meta = self._sources_meta()
        if self._state == "running":
            frame = self._BRAILLE[int(elapsed * 10) % len(self._BRAILLE)]
            title = f"{frame} {title}" if self._steps else f"{frame} working"
            bits: list[str] = []
            if source_meta:
                bits.append(source_meta)
            if self._steps == 0:
                bits.append("starting")
            else:
                bits.append(f"{elapsed:.1f}s")
            if self._activity:
                bits.append(self._activity)
            meta = "· " + " · ".join(bits)
            state_text = "running"
            state_style = f"bold {theme.secondary}"
        elif self._state == "failed":
            meta = "· failed"
            if source_meta:
                meta = f"· {source_meta} · failed"
            state_text = "failed"
            state_style = f"bold {theme.error}"
        else:
            bits = []
            if source_meta:
                bits.append(source_meta)
            bits.append(self._format_seconds(elapsed))
            if self._files:
                bits.append(
                    f"{self._files} file"
                    f"{'s' if self._files != 1 else ''} changed"
                )
            meta = "· " + " · ".join(bits)
            state_text = "done"
            state_style = f"bold {theme.success}"
        self._title.update(Text(title, style=f"bold {theme.primary}"))
        self._meta.update(meta)
        self._state_label.update(Text(state_text, style=state_style))
        self._chev.update("▾" if self._expanded else "▸")
        self._body.display = self._expanded and bool(self._body_text())
        if self._body.display:
            self._body.update(self._body_text())
        self.set_class(self._state == "failed", "-fail")
        self.set_class(self._state == "running", "-running")

    def _body_text(self) -> Text:
        theme = self._theme
        rows: list[Text] = []
        if self._rules_count:
            rows.append(
                self._row(
                    "Rules",
                    f"{self._rules_count} loaded · "
                    f"{format_bytes(self._rules_size)}",
                )
            )
        if self._skills:
            detail = " · ".join(
                f"{name} {format_bytes(size)}"
                for name, (size, _seconds) in self._skills.items()
            )
            rows.append(self._row("Skills", detail))
        if self._thinking_segments:
            rows.append(
                self._row(
                    "Thinking",
                    f"{self._thinking_segments} segment"
                    f"{'s' if self._thinking_segments != 1 else ''} · "
                    f"{self._thinking_chars:,} chars",
                )
            )
        for name, count in sorted(self._tools.items()):
            if self._skills and name == "Skill":
                continue
            if self._diffs and name in {"Edit", "Write"}:
                continue
            rows.append(
                self._row(name, f"{count} call{'s' if count != 1 else ''}")
            )
        for path, additions, deletions in self._diffs:
            rows.append(
                self._row(
                    "Edit",
                    f"{Path(path).name} +{additions} -{deletions}",
                )
            )
        if self._changes is not None:
            files, additions, deletions = self._changes
            rows.append(
                self._row(
                    "Changes",
                    f"{files} file{'s' if files != 1 else ''} changed · "
                    f"+{additions} -{deletions}",
                )
            )
        for error in self._errors[-3:]:
            row = Text()
            row.append("✗ ", style=f"bold {theme.error}")
            row.append(error, style=theme.error)
            rows.append(row)
        if not rows:
            return Text("")
        text = Text()
        for index, row in enumerate(rows):
            if index:
                text.append("\n")
            text.append_text(row)
        return text

    def _row(self, label: str, detail: str) -> Text:
        theme = self._theme
        row = Text()
        row.append(f"{label:<11}", style=theme.secondary)
        row.append(detail, style=theme.foreground)
        return row

    @staticmethod
    def _format_seconds(total: float) -> str:
        total = max(0, int(round(total)))
        if total < 60:
            return f"{total}s"
        minutes, seconds = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m{seconds:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h{minutes:02d}m"

    def on_click(self, event) -> None:
        node = event.widget
        while node is not None and node is not self:
            if node is self._head:
                event.stop()
                self.toggle()
                return
            node = node.parent
