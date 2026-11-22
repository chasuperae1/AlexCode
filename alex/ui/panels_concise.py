"""简洁模式的单轮汇总卡：静态状态行，不放命令与输出正文。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static
from termui.widgets import SPINNER_FRAMES

from alex.core.formatting import format_duration
from alex.ui.activity import SHELL_TOOLS
from alex.ui.sources import format_bytes
from alex.ui.timing import ensure_interval, stop_timer


class ConciseSummary(Vertical):
    """简洁模式的一轮汇总卡。

    头部只显示 steps / rules / 状态；Rules、Thinking、Shell、Diff 等细项默认
    收起，点击卡片或按 Enter / Space 展开。shell 只显示调用次数、耗时、
    输出行数与 exit code，不显示命令行和 stdout 正文。
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "toggle_body", "Expand/collapse", show=False),
        Binding("space", "toggle_body", "Expand/collapse", show=False),
    ]

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
    ConciseSummary .concise-title {
        width: auto;
        text-style: bold;
        padding: 0 0 0 1;
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
        padding: 0 0 1 2;
    }
    """

    _BRAILLE = SPINNER_FRAMES

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
        self._shells: list[dict[str, Any]] = []
        self._activity = ""
        self._started = time.monotonic()
        self._collapsed = True
        self._timer = None
        self.exchange_id: int | None = None
        self._title = Static("", classes="concise-title")
        self._meta = Static("", classes="concise-meta")
        self._state_label = Static("running", classes="concise-state")
        self._head = Horizontal(
            self._title,
            self._meta,
            self._state_label,
            classes="concise-head",
        )
        self._body = Static("", classes="concise-body")
        self._refresh()

    def compose(self) -> ComposeResult:
        yield self._head
        yield self._body

    def on_mount(self) -> None:
        self._sync_timer()
        self._refresh()

    def on_unmount(self) -> None:
        self._stop_timer()

    # ------------------------------------------------------------ 重建快照
    def snapshot_state(self) -> dict[str, Any]:
        """保存尚未落进 history 的进行中摘要，供主题切换/设置返回后重建。"""
        return {
            "exchange_id": self.exchange_id,
            "state": self._state,
            "steps": self._steps,
            "seconds": self._seconds,
            "files": self._files,
            "thinking_segments": self._thinking_segments,
            "thinking_chars": self._thinking_chars,
            "tools": dict(self._tools),
            "diffs": list(self._diffs),
            "changes": self._changes,
            "errors": list(self._errors),
            "rules_count": self._rules_count,
            "rules_size": self._rules_size,
            "skills": dict(self._skills),
            "shells": [dict(item) for item in self._shells],
            "activity": self._activity,
            "started": self._started,
            "collapsed": self._collapsed,
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        self.exchange_id = data.get("exchange_id")
        self._state = str(data.get("state") or "running")
        self._steps = int(data.get("steps", 0) or 0)
        self._seconds = float(data.get("seconds", 0.0) or 0.0)
        self._files = int(data.get("files", 0) or 0)
        self._thinking_segments = int(data.get("thinking_segments", 0) or 0)
        self._thinking_chars = int(data.get("thinking_chars", 0) or 0)
        self._tools = dict(data.get("tools") or {})
        self._diffs = [tuple(item) for item in data.get("diffs") or []]
        changes = data.get("changes")
        self._changes = tuple(changes) if changes else None
        self._errors = [str(item) for item in data.get("errors") or []]
        self._rules_count = int(data.get("rules_count", 0) or 0)
        self._rules_size = int(data.get("rules_size", 0) or 0)
        self._skills = dict(data.get("skills") or {})
        self._shells = [dict(item) for item in data.get("shells") or []]
        self._activity = str(data.get("activity") or "")
        self._started = float(data.get("started", time.monotonic()) or time.monotonic())
        self._collapsed = bool(data.get("collapsed", True))
        self._refresh()

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

    def add_tool(self, name: str, ok: bool, text: str, *, call_id: str = "") -> None:
        name = (name or "Tool").strip() or "Tool"
        if name in SHELL_TOOLS:
            # 旧历史里没有 shell_card 记录时，从结果行兜底恢复 Shell 行
            self.start_shell(name, call_id)
            seconds = self._seconds_from_text(text)
            self.finish_shell(name, ok, seconds, call_id=call_id)
            return
        self._tools[name] = self._tools.get(name, 0) + 1
        if not ok and text:
            self._errors.append(text)
        self._refresh()

    def add_diff(
        self, path: str, additions: int, deletions: int, files: int = 1
    ) -> None:
        self._diffs.append((path, additions, deletions))
        self._files += max(0, int(files))
        self._activity = f"editing {Path(path).name}"
        self._refresh()

    def finish(self, duration: float) -> None:
        if self._errors or any(
            call.get("state") == "failed" for call in self._shells
        ):
            self._state = "failed"
        elif self._state != "failed":
            self._state = "done"
        self._seconds = max(0.0, float(duration))
        self._stop_timer()
        self._refresh()

    def set_changes(self, files: int, additions: int, deletions: int) -> None:
        """整轮改动汇总：文件数 + 增删行，Diff 行没有逐条记录时兜底。"""
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
        self._stop_timer()
        self._refresh()

    # ------------------------------------------------------------ shell 状态
    def start_shell(self, name: str, call_id: str = "") -> None:
        """shell 调用开始：只记录状态，不保存命令正文。"""
        name = (name or "Shell").strip() or "Shell"
        if call_id and any(item.get("id") == call_id for item in self._shells):
            return
        self._shells.append(
            {
                "id": call_id,
                "name": name,
                "state": "running",
                "ok": True,
                "elapsed": 0.0,
                "lines": 0,
                "exit_code": None,
            }
        )
        self._refresh()

    def ensure_shell(self, name: str) -> None:
        """确保 Shell 行存在：tool_start 漏掉时由权限请求补一行。"""
        name = (name or "Shell").strip() or "Shell"
        if self._find_shell(name, states=("running", "pending")) is not None:
            return
        self.start_shell(name)

    def wait_shell(self, name: str = "") -> None:
        """权限卡出现：把对应的 shell 调用标成等待批准。"""
        call = self._find_shell(name, states=("running",))
        if call is not None:
            call["state"] = "pending"
            self._refresh()

    def allow_shell(self, name: str = "") -> None:
        """用户允许：卡片消失后 Shell 行回到运行中。"""
        call = self._find_shell(name, states=("pending", "running"))
        if call is not None:
            call["state"] = "running"
            self._refresh()

    def deny_shell(self, name: str = "") -> None:
        """用户拒绝：Shell 行只留拒绝状态。"""
        call = self._find_shell(name, states=("running", "pending"))
        if call is not None:
            call["state"] = "denied"
            call["ok"] = False
            self._refresh()

    def finish_shell(
        self,
        name: str,
        ok: bool,
        elapsed: float,
        *,
        lines: int = 0,
        exit_code: int | None = None,
        call_id: str = "",
    ) -> None:
        """shell 结束：只回填耗时、行数、exit code，不接收 stdout。"""
        call = self._find_shell(name, call_id=call_id, states=("running", "pending"))
        if call is None:
            self.start_shell(name, call_id)
            call = self._shells[-1] if self._shells else None
        if call is None or call.get("state") == "denied":
            return
        call["state"] = "done" if ok else "failed"
        call["ok"] = bool(ok)
        call["elapsed"] = max(0.0, float(elapsed or 0.0))
        call["lines"] = max(0, int(lines or 0))
        if exit_code is None:
            exit_code = 0 if ok else 1
        call["exit_code"] = int(exit_code)
        self._refresh()

    def add_shell_record(self, data: dict) -> None:
        """从 history 的 shell_card 记录恢复 Shell 行（不恢复命令正文）。"""
        if not isinstance(data, dict):
            return
        tool = str(data.get("tool") or "Shell")
        state = str(data.get("state") or "done")
        if state == "denied":
            self.start_shell(tool)
            self.deny_shell(tool)
            return
        if state in {"pending", "allowed"}:
            self.start_shell(tool)
            if state == "pending":
                self.wait_shell(tool)
            return
        self.start_shell(tool)
        self.finish_shell(
            tool,
            state != "failed",
            float(data.get("elapsed", 0.0) or 0.0),
            lines=int(data.get("line_count", 0) or 0),
            exit_code=data.get("exit_code"),
        )

    def _find_shell(
        self,
        name: str = "",
        *,
        call_id: str = "",
        states: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        for call in reversed(self._shells):
            if call_id and call.get("id") != call_id:
                continue
            if name and call.get("name") != name:
                continue
            if states is not None and call.get("state") not in states:
                continue
            return call
        return None

    @staticmethod
    def _seconds_from_text(text: str) -> float:
        tail = str(text or "").rsplit(" · ", 1)[-1].strip()
        if tail.endswith("s"):
            try:
                return float(tail[:-1])
            except ValueError:
                return 0.0
        return 0.0

    # ------------------------------------------------------------ 渲染
    def _sync_timer(self) -> None:
        if self._state == "running":
            self._timer = ensure_interval(self, self._timer, self._tick)

    def _stop_timer(self) -> None:
        self._timer = stop_timer(self._timer)

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

    def action_toggle_body(self) -> None:
        if not self._body_text().plain:
            return
        self._collapsed = not self._collapsed
        self._refresh()

    def on_click(self, event) -> None:
        event.stop()
        self.focus()
        self.action_toggle_body()

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
            bits.append(format_duration(elapsed))
            meta = "· " + " · ".join(bits)
            state_text = "done"
            state_style = f"bold {theme.success}"
        self._meta.update(meta)
        self._state_label.update(Text(state_text, style=state_style))
        body = self._body_text()
        has_body = bool(body.plain)
        self._body.display = has_body and not self._collapsed
        if has_body:
            self._body.update(body)
        marker = ("▸ " if self._collapsed else "▾ ") if has_body else ""
        self._title.update(Text(f"{marker}{title}", style=f"bold {theme.primary}"))
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
        shell_row = self._shell_row()
        if shell_row is not None:
            rows.append(shell_row)
        for name, count in sorted(self._tools.items()):
            if name in SHELL_TOOLS:
                continue
            if self._skills and name == "Skill":
                continue
            if self._diffs and name in {"Edit", "Write"}:
                continue
            rows.append(
                self._row(name, f"{count} call{'s' if count != 1 else ''}")
            )
        diff_row = self._diff_row()
        if diff_row is not None:
            rows.append(diff_row)
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

    def _shell_row(self) -> Text | None:
        if not self._shells:
            return None
        theme = self._theme
        names = sorted({str(call.get("name") or "Shell") for call in self._shells})
        label = names[0] if len(names) == 1 else "Shell"
        count = len(self._shells)
        calls = f"{count} call{'s' if count != 1 else ''}"
        states = {str(call.get("state") or "") for call in self._shells}
        if "denied" in states:
            detail = f"{label} · Denied by user"
            return self._row("Shell", detail, mark="✗", tone=theme.error)
        if "pending" in states:
            return self._row(
                "Shell",
                f"{label} · waiting for approval",
                mark="…",
                tone=theme.secondary,
            )
        if "running" in states:
            return self._row(
                "Shell",
                f"{label} · {calls} · running",
                mark="…",
                tone=theme.secondary,
            )
        ok = all(bool(call.get("ok")) for call in self._shells)
        seconds = sum(float(call.get("elapsed") or 0.0) for call in self._shells)
        lines = sum(int(call.get("lines") or 0) for call in self._shells)
        exit_code = 0
        if not ok:
            exit_code = next(
                (
                    int(call.get("exit_code") or 1)
                    for call in self._shells
                    if not call.get("ok")
                ),
                1,
            )
        detail = f"{label} · {calls} · {seconds:.1f}s"
        if lines:
            detail += f" · {lines} line{'s' if lines != 1 else ''}"
        detail += f" · exit {exit_code}"
        return self._row(
            "Shell",
            detail,
            mark="✓" if ok else "✗",
            tone=theme.success if ok else theme.error,
        )

    def _diff_row(self) -> Text | None:
        if self._diffs:
            path, additions, deletions = self._diffs[0]
            shown = path if len(self._diffs) == 1 else f"{path}  +{len(self._diffs) - 1} more"
            detail = f"{shown} +{additions} -{deletions}"
            return self._row("Diff", detail)
        if self._changes is not None:
            files, additions, deletions = self._changes
            detail = (
                f"{files} file{'s' if files != 1 else ''} "
                f"+{additions} -{deletions}"
            )
            return self._row("Diff", detail)
        return None

    def _row(
        self,
        label: str,
        detail: str,
        *,
        mark: str = "",
        tone: str = "",
    ) -> Text:
        theme = self._theme
        row = Text()
        row.append(f"{label:<10}", style=theme.secondary)
        if mark:
            row.append(f"{mark} ", style=f"bold {tone or theme.foreground}")
        row.append(detail, style=theme.foreground)
        return row
