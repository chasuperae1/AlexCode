"""Live subagent / shell runs shown at the right of the input status bar."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from alex.core.formatting import format_duration  # noqa: F401 (re-exported)

SHELL_TOOLS = frozenset({"Bash", "Cmd", "PowerShell", "Shell"})
LONG_RUNNING_SECONDS = 300.0
MIN_VISIBLE_SECONDS = 1.0


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
