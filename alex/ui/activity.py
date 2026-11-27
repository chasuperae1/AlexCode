"""Live subagent / shell runs shown at the right of the input status bar."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text
from textual.widgets import Static

from alex.core.formatting import format_duration  # noqa: F401 (re-exported)

SHELL_TOOLS = frozenset({"Bash", "Cmd", "PowerShell", "Shell"})
LONG_RUNNING_SECONDS = 300.0
MIN_VISIBLE_SECONDS = 1.0


def subagent_icon(record: dict[str, Any]) -> str:
    if record.get("waiting"):
        return "▲"
    status = str(record.get("status") or "running")
    if status == "running":
        return "◐"
    if status == "completed":
        return "✓"
    return "✗"


def subagent_row_text(record: dict[str, Any], theme) -> Text:
    """父会话里的一条子代理内联行：图标 + 名称 + 任务 + 状态。"""
    waiting = bool(record.get("waiting"))
    status = str(record.get("status") or "running")
    line = Text()
    icon = subagent_icon(record)
    tone = (
        f"bold {theme.warning}"
        if waiting
        else f"bold {theme.success}"
        if status == "completed"
        else f"bold {theme.error}"
        if status in {"failed", "interrupted"}
        else f"bold {theme.primary}"
    )
    line.append(f"{icon} ", style=tone)
    line.append("Subagent · ", style=theme.secondary)
    line.append(str(record.get("label") or "Subagent"), style=f"bold {theme.foreground}")
    if waiting:
        tool = str(record.get("waiting_tool") or "approval")
        line.append(f" · requests {tool}", style=theme.warning)
        line.append(" · awaiting approval", style=theme.secondary)
    elif str(record.get("description") or ""):
        desc = str(record.get("description") or "")
        if len(desc) > 80:
            desc = desc[:79] + "…"
        line.append(f" · {desc}", style=theme.secondary)
    if status == "running" and not waiting:
        line.append(" …", style=theme.secondary)
    else:
        seconds = float(record.get("elapsed") or 0.0)
        if not seconds and status == "running":
            started = float(record.get("started_at") or 0.0)
            if started:
                seconds = max(0.0, time.monotonic() - started)
        if seconds:
            line.append(f" · {format_duration(seconds)}", style=theme.secondary)
    return line


def _clip_line(text: object, limit: int) -> str:
    """单行化并截断：终端里所有行都必须压成一行，避免换行把列表撑乱。"""
    value = " ".join(str(text or "").split())
    if len(value) > limit:
        return value[: max(1, limit - 1)] + "…"
    return value


def subagent_panel_text(record: dict[str, Any], theme) -> Text:
    """浮层一行：状态 + 名称 + 耗时/工具数 + 任务摘要，严格单行。"""
    waiting = bool(record.get("waiting"))
    status = str(record.get("status") or "running")
    line = Text()
    icon = subagent_icon(record)
    tone = (
        f"bold {theme.warning}"
        if waiting
        else f"bold {theme.success}"
        if status == "completed"
        else f"bold {theme.error}"
        if status in {"failed", "interrupted"}
        else f"bold {theme.primary}"
    )
    line.append(f"{icon} ", style=tone)
    line.append(_clip_line(record.get("label"), 18), style=f"bold {theme.foreground}")
    if waiting:
        tool = str(record.get("waiting_tool") or "approval")
        line.append(f" · awaiting {tool}", style=theme.warning)
    elif status == "running":
        started = float(record.get("started_at") or 0.0)
        elapsed = (
            max(0.0, time.monotonic() - started)
            if started
            else float(record.get("elapsed") or 0.0)
        )
        line.append(f" · {format_duration(elapsed)}", style=theme.secondary)
    elif status == "completed":
        line.append(" · done", style=theme.success)
        seconds = float(record.get("elapsed") or 0.0)
        if seconds:
            line.append(f" · {format_duration(seconds)}", style=theme.secondary)
    elif status == "interrupted":
        line.append(" · interrupted", style=theme.error)
    else:
        line.append(" · failed", style=theme.error)
    steps = int(record.get("steps") or 0)
    if steps:
        line.append(f" · {steps} tool{'s' if steps != 1 else ''}", style=theme.secondary)
    return line


class SubagentRow(Static):
    """父会话里的子代理内联行；点击进入子会话整页。"""

    def __init__(self, record: dict[str, Any], theme) -> None:
        super().__init__(subagent_row_text(record, theme), classes="subagent-row")
        self.subagent_id = str(record.get("id") or "")
        self.can_focus = False

    def update_record(self, record: dict[str, Any], theme) -> None:
        self.update(subagent_row_text(record, theme))


class SubagentNotice(Static):
    """等待权限时父会话里的一行提示；点击进入子会话确认。"""

    def __init__(self, record: dict[str, Any], payload: dict[str, Any], theme) -> None:
        self.subagent_id = str(record.get("id") or "")
        self.request_id = str(payload.get("request_id") or "")
        self.can_focus = False
        logo = record.get("waiting_tool") or payload.get("tool") or "tool"
        command = str(payload.get("command") or "").strip()
        detail = str(payload.get("detail") or "").strip()
        target = command or (detail[9:] if detail.startswith("command: ") else detail)
        text = Text()
        text.append("▲ ", style=f"bold {theme.warning}")
        text.append(
            str(record.get("label") or "Subagent"), style=f"bold {theme.warning}"
        )
        text.append(" · requests to run ", style=theme.secondary)
        text.append(target[:120] or logo, style=theme.foreground)
        text.append(" · ↵ review", style=theme.secondary)
        super().__init__(text, classes="subagent-notice")


def subagent_chip_text(records: list[dict[str, Any]], theme) -> Text | None:
    """右下角入口：等待授权 > 运行中 > 已完成。"""
    if not records:
        return None
    waiting = next((item for item in records if item.get("waiting")), None)
    if waiting is not None:
        line = Text()
        line.append("▲ ", style=f"bold {theme.warning}")
        line.append(
            f"{waiting.get('label') or 'Subagent'} needs approval",
            style=f"bold {theme.warning}",
        )
        line.append(" ▾", style=theme.secondary)
        return line
    running = [item for item in records if item.get("status") == "running"]
    line = Text()
    if running:
        line.append("◐ ", style=f"bold {theme.primary}")
        line.append(
            f"{len(running)} subagent{'s' if len(running) != 1 else ''}",
            style=f"bold {theme.foreground}",
        )
    else:
        line.append("✓ ", style=theme.secondary)
        line.append(
            f"{len(records)} subagent{'s' if len(records) != 1 else ''}",
            style=theme.secondary,
        )
    line.append(" ▾", style=theme.secondary)
    return line


@dataclass(frozen=True)
class ActivitySummary:
    """Snapshot of the currently running subagents and shell commands."""

    subagents: int = 0
    shells: int = 0
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return self.subagents + self.shells

    @property
    def long_running(self) -> bool:
        return self.elapsed >= LONG_RUNNING_SECONDS


@dataclass
class _Run:
    name: str
    kind: str
    started: float


@dataclass
class ActivityTracker:
    """Track live tool calls by id and collapse them into one status line."""

    _runs: dict[str, _Run] = field(default_factory=dict)

    def start(self, tool_id: str, name: str, *, now: float | None = None) -> None:
        tool_id = str(tool_id or "")
        if not tool_id:
            return
        if name == "Subagent":
            kind = "subagent"
        elif name in SHELL_TOOLS:
            kind = "shell"
        else:
            return
        self._runs[tool_id] = _Run(
            name=name,
            kind=kind,
            started=time.monotonic() if now is None else float(now),
        )

    def finish(self, tool_id: str) -> None:
        self._runs.pop(str(tool_id or ""), None)

    def clear(self) -> None:
        self._runs.clear()

    def summary(self, *, now: float | None = None) -> ActivitySummary:
        if not self._runs:
            return ActivitySummary()
        current = time.monotonic() if now is None else float(now)
        subagents = sum(1 for run in self._runs.values() if run.kind == "subagent")
        shells = sum(1 for run in self._runs.values() if run.kind == "shell")
        elapsed = max(max(0.0, current - run.started) for run in self._runs.values())
        return ActivitySummary(
            subagents=subagents,
            shells=shells,
            elapsed=elapsed,
        )
