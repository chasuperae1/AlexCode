"""交互区里的子代理列表：与聊天区互斥显示，不遮顶栏、输入框与状态栏。

这一版没有独立 Screen：``#chat`` 与 ``#subagents`` 在同一块区域里切换，
所以列表只是主界面里的一个容器，卡片视觉沿用原来的 Subagent 卡。
"""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from alex.core.formatting import format_duration


def _clip(value: object, limit: int) -> str:
    """压成单行并截断：终端卡片里长任务不能换行撑破布局。"""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: max(1, limit - 1)] + "…"
    return text


class SubagentCard(Vertical):
    """列表里的一张子代理卡；点击直接进入对应子会话。"""

    def __init__(self, record: dict[str, Any], theme) -> None:
        self.record = dict(record)
        self.subagent_id = str(record.get("id") or "")
        self._theme = theme
        super().__init__(classes=f"subagent-card {self._state_class()}")

    def _state_class(self) -> str:
        if self.record.get("waiting"):
            return "waiting"
        status = str(self.record.get("status") or "running")
        if status == "completed":
            return "done"
        if status in {"failed", "interrupted"}:
            return "failed"
        return "running"

    def _icon(self) -> tuple[str, str]:
        theme = self._theme
        if self.record.get("waiting"):
            return "▲", theme.warning
        status = str(self.record.get("status") or "running")
        if status == "running":
            return "◐", theme.primary
        if status == "completed":
            return "✓", theme.success
        return "✗", theme.error

    def _meta(self) -> str:
        status = str(self.record.get("status") or "running")
        seconds = float(self.record.get("elapsed") or 0.0)
        if status == "running" and not seconds:
            started = float(self.record.get("started_at") or 0.0)
            if started:
                seconds = max(0.0, time.monotonic() - started)
        parts: list[str] = []
        if seconds:
            parts.append(format_duration(seconds))
        steps = int(self.record.get("steps") or 0)
        if steps:
            parts.append(f"{steps} tool{'s' if steps != 1 else ''}")
        return " · ".join(parts)

    def _activity(self) -> Text:
        theme = self._theme
        steps = int(self.record.get("steps") or 0)
        line = Text()
        line.append(f"step {steps or 1}", style=theme.secondary)
        last = next(
            (
                item
                for item in reversed(self.record.get("transcript") or [])
                if item.get("kind") == "tool"
                and str(item.get("phase") or "") == "end"
            ),
            None,
        )
        if last is None:
            line.append(" · working…", style=theme.secondary)
            return line
        line.append(" · ", style=theme.secondary)
        line.append(str(last.get("name") or "Tool"), style=theme.foreground)
        elapsed = float(last.get("elapsed") or 0.0)
        if elapsed:
            line.append(f" · {elapsed:.1f}s", style=theme.secondary)
        return line

    def compose(self) -> ComposeResult:
        with Horizontal(classes="sa-head"):
            self._cursor = Static("›", classes="sa-cursor")
            self._icon_w = Static("", classes="sa-icon")
            self._name_w = Static("", classes="sa-name")
            self._pill_w = Static("", classes="sa-pill")
            self._meta_w = Static("", classes="sa-meta")
            for widget in (
                self._cursor,
                self._icon_w,
                self._name_w,
                self._pill_w,
                self._meta_w,
            ):
                yield widget
        # 行固定挂好，后续只改内容：每来一条事件重挂控件会整卡闪一下
        self._desc_w = Static("", classes="sa-desc")
        yield self._desc_w
        self._perm_head_w = Static("", classes="sa-perm-head")
        self._perm_sub_w = Static("", classes="sa-perm-sub")
        self._perm_hint_w = Static("", classes="sa-perm-hint")
        for widget in (self._perm_head_w, self._perm_sub_w, self._perm_hint_w):
            yield widget
        self._line_w = Static("", classes="sa-line")
        yield self._line_w

    def on_mount(self) -> None:
        # 等挂载稳定后再改子行显示状态，避免布局中途改 display
        self.call_after_refresh(self._refresh_rows)

    def update_record(self, record: dict[str, Any]) -> None:
        """同一条子代理的状态刷新：只更新文本，不重建卡片。"""
        self.record = dict(record)
        self._apply_state_classes()
        self._refresh_rows()

    def _apply_state_classes(self) -> None:
        state = self._state_class()
        for name in ("waiting", "running", "done", "failed"):
            self.set_class(name == state, name)

    def _refresh_rows(self) -> None:
        if getattr(self, "_name_w", None) is None:
            return
        theme = self._theme
        record = self.record
        waiting = bool(record.get("waiting"))
        status = str(record.get("status") or "running")
        icon, tone = self._icon()
        pill = "awaiting approval" if waiting else status
        self._icon_w.update(Text(icon, style=tone))
        self._name_w.update(
            Text(_clip(record.get("label") or "Subagent", 32), style=f"bold {theme.foreground}")
        )
        self._pill_w.update(Text(pill, style=f"bold {tone}"))
        self._meta_w.update(Text(self._meta(), style=theme.secondary))
        self._desc_w.update(
            Text(_clip(record.get("description") or "—", 220), style=theme.secondary)
        )
        if waiting:
            self._perm_head_w.display = True
            self._perm_sub_w.display = True
            self._perm_hint_w.display = True
            self._line_w.display = False
            payload = record.get("permission") or {}
            tool = str(payload.get("tool") or record.get("waiting_tool") or "Tool")
            command = str(payload.get("command") or payload.get("detail") or "")
            self._perm_head_w.update(
                Text(f"◇ {tool} · {_clip(command, 90)}", style=f"bold {theme.warning}")
            )
            cwd = str(payload.get("cwd") or "")
            self._perm_sub_w.update(
                Text(
                    f"cwd {cwd}" if cwd else "blocked until the permission is resolved",
                    style=theme.secondary,
                )
            )
            self._perm_hint_w.update(
                Text("↵ review permission · esc back", style=theme.secondary)
            )
            return
        for widget in (self._perm_head_w, self._perm_sub_w, self._perm_hint_w):
            widget.display = False
        self._line_w.display = True
        if status == "running":
            self._line_w.update(self._activity())
        elif status == "completed":
            result = _clip(record.get("result") or record.get("text") or "done", 180)
            self._line_w.update(Text(f"result · {result}", style=theme.success))
        else:
            error = _clip(record.get("error") or "no details", 180)
            self._line_w.update(Text(f"{status} · {error}", style=theme.error))

    def set_selected(self, selected: bool) -> None:
        self.set_class(selected, "-selected")

    def on_click(self, event) -> None:
        event.stop()
        opener = getattr(self.app, "open_subagent", None)
        if opener is not None:
            opener(self.subagent_id)


class SubagentBoard(Vertical):
    """``#chat`` 位置上的子代理列表页。"""

    can_focus = True
    # 只在列表持有焦点时生效：聊天输入框里的 ctrl+x 仍是剪切
    BINDINGS = [
        Binding("up", "move(-1)", "Up", priority=True, show=False),
        Binding("down", "move(1)", "Down", priority=True, show=False),
        Binding("enter", "open", "Open", priority=True, show=False),
        Binding("ctrl+x", "stop", "Stop selected", priority=True, show=False),
    ]

    def action_move(self, step: int) -> None:
        self.move(step)

    def action_open(self) -> None:
        if self._selected_id:
            self.app.open_subagent(self._selected_id)

    def action_stop(self) -> None:
        self.app.action_subagent_stop()

    def __init__(self, records: list[dict[str, Any]], theme) -> None:
        super().__init__(classes="sa-board")
        self._records = list(records)
        self._theme = theme
        self._cards: list[SubagentCard] = []
        self._selected_id = self._default_selected(records)

    @staticmethod
    def _default_selected(records: list[dict[str, Any]]) -> str:
        for record in records:
            if record.get("waiting"):
                return str(record.get("id") or "")
        if records:
            return str(records[0].get("id") or "")
        return ""

    @property
    def selected_id(self) -> str:
        return self._selected_id

    def compose(self) -> ComposeResult:
        with Horizontal(classes="sa-page-head"):
            yield Static(
                Text("Subagents", style=f"bold {self._theme.foreground}"),
                classes="sa-title",
            )
            yield Static(self._badges_text(), id="sa-badges", classes="sa-badges")
        yield Static(
            Text(
                "每个子代理一张卡 · ↵ 进入只读子会话 · ctrl+x 停掉选中的这一个",
                style=self._theme.secondary,
            ),
            classes="sa-sub",
        )
        with VerticalScroll(id="sa-list"):
            with Vertical(id="sa-cards"):
                for record in self._records:
                    card = SubagentCard(record, self._theme)
                    self._cards.append(card)
                    yield card
        with Horizontal(classes="sa-page-foot"):
            yield Static(
                Text("↑↓ move · ↵ open · ctrl+x stop · esc back", style=self._theme.secondary),
                classes="sa-foot-hint",
            )
            yield Static(self._count_text(), id="sa-count", classes="sa-foot-count")

    def on_mount(self) -> None:
        self._sync_selection()

    def set_records(self, records: list[dict[str, Any]]) -> None:
        """列表打开期间状态变化：同序时只改文本，顺序变了才重排卡片。"""
        records = list(records)
        ids = [str(item.get("id") or "") for item in records]
        card_ids = [card.subagent_id for card in self._cards]
        new_selected = self._selected_id not in ids
        if new_selected:
            self._selected_id = self._default_selected(records)
        self._records = records
        if ids == card_ids:
            for card, record in zip(self._cards, records):
                card.update_record(record)
            self._update_meta()
            self._sync_selection(scroll=new_selected)
            return
        try:
            container = self.query_one("#sa-cards", Vertical)
        except Exception:
            return
        container.remove_children()
        self._cards = [SubagentCard(record, self._theme) for record in records]
        container.mount_all(self._cards)
        self._update_meta()
        # 状态刷新不抢滚动位置：只有选中项变了才跟着跳
        self.call_after_refresh(lambda: self._sync_selection(scroll=new_selected))

    def move(self, step: int) -> None:
        if not self._cards:
            return
        current = next(
            (
                index
                for index, card in enumerate(self._cards)
                if card.subagent_id == self._selected_id
            ),
            -1,
        )
        current = (current + step) % len(self._cards)
        self._selected_id = self._cards[current].subagent_id
        self._sync_selection()

    def _update_meta(self) -> None:
        try:
            self.query_one("#sa-badges", Static).update(self._badges_text())
            self.query_one("#sa-count", Static).update(self._count_text())
        except Exception:
            pass

    def _badges_text(self) -> Text:
        theme = self._theme
        waiting = sum(1 for item in self._records if item.get("waiting"))
        running = sum(
            1
            for item in self._records
            if not item.get("waiting") and item.get("status") == "running"
        )
        done = sum(1 for item in self._records if item.get("status") == "completed")
        failed = sum(
            1
            for item in self._records
            if item.get("status") in {"failed", "interrupted"}
        )
        text = Text()
        text.append(f"{len(self._records)} total", style=theme.secondary)
        if waiting:
            text.append(f" · {waiting} waiting", style=theme.warning)
        if running:
            text.append(f" · {running} running", style=theme.foreground)
        if done:
            text.append(f" · {done} done", style=theme.success)
        if failed:
            text.append(f" · {failed} failed", style=theme.error)
        return text

    def _count_text(self) -> Text:
        waiting = sum(1 for item in self._records if item.get("waiting"))
        label = f"{len(self._records)} subagent"
        if len(self._records) != 1:
            label += "s"
        if waiting:
            label += f" · {waiting} waiting"
        return Text(label, style=self._theme.secondary)

    def _sync_selection(self, scroll: bool = True) -> None:
        selected_index = 0
        for index, card in enumerate(self._cards):
            selected = card.subagent_id == self._selected_id
            card.set_selected(selected)
            if selected:
                selected_index = index
        if scroll and self._cards:
            try:
                self._cards[selected_index].scroll_visible()
            except Exception:  # 尚未完成布局时跳过
                pass
