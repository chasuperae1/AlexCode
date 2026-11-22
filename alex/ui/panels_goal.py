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
            "Confirm and start", "goal_confirm", classes="goal-act goal-confirm"
        )
        self._confirm_key = Static("(enter)", classes="goal-key")
        self._edit = QueueButton("Edit request", "goal_edit", classes="goal-act goal-edit")
        self._edit_key = Static("(m)", classes="goal-key")
        self._cancel = QueueButton("Cancel", "goal_cancel", classes="goal-act goal-cancel")
        self._cancel_key = Static("(esc)", classes="goal-key")
        self._note = Static("Not started", classes="goal-note")
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
            title = "Goal · Additional details"
            total = int(payload.get("questions") or 0)
            done = int(payload.get("answered") or 0)
            meta = f"{done}/{total} · {frame} {elapsed:.1f}s"
            body = "Answer the questions below. Press Esc to skip one."
        else:
            title = "Goal · Understanding request"
            meta = f"{frame} {elapsed:.1f}s"
            body = "Breaking your request down into actionable constraints…"
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
                Text("  Model is taking longer than usual…  Esc Cancel", style=theme.secondary)
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
        except Exception:  # 底栏尚未挂载
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
        """按阶段渲染：理解中 / 待确认走理解块，其余走执行块 + 校验结论。"""
        phase = str(self._payload.get("phase") or "executing")
        width = self._content_width()
        if phase in {"understanding", "clarifying", "awaiting_confirmation"}:
            return self._render_understanding(width, phase)
        return self._render_execute(width, phase)

    def _content_width(self) -> int:
        try:
            width = self.content_size.width
            if width <= 0:
                width = self.size.width - 2
        except Exception:  # 尚未挂载，拿不到实际尺寸
            width = 76
        return max(30, min(width, 160))

    def _render_understanding(self, width: int, phase: str) -> Text:
        """理解中：转圈提示；理解完毕：摘要 + 已确认约束 + 确认提示。"""
        if phase in {"understanding", "clarifying"}:
            return self._render_live(width, phase)
        theme = self._theme
        lines = [self._render_head(phase, width), self._render_summary()]
        brief = self._payload.get("brief") or []
        if brief:
            row = Text()
            row.append("   Confirmed constraints  ", style=theme.secondary)
            for index, item in enumerate(brief):
                if index:
                    row.append(" · ", style=theme.secondary)
                key = str(item.get("key") or "")
                value = str(item.get("value") or "")
                if key:
                    row.append(f"{key}: ", style=theme.secondary)
                row.append(value, style=theme.foreground)
            lines.append(row)
        lines.append(
            Text(
                "Execution starts after confirmation. Edit the request and press Enter to confirm again.",
                style=theme.secondary,
            )
        )
        return Text("\n").join(lines)

    def _render_execute(self, width: int, phase: str) -> Text:
        """执行中 / 已结束：标题 + 任务摘要 + 每轮列表 + 校验结论。"""
        lines = [self._render_head(phase, width), self._render_summary()]
        lines.extend(self._render_rows(width))
        lines.append(self._render_verify(phase, width))
        return Text("\n").join(lines)

    def _render_head(self, phase: str, width: int) -> Text:
        theme = self._theme
        payload = self._payload
        max_iters = max(1, int(payload.get("max_iters") or 1))
        round_no = max(1, int(payload.get("round") or 1))

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
            title = "Goal · Confirm task"
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
        return head

    def _render_summary(self) -> Text:
        theme = self._theme
        payload = self._payload
        goal = Text()
        goal.append("1  ", style=theme.secondary)
        goal.append("Task summary: ", style=theme.secondary)
        goal.append(
            str(payload.get("restatement") or payload.get("goal") or ""),
            style=theme.foreground,
        )
        return goal

    def _render_rows(self, width: int) -> list[Text]:
        """每轮 execute / verify 一行：序号、阶段、结果、耗时。"""
        theme = self._theme
        lines: list[Text] = []
        for row in self._payload.get("rows") or []:
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
        return lines

    def _render_verify(self, phase: str, width: int) -> Text:
        """底部结论行：轮次/耗时、失败轮数或停止说明。"""
        theme = self._theme
        payload = self._payload
        round_no = max(1, int(payload.get("round") or 1))
        seconds = max(0, int(payload.get("seconds") or 0))
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
        return foot

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
