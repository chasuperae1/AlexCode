"""文件改动的权限确认卡：确认栏固定在卡片底部。"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from alex.screens.diff import diff_lines, parse_diff
from alex.ui.permission_choice import PermissionChoiceMixin

from alex.ui.cards.base import DiffAction
from alex.ui.cards.tool_diff import ToolDiffCard


class PermissionDiffCard(PermissionChoiceMixin, ToolDiffCard):
    """权限确认复用单次 diff 卡：确认栏固定在卡片底部，不随折叠隐藏。

    ``ToolDiffCard`` 的 footer 原本在 ``.tdc-body`` 内，折叠会一并隐藏；权限
    确认需要始终可见的 Allow / Always / Deny，所以这里只复用它的路径头、
    diff 正文和配色，footer 单独挂在 body 外面。
    """

    can_focus = True

    BINDINGS = [
        Binding("enter", "allow", "Allow once", show=False, priority=True),
        Binding("a", "always", "Always allow", show=False, priority=True),
        Binding("escape", "deny", "Deny", show=False, priority=True),
        Binding("space", "toggle", "Expand", show=False),
    ]

    DEFAULT_CSS = ToolDiffCard.DEFAULT_CSS.replace(
        "ToolDiffCard", "PermissionDiffCard"
    ) + """
    PermissionDiffCard .pdc-foot {
        height: 2;
        padding: 0;
        border-top: solid $primary 30%;
    }
    PermissionDiffCard .pdc-label {
        width: 1fr;
        height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    PermissionDiffCard .pdc-hint {
        width: auto;
        height: 1;
        color: $text-muted;
    }
    PermissionDiffCard .pdc-actions {
        width: auto;
        height: 1;
    }
    PermissionDiffCard .pdc-act {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    PermissionDiffCard .pdc-act:hover {
        color: $foreground;
        background: $boost;
    }
    PermissionDiffCard .pdc-act.-primary {
        color: $foreground;
        background: $primary 16%;
        text-style: bold;
    }
    PermissionDiffCard .pdc-act.-primary:hover {
        background: $primary 25%;
    }
    PermissionDiffCard .pdc-act.-danger {
        color: $error;
    }
    PermissionDiffCard .tdc-code {
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    PermissionDiffCard.-allowed {
        border: round $success 35%;
    }
    PermissionDiffCard.-allowed:focus,
    PermissionDiffCard.-allowed:focus-within {
        border: round $success;
    }
    PermissionDiffCard.-denied {
        border: round $error 35%;
    }
    PermissionDiffCard.-denied:focus,
    PermissionDiffCard.-denied:focus-within {
        border: round $error;
    }
    """

    def __init__(self, payload: dict, theme, on_choice, **kwargs) -> None:
        self._payload = dict(payload)
        self.tool_name = str(payload.get("tool") or "Tool")
        self._agent_name = str(payload.get("agent") or "Alex")
        self._on_choice = on_choice
        self._state = "pending"
        self._ready = False
        self._allow_always = bool(payload.get("allow_always", True))
        diff_text = str(payload.get("diff") or "")
        files = parse_diff(diff_text)
        lines: list[Text] = []
        if files:
            try:
                lines = diff_lines(diff_text, dark=bool(theme.dark))
                if len(files) == 1 and lines:
                    # 路径和 +/− 已在标题行，正文不再重复文件头与 hunk 头
                    lines = lines[1:]
                    lines = [
                        line for line in lines
                        if not line.plain.lstrip().startswith("@@")
                    ]
            except Exception:
                lines = []
            path = files[0].path or str(payload.get("detail") or self.tool_name)
            additions = sum(item.additions for item in files)
            deletions = sum(item.deletions for item in files)
            has_diff = True
        else:
            path = self.tool_name
            additions = deletions = 0
            detail = str(payload.get("detail") or "").strip()
            if detail:
                lines = [Text(detail, style=theme.foreground)]
            has_diff = False

        super().__init__(path, (additions, deletions), lines, "", theme, **kwargs)
        self.add_class("permission-diff-card")
        self._has_diff = has_diff
        self._max_lines = 18
        self._open = True
        self.set_class(True, "-open")

        self._foot_label = Static(classes="pdc-label")
        self._foot_hint = Static("Space Collapse", classes="pdc-hint")
        self._allow = DiffAction(
            self._act_label("Enter", "Allow once"),
            "allow",
            self._act,
            classes="pdc-act -primary",
        )
        self._always = None
        if self._allow_always:
            self._always = DiffAction(
                self._act_label("A", "Always allow"),
                "always",
                self._act,
                classes="pdc-act",
            )
        self._deny = DiffAction(
            self._act_label("Esc", "Deny"),
            "deny",
            self._act,
            classes="pdc-act -danger",
        )
        children = [self._allow]
        if self._always is not None:
            children.append(self._always)
        children.append(self._deny)
        self._actions = Horizontal(*children, classes="pdc-actions")
        self._foot = Horizontal(
            self._foot_label, self._actions, self._foot_hint, classes="pdc-foot"
        )

    # ------------------------------------------------------------ 文案
    def _act_label(self, key: str, label: str) -> Text:
        text = Text()
        text.append(key, style=self._theme.secondary)
        text.append(" ", style=self._theme.secondary)
        text.append(label)
        return text

    def _header_counts(self) -> Text:
        if not getattr(self, "_has_diff", True):
            return Text("")
        return super()._header_counts()

    def _header_path(self) -> Text:
        style = self._theme.primary
        if getattr(self, "_state", "pending") == "denied":
            style = self._theme.error
        elif getattr(self, "_state", "pending") == "allowed":
            style = self._theme.success
        text = Text()
        text.append("▤ ", style=f"bold {style}")
        text.append(self._path, style=self._theme.foreground)
        return text

    # ------------------------------------------------------------ 组装
    def compose(self) -> ComposeResult:
        yield self._head
        with Vertical(classes="tdc-body"):
            if self._has_diff:
                yield Static(self._legend(), classes="tdc-legend")
            yield self._code
        yield self._foot

    def on_mount(self) -> None:
        super().on_mount()
        self.add_class("-open")
        self._ready = True
        self._refresh_state()
        self._code.styles.height = min(
            max(1, len(self._lines)), self._max_lines
        )
        self.call_after_refresh(self._sync_scrollbar)
        self.call_after_refresh(self._sync_scrollbar)
        self.focus()

    def _sync_scrollbar(self) -> None:
        scrolls = len(self._lines) > self._max_lines
        self._code.show_vertical_scrollbar = scrolls
        self.set_class(scrolls, "-scroll")

    def set_lines(self, lines: list[Text]) -> None:
        self._lines = list(lines)
        self._path_static.update(self._header_path())
        self._counts_static.update(self._header_counts())
        self._write_lines()
        if self._open:
            self._code.styles.height = min(
                max(1, len(self._lines)), self._max_lines
            )

    def _write_lines(self) -> None:
        super()._write_lines()
        # 预览完整落在卡片内时不要挂一条空滚动条，保持和批准稿一致
        self._sync_scrollbar()

    # ------------------------------------------------------------ 状态
    def _refresh_state(self) -> None:
        state = self._state
        self.set_class(state == "allowed", "-allowed")
        self.set_class(state == "denied", "-denied")
        self._actions.display = state == "pending"
        self._foot_hint.display = state != "pending"
        if not getattr(self, "_ready", False):
            return
        self._path_static.update(self._header_path())
        self._counts_static.update(self._header_counts())
        self._hint_static.update(self._hint_text())

        text = Text()
        if state == "pending":
            who = str(
                self._payload.get("subagent_label") or self._agent_name
            )
            text.append("Permission required", style=f"bold {self._theme.warning}")
            text.append(
                f" · {who} · {self.tool_name}",
                style=self._theme.secondary,
            )
            text.append(" · mode default", style=self._theme.secondary)
        elif state == "allowed":
            text.append("Allowed · Writing…", style=f"bold {self._theme.success}")
        elif state == "denied":
            text.append("Denied · File unchanged", style=f"bold {self._theme.error}")
        elif state == "failed":
            text.append(
                str(self._payload.get("fail_text") or "Execution failed · File unchanged"),
                style=f"bold {self._theme.error}",
            )
        elif state == "done":
            text.append(
                f"Changes complete {self._payload.get('clock', '')} · "
                f"+{self._additions} added · −{self._deletions} deleted",
                style=self._theme.secondary,
            )
        self._foot_label.update(text)

    def set_state(self, state: str) -> None:
        self._state = state
        self._refresh_state()

    def finish(self, data: dict) -> None:
        """把权限卡原地变成正常的完成/失败 diff 卡，避免再挂第二张。"""
        ok = bool(data.get("ok", True))
        self._payload["clock"] = time.strftime("%H:%M:%S")
        if ok:
            self._state = "done"
        else:
            self._state = "failed"
            self._payload["fail_text"] = str(data.get("text") or "Write failed · File unchanged")
        self._refresh_state()

    def _act(self, action: str) -> None:
        self.choose(action)

    def action_allow(self) -> None:
        self.choose("allow")

    def action_always(self) -> None:
        self.choose("always")

    def action_deny(self) -> None:
        self.choose("deny")

    def action_toggle(self) -> None:
        super().action_toggle()
        if self._state != "pending":
            self._foot_hint.update("Space Collapse" if self._open else "Space Expand")
