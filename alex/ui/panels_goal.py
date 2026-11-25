"""目标流程卡。"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static
from termui.widgets import SPINNER_FRAMES

from alex.ui.panels_queue import QueueButton
from alex.ui.timing import ensure_interval, stop_timer


class GoalCard(Vertical):
    """目标卡片：按轮次显示 execute / verify、失败原因与最终判定。"""

    DEFAULT_CSS = """
    GoalCard {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 0 0 1;
        background: $background;
        border-left: solid $primary 45%;
    }
    GoalCard.-confirming {
        border: round $primary 35%;
        padding: 0 1;
    }
    GoalCard.-fail { border-left: solid $warning 55%; }
    GoalCard.-pass { border-left: solid $success 55%; }
    GoalCard.-stop { border-left: solid $secondary 45%; }
    GoalCard .goal-body { height: auto; padding: 0; }
    GoalCard .goal-foot { height: 1; margin-top: 1; }
    GoalCard .goal-act { width: auto; }
    GoalCard .goal-key, GoalCard .goal-sep { width: auto; color: $text-muted; }
    GoalCard .goal-gap { width: 1fr; height: 1; }
    GoalCard .goal-confirm { color: $primary; text-style: underline; }
    GoalCard .goal-edit { color: $accent; text-style: underline; }
    GoalCard .goal-cancel { color: $text-muted; text-style: underline; }
    GoalCard .goal-note { width: auto; color: $text-muted; }
    """

    def __init__(self, payload: dict, theme, **kwargs) -> None:
        super().__init__(classes="goal-card", **kwargs)
        self._payload = payload
        self._theme = theme
        self._started: float | None = None
        self._timer = None
        self._body = Static(self._render_content(), classes="goal-body")
        self._confirm = QueueButton(
            "确认并开始", "goal_confirm", classes="goal-act goal-confirm"
        )
        self._confirm_key = Static("(enter)", classes="goal-key")
        self._edit = QueueButton("修改需求", "goal_edit", classes="goal-act goal-edit")
        self._edit_key = Static("(m)", classes="goal-key")
        self._cancel = QueueButton("取消", "goal_cancel", classes="goal-act goal-cancel")
        self._cancel_key = Static("(esc)", classes="goal-key")
        self._note = Static("尚未执行", classes="goal-note")
        self._sync_classes()

    def compose(self) -> ComposeResult:
        yield self._body
        with Horizontal(classes="goal-foot"):
            yield self._confirm
            yield self._confirm_key
            yield Static("·", classes="goal-sep")
            yield self._edit
            yield self._edit_key
            yield Static("·", classes="goal-sep")
            yield self._cancel
            yield self._cancel_key
            yield Static("", classes="goal-gap")
            yield self._note

    def on_mount(self) -> None:
        self._refresh_actions()
        self._sync_timer()

    def on_unmount(self) -> None:
        self._stop_timer()

    _BRAILLE = SPINNER_FRAMES

    def _elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started)

    def _sync_timer(self) -> None:
        """只有实时卡（payload 带 live）在理解/澄清阶段才转圈，历史回放保持静态。"""
        phase = str(self._payload.get("phase") or "")
        if self._payload.get("live") and phase in {"understanding", "clarifying"}:
            if self._timer is None:
                self._started = time.monotonic()
                self._timer = ensure_interval(self, None, self._tick)
            return
        self._stop_timer()

    def _stop_timer(self) -> None:
        self._timer = stop_timer(self._timer)
        self._started = None

    def _tick(self) -> None:
        if str(self._payload.get("phase") or "") not in {"understanding", "clarifying"}:
            self._stop_timer()
            return
        self._body.update(self._render_content())

    def _render_live(self, width: int, phase: str) -> Text:
        """理解中 / 补充条件：一行标题 + 一行说明，右侧走盲文帧和秒数。"""
        theme = self._theme
        payload = self._payload
        elapsed = self._elapsed()
        frame = self._BRAILLE[int(elapsed * 10) % len(self._BRAILLE)]
        if phase == "clarifying":
            title = "Goal · 补充条件"
            total = int(payload.get("questions") or 0)
            done = int(payload.get("answered") or 0)
            meta = f"{done}/{total} · {frame} {elapsed:.1f}s"
            body = "回答下面的问题，Esc 可跳过本题。"
        else:
            title = "Goal · 理解需求中"
            meta = f"{frame} {elapsed:.1f}s"
            body = "正在把需求拆成可执行的约束…"
        head = Text()
        head.append(title, style=f"bold {theme.primary}")
        head.append(self._fill(len(title), len(meta), width))
        head.append(meta, style=theme.secondary)
        line = Text()
        line.append("   ", style=theme.secondary)
        line.append(body, style=theme.foreground)
        lines = [head, line]
        if phase == "understanding" and elapsed >= 4:
            lines.append(
                Text("  模型响应较慢，仍在等待…  Esc 取消", style=theme.secondary)
            )
        return Text("\n").join(lines)

    def update_payload(self, payload: dict, theme=None) -> None:
        self._payload = payload
        if theme is not None:
            self._theme = theme
        self._sync_classes()
        self._body.update(self._render_content())
        self._refresh_actions()
        self._sync_timer()

    def _refresh_actions(self) -> None:
        confirming = self._payload.get("phase") == "awaiting_confirmation"
        for widget in (
            self._confirm,
            self._confirm_key,
            self._edit,
            self._edit_key,
            self._cancel,
            self._cancel_key,
        ):
            widget.display = confirming
        self._note.display = confirming
        try:
            self.query_one(".goal-foot").display = confirming
        except Exception:
            pass

    def _sync_classes(self) -> None:
        phase = str(self._payload.get("phase") or "")
        rows = self._payload.get("rows") or []
        failed = any(
            row.get("phase") == "verify" and row.get("status") == "bad"
            for row in rows
        )
        if phase == "awaiting_confirmation":
            target = "-confirming"
        elif phase == "passed":
            target = "-pass"
        elif phase in {"stopped", "impossible", "failed", "cancelled"}:
            target = "-stop"
        elif failed:
            target = "-fail"
        else:
            target = "-run"
        for name in ("-run", "-fail", "-pass", "-stop", "-confirming"):
            self.set_class(target == name, name)

    def _render_content(self) -> Text:
        theme = self._theme
        payload = self._payload
        phase = str(payload.get("phase") or "executing")
        max_iters = max(1, int(payload.get("max_iters") or 1))
        round_no = max(1, int(payload.get("round") or 1))
        seconds = max(0, int(payload.get("seconds") or 0))
        rows = payload.get("rows") or []
        try:
            width = self.content_size.width
            if width <= 0:
                width = self.size.width - 2
        except Exception:
            width = 76
        width = max(30, min(width, 160))

        if phase in {"understanding", "clarifying"}:
            return self._render_live(width, phase)

        if phase == "passed":
            suffix = "" if round_no == 1 else "s"
            title = f"Goal · passed in {round_no} round{suffix}"
            meta = f"PASSED · {round_no}/{max_iters}"
            title_style = meta_style = f"bold {theme.success}"
        elif phase in {"stopped", "impossible"}:
            title = "Goal · stopped"
            meta = f"STOPPED · ROUND {round_no}/{max_iters}"
            title_style, meta_style = f"bold {theme.secondary}", theme.secondary
        elif phase == "cancelled":
            title = "Goal · cancelled"
            meta = "NOT STARTED"
            title_style = meta_style = theme.secondary
        elif phase == "failed":
            title = "Goal · failed"
            meta = f"FAILED · ROUND {round_no}/{max_iters}"
            title_style = meta_style = f"bold {theme.error}"
        elif phase == "awaiting_confirmation":
            title = "Goal · 任务确认"
            meta = "AWAITING CONFIRMATION"
            title_style = f"bold {theme.primary}"
            meta_style = theme.secondary
        else:
            label = "VERIFYING" if phase == "verifying" else "WORKING"
            title = f"Goal · up to {max_iters} rounds"
            meta = f"{label} · ROUND {round_no}/{max_iters}"
            title_style = f"bold {theme.primary}"
            meta_style = theme.secondary

        head = Text()
        head.append(title, style=title_style)
        head.append(self._fill(len(title), len(meta), width))
        head.append(meta, style=meta_style)

        goal = Text()
        goal.append("1  ", style=theme.secondary)
        goal.append("我理解的任务：", style=theme.secondary)
        goal.append(
            str(payload.get("restatement") or payload.get("goal") or ""),
            style=theme.foreground,
        )

        lines = [head, goal]
        brief = payload.get("brief") or []
        if phase == "awaiting_confirmation" and brief:
            row = Text()
            row.append("   已确认约束  ", style=theme.secondary)
            for index, item in enumerate(brief):
                if index:
                    row.append(" · ", style=theme.secondary)
                key = str(item.get("key") or "")
                value = str(item.get("value") or "")
                if key:
                    row.append(f"{key}：", style=theme.secondary)
                row.append(value, style=theme.foreground)
            lines.append(row)
        if phase == "awaiting_confirmation":
            hint = Text(
                "确认后才开始执行；修改需求后按 Enter 重新确认。",
                style=theme.secondary,
            )
            lines.extend((hint,))
            return Text("\n").join(lines)
        for row in rows:
            is_execute = row.get("phase") == "execute"
            index = str(row.get("round") or "") if is_execute else ""
            label = "execute" if is_execute else "verify"
            status = str(row.get("status") or "")
            body = str(row.get("text") or "")
            cost = max(0, int(row.get("seconds") or 0))
            prefix = f"{index:>2}  {label:<7} "
            tail = f"  {cost}s" if cost else ""
            room = max(6, width - len(prefix) - len(tail) - 1)
            if len(body) > room:
                body = body[: room - 1] + "…"
            color = (
                theme.success
                if status == "ok"
                else theme.error
                if status == "bad"
                else theme.foreground
            )
            line = Text()
            line.append(prefix, style=theme.secondary)
            line.append(body, style=color)
            line.append(" " * max(1, room - len(body)) + tail, style=theme.secondary)
            lines.append(line)

        failed_rounds = int(payload.get("failed_rounds") or 0)
        if phase == "passed":
            suffix = "" if round_no == 1 else "s"
            left = f"{round_no} round{suffix} · {self._seconds(seconds)}"
            right = "stopped automatically after pass"
        elif phase in {"stopped", "impossible"}:
            left, right = "workspace changes are kept", ""
        elif phase == "failed":
            left, right = "verifier could not finish", ""
        else:
            left = "esc stop · verifier runs after each round"
            if failed_rounds:
                suffix = "" if failed_rounds == 1 else "s"
                right = f"{failed_rounds} failed round{suffix}"
            else:
                right = "verifier is read-only"
        foot = Text()
        if right:
            room = max(6, width - len(right) - 1)
            if len(left) > room:
                left = left[: room - 1] + "…"
            foot.append(left, style=theme.secondary)
            foot.append(self._fill(len(left), len(right), width))
            foot.append(right, style=theme.secondary)
        else:
            foot.append(left[:width], style=theme.secondary)
        lines.append(foot)

        return Text("\n").join(lines)

    @staticmethod
    def _fill(left: int, right: int, width: int) -> str:
        return " " * max(1, width - left - right)

    @staticmethod
    def _seconds(total: int) -> str:
        if total < 60:
            return f"{total}s"
        minutes, seconds = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m {seconds:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"
