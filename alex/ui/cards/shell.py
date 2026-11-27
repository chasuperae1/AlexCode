"""Shell 命令的权限确认卡：命令正文换行、风险高亮、底部确认栏。"""

from __future__ import annotations

import math
import re
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import RichLog, Static

from alex.core.permissions import dangerous_patterns
from alex.ui.permission_choice import PermissionChoiceMixin

from alex.ui.cards.base import DiffAction


_MAX_ROWS = 10
_MIN_HEIGHT = 2
_PATH_LIMIT = 52
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')


class ShellPermissionCard(PermissionChoiceMixin, Vertical):
    """单个 shell 调用的权限确认：命令完整可见，确认栏固定在卡片底部。"""

    can_focus = True

    # 完成后不会被当成 diff 卡计数（见 ChatDiffMixin._append_tool_diff）
    finishes_as_diff = False

    BINDINGS = [
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
        Binding("left", "focus_prev", "Previous option", show=False, priority=True),
        Binding("right", "focus_next", "Next option", show=False, priority=True),
        Binding("a", "always", "Always allow", show=False, priority=True),
        Binding("escape", "deny", "Deny", show=False, priority=True),
        Binding("space", "toggle", "Expand", show=False),
    ]

    DEFAULT_CSS = """
    ShellPermissionCard {
        height: auto;
        margin: 0 0 1 2;
        padding: 0 1;
        background: transparent;
        border: round $primary 35%;
    }
    ShellPermissionCard .spc-head { height: 1; }
    ShellPermissionCard .spc-head:hover { background: $boost; }
    ShellPermissionCard .spc-title { width: 1fr; }
    ShellPermissionCard .spc-hint {
        width: auto;
        padding: 0 1 0 2;
        color: $text-muted;
    }
    ShellPermissionCard .spc-body { height: auto; }
    ShellPermissionCard.-folded .spc-body { display: none; }
    ShellPermissionCard .spc-meta { height: 1; color: $text-muted; }
    ShellPermissionCard .spc-code {
        height: 1;
        background: transparent;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
    }
    ShellPermissionCard .spc-out-label { height: 1; color: $text-muted; }
    ShellPermissionCard .spc-out {
        height: 1;
        background: transparent;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
    }
    ShellPermissionCard .spc-risk { height: 1; }
    ShellPermissionCard .spc-rule { height: 1; color: $text-muted; }
    ShellPermissionCard .spc-foot {
        height: auto;
        padding: 0;
        border-top: solid $primary 30%;
    }
    ShellPermissionCard .spc-foot-top { height: 1; }
    ShellPermissionCard .spc-actions-row { height: 1; }
    ShellPermissionCard .spc-label {
        width: 1fr;
        height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    ShellPermissionCard .spc-actions { width: auto; height: 1; }
    ShellPermissionCard .spc-act {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    ShellPermissionCard .spc-act:hover {
        color: $foreground;
        background: $boost;
    }
    ShellPermissionCard .spc-act.-danger { color: $error; }
    ShellPermissionCard .spc-act.-focus {
        color: $foreground;
        background: $primary 16%;
        text-style: bold;
    }
    ShellPermissionCard .spc-act.-danger.-focus {
        color: $error;
        background: $error 16%;
    }
    ShellPermissionCard.-allowed { border: round $success 40%; }
    ShellPermissionCard.-denied { border: round $error 40%; }
    ShellPermissionCard.-done { border: round $primary 35%; }
    ShellPermissionCard.-failed { border: round $error 40%; }
    ShellPermissionCard.-allowed .spc-foot { border-top: solid $success 30%; }
    ShellPermissionCard.-denied .spc-foot,
    ShellPermissionCard.-failed .spc-foot { border-top: solid $error 30%; }
    ShellPermissionCard.-done .spc-foot { border-top: solid $primary 30%; }
    """

    def __init__(self, payload: dict, theme, on_choice, **kwargs) -> None:
        super().__init__(classes="shell-permission", **kwargs)
        self._payload = dict(payload)
        self.tool_name = str(payload.get("tool") or "Shell")
        self._agent_name = str(payload.get("agent") or "Alex")
        self._on_choice = on_choice
        self._theme = theme
        self._state = "pending"
        self._ready = False
        self._open = True
        self._allow_always = bool(payload.get("allow_always", True))
        self._readonly = bool(payload.get("readonly", False))

        self._command = str(payload.get("command") or "").strip()
        if not self._command:
            detail = str(payload.get("detail") or "").strip()
            self._command = detail[len("command: "):] if detail.startswith("command: ") else detail
        self._cwd = str(payload.get("cwd") or "").strip()
        self._reason = str(payload.get("reason") or "").strip()
        self._rules = self._read_rules(payload.get("rules"))
        self._patterns = self._matched_patterns(self._command)
        self._pattern_hits = self._matched_texts(self._command, self._patterns)
        self._hits = [
            text for texts in self._pattern_hits.values() for text in texts
        ][:4]
        self._lines = self._build_lines()
        self._output = ""
        self._exit_code = int(payload.get("exit_code", 0) or 0)
        self._out_rows = 0
        # 超过首尾预览阈值的输出全文落在 data/shell/<request>.txt，展开时再读
        self._output_path = str(payload.get("output_path") or "")
        self._output_full = False
        self._total_rows = int(payload.get("line_count") or 0)

        self._title = Static(self._title_text(), classes="spc-title")
        self._hint = Static(self._hint_text(), classes="spc-hint")
        self._head = Horizontal(self._title, self._hint, classes="spc-head")

        self._meta = Static(self._meta_text(), classes="spc-meta")
        self._code = RichLog(
            wrap=True,
            min_width=1,
            markup=False,
            highlight=False,
            auto_scroll=False,
            classes="spc-code",
        )
        self._out_label = Static(classes="spc-out-label")
        self._out = RichLog(
            wrap=True,
            min_width=1,
            markup=False,
            highlight=False,
            auto_scroll=False,
            classes="spc-out",
        )
        self._out_label.display = False
        self._out.display = False
        risk_text = self._risk_text()
        self._risk = Static(risk_text, classes="spc-risk")
        self._risk.display = bool(risk_text.plain)
        self._rule = Static(self._rule_text(), classes="spc-rule")
        self._rule.display = self._allow_always

        self._foot_label = Static(classes="spc-label")
        self._always = None
        self._allow = DiffAction(
            self._act_label("Enter", "Allow once", strong=True),
            "allow",
            self._act,
            classes="spc-act",
        )
        if self._allow_always:
            self._always = DiffAction(
                self._act_label("A", "Always allow"),
                "always",
                self._act,
                classes="spc-act",
            )
        self._deny = DiffAction(
            self._act_label("Esc", "Deny"),
            "deny",
            self._act,
            classes="spc-act -danger",
        )
        options = [self._allow]
        keys = ["allow"]
        if self._always is not None:
            options.append(self._always)
            keys.append("always")
        options.append(self._deny)
        keys.append("deny")
        self._options = options
        self._option_keys = keys
        self._focus_index = 0
        children = list(options)
        self._actions = Horizontal(*children, classes="spc-actions")
        self._foot_top = Horizontal(self._foot_label, classes="spc-foot-top")
        self._actions_row = Horizontal(self._actions, classes="spc-actions-row")
        self._foot = Vertical(self._foot_top, self._actions_row, classes="spc-foot")

    # ------------------------------------------------------------ 文案
    @staticmethod
    def _read_rules(raw) -> list[str]:
        if not isinstance(raw, list):
            return []
        lines = []
        for item in raw:
            if isinstance(item, dict):
                text = str(item.get("content") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                lines.append(text)
        return lines

    @staticmethod
    def _matched_patterns(command: str) -> list[str]:
        """命中的危险规则：先去掉引号里的内容，避免提交信息之类误报。"""
        if not command:
            return []
        cleaned = _QUOTED.sub(" ", command)
        hits: list[str] = []
        for pattern in dangerous_patterns():
            try:
                if re.search(pattern, cleaned, re.IGNORECASE):
                    hits.append(pattern)
            except re.error:
                continue
        return hits

    @staticmethod
    def _matched_texts(
        command: str, patterns: list[str]
    ) -> dict[str, list[str]]:
        """命中的原文片段——给用户看的是 "del" 而不是正则 "\\bdel\\b"。"""
        found: dict[str, list[str]] = {}
        if not command:
            return found
        quoted = [match.span() for match in _QUOTED.finditer(command)]
        for pattern in patterns:
            try:
                matches = list(re.finditer(pattern, command, re.IGNORECASE))
            except re.error:
                continue
            texts: list[str] = []
            for match in matches:
                if match.start() == match.end():
                    continue
                if any(
                    match.start() >= start and match.end() <= end
                    for start, end in quoted
                ):
                    continue
                text = " ".join(match.group(0).split())[:40]
                if text and text not in texts:
                    texts.append(text)
            if texts:
                found[pattern] = texts
        return found

    def _title_text(self) -> Text:
        text = Text()
        text.append("❯ ", style=f"bold {self._theme.primary}")
        text.append(self.tool_name, style=f"bold {self._theme.foreground}")
        return text

    def _hint_text(self) -> Text:
        return Text(
            "Space Collapse" if self._open else "Space Expand",
            style=self._theme.secondary,
        )

    def _short_path(self, path: str) -> str:
        if len(path) <= _PATH_LIMIT:
            return path
        return "…" + path[-_PATH_LIMIT:]

    def _meta_text(self) -> Text:
        text = Text()
        text.append("cwd ", style=self._theme.secondary)
        text.append(
            self._short_path(self._cwd or "(session workspace)"),
            style=self._theme.foreground,
        )
        text.append(" · mode default", style=self._theme.secondary)
        return text

    def _risk_text(self) -> Text:
        if self._patterns:
            text = Text()
            text.append("⚠  ", style=f"bold {self._theme.error}")
            label = (
                "matches dangerous rule · "
                if len(self._hits) == 1
                else "matches dangerous rules · "
            )
            text.append(label, style=self._theme.error)
            text.append(
                "、".join(self._hits), style=f"bold {self._theme.error}"
            )
            return text
        if self._reason:
            text = Text()
            text.append("⚠  ", style=f"bold {self._theme.warning}")
            text.append(self._reason, style=self._theme.warning)
            return text
        return Text("")

    def _rule_text(self) -> Text:
        text = Text()
        text.append("A Always allow · ", style=self._theme.secondary)
        labels: list[str] = []
        for rule in self._rules[:3]:
            for hit in self._pattern_hits.get(rule, []):
                if hit not in labels:
                    labels.append(hit)
            if not self._pattern_hits.get(rule) and rule not in labels:
                labels.append(rule)
        if labels:
            text.append(
                f"remembers {'、'.join(labels)}", style=self._theme.foreground
            )
            text.append(" (this session)", style=self._theme.secondary)
        else:
            text.append(
                "no rule to remember — same as Allow once",
                style=self._theme.secondary,
            )
        return text

    def _act_label(self, key: str, label: str, strong: bool = False) -> Text:
        text = Text()
        text.append(key, style=self._theme.secondary)
        text.append(" ", style=self._theme.secondary)
        text.append(label, style=self._theme.foreground if strong else self._theme.secondary)
        return text

    def _state_text(self) -> Text:
        text = Text()
        state = self._state
        if state == "pending":
            who = str(
                self._payload.get("subagent_label") or self._agent_name
            )
            text.append("Permission required", style=f"bold {self._theme.warning}")
            text.append(
                f" · {who} · {self.tool_name} · mode default",
                style=self._theme.secondary,
            )
        elif state == "allowed":
            text.append("Allowed · Running…", style=f"bold {self._theme.success}")
        elif state == "denied":
            text.append("Denied · Command not run", style=f"bold {self._theme.error}")
        elif state == "failed":
            text.append(
                str(self._payload.get("fail_text") or "Execution failed"),
                style=f"bold {self._theme.error}",
            )
        elif state == "done":
            text.append("✓ ", style=f"bold {self._theme.success}")
            text.append(
                str(self._payload.get("result_text") or f"{self.tool_name} done"),
                style=self._theme.secondary,
            )
        return text

    # ------------------------------------------------------------ 命令正文
    def _build_lines(self) -> list[Text]:
        raw = (
            (self._command or "(no command)")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        lines: list[Text] = []
        for index, part in enumerate(raw.split("\n")):
            line = Text()
            line.append(
                "❯ " if index == 0 else "  ",
                style=(
                    f"bold {self._theme.primary}"
                    if index == 0
                    else self._theme.secondary
                ),
            )
            body = Text(part, style=self._theme.foreground)
            quoted = [match.span() for match in _QUOTED.finditer(part)]
            for pattern in self._patterns:
                for match in re.finditer(pattern, part, re.IGNORECASE):
                    if match.start() == match.end():
                        continue
                    # 引号里的内容不参与风险判定，这里也不高亮
                    if any(
                        match.start() >= start and match.end() <= end
                        for start, end in quoted
                    ):
                        continue
                    body.stylize(
                        f"bold {self._theme.error}", match.start(), match.end()
                    )
            line.append_text(body)
            lines.append(line)
        return lines

    def _visual_rows(self, width: int) -> int:
        width = max(8, width)
        rows = 0
        for line in self._lines:
            rows += max(1, math.ceil(cell_len(line.plain) / width))
        return rows

    def _sync_height(self) -> None:
        if not self._open:
            return
        width = 0
        try:
            width = self._code.content_size.width
        except Exception:
            width = 0
        if width <= 0:
            width = max(10, self.size.width - 6)
        # 以 RichLog 自己折行后的行数为准，避免按字符宽度估算算少了把命令裁掉
        rows = 0
        try:
            rows = self._code.virtual_size.height
        except Exception:
            rows = 0
        if rows <= 0:
            rows = self._visual_rows(width)
        self._code.styles.height = min(_MAX_ROWS, max(_MIN_HEIGHT, rows))

    # ------------------------------------------------------------ 组装
    def compose(self) -> ComposeResult:
        yield self._head
        with Vertical(classes="spc-body"):
            yield self._meta
            yield self._code
            yield self._out_label
            yield self._out
            yield self._risk
            yield self._rule
        yield self._foot

    def on_mount(self) -> None:
        self.add_class("-open")
        self._ready = True
        self._refresh_state()
        self._refresh_focus()
        for line in self._lines:
            self._code.write(line)
        if self._output:
            self._write_output(failed=self._state == "failed")
        # 先按估算给高度，布局完成后用实际折行行数再校正一次
        self.call_after_refresh(self._sync_height)
        self.call_after_refresh(self._sync_height)
        if not self._readonly:
            self.focus()

    def on_resize(self, event) -> None:
        self._sync_height()

    def set_theme(self, theme) -> None:
        """主题切换后就地换色，避免把未决卡从聊天流里摘掉再重挂。"""
        self._theme = theme
        self._lines = self._build_lines()
        self._title.update(self._title_text())
        self._hint.update(self._hint_text())
        self._meta.update(self._meta_text())
        self._risk.update(self._risk_text())
        self._rule.update(self._rule_text())
        self._allow.update(self._act_label("Enter", "Allow once", strong=True))
        if self._always is not None:
            self._always.update(self._act_label("A", "Always allow"))
        self._deny.update(self._act_label("Esc", "Deny"))
        self._code.clear()
        for line in self._lines:
            self._code.write(line)
        if self._output:
            self._write_output(failed=self._state == "failed")
        if self._ready:
            self._foot_label.update(self._state_text())
        self._refresh_focus()
        self.call_after_refresh(self._sync_height)
        self.refresh()

    # ------------------------------------------------------------ 状态
    def _refresh_state(self) -> None:
        state = self._state
        self.set_class(state == "allowed", "-allowed")
        self.set_class(state == "denied", "-denied")
        self.set_class(state == "done", "-done")
        self.set_class(state == "failed", "-failed")
        self._actions.display = state == "pending"
        self._actions_row.display = state == "pending"
        self._rule.display = self._allow_always and state == "pending"
        if not self._ready:
            return
        self._foot_label.update(self._state_text())

    def set_state(self, state: str) -> None:
        self._state = state
        self._refresh_state()

    def set_output(
        self,
        text: str,
        *,
        exit_code: int | None = None,
        failed: bool = False,
        path: str = "",
        line_count: int = 0,
    ) -> None:
        """开发者模式下展示 shell 输出；超出阈值时 text 只是首尾预览。"""
        self._output = str(text or "")
        self._output_path = str(path or "")
        self._output_full = False
        if line_count:
            self._total_rows = int(line_count)
        if exit_code is not None:
            self._exit_code = int(exit_code)
        self._out_rows = len(self._output.splitlines())
        if self._ready:
            self._write_output(failed=failed)

    def _ensure_full_output(self) -> bool:
        """展开时按需把外置的全文读回来；读不到就保留首尾预览。"""
        if self._output_full or not self._output_path:
            return False
        self._output_full = True
        try:
            from alex.config import ROOT

            path = Path(self._output_path)
            if not path.is_absolute():
                path = ROOT / path
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if not text.strip():
            return False
        self._output = text
        self._out_rows = len(text.splitlines())
        self._total_rows = max(self._total_rows, self._out_rows)
        return True

    def _write_output(self, *, failed: bool = False) -> None:
        text = self._output.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.splitlines()
        self._out_rows = len(lines)
        self._out.clear()
        self._out_label.display = bool(text)
        self._out.display = bool(text)
        if not text:
            self._out.styles.height = 1
            return
        label = Text()
        label.append("Output", style=f"bold {self._theme.secondary}")
        total = max(self._total_rows, self._out_rows)
        label.append(
            f" · {total} line{'s' if total != 1 else ''}",
            style=self._theme.secondary,
        )
        if total > self._out_rows:
            label.append(" · preview", style=self._theme.secondary)
        label.append(f" · exit {self._exit_code}", style=self._theme.secondary)
        self._out_label.update(label)
        style = self._theme.error if failed else self._theme.foreground
        for line in lines:
            self._out.write(Text(line, style=style))
        self._out.styles.height = min(12, max(1, self._out_rows))
        self.call_after_refresh(self._sync_height)

    def _fold(self, folded: bool) -> None:
        self._open = not folded
        self.set_class(folded, "-folded")
        self._hint.update(self._hint_text())
        if self._open:
            # 展开才把全文读回来：首屏只排版首尾预览，长输出不再拖慢挂载。
            if self._ensure_full_output() and self._ready:
                self._write_output(failed=self._state == "failed")
            self.call_after_refresh(self._sync_height)

    def finish(self, data: dict) -> None:
        """执行结束：原地保留完整卡片，并补上开发者模式的输出区。"""
        ok = bool(data.get("ok", True))
        text = str(data.get("text") or "").strip()
        self._payload["result_text"] = text or f"{self.tool_name} done"
        self._state = "done" if ok else "failed"
        if not ok:
            self._payload["fail_text"] = text or f"{self.tool_name} failed"
        self._exit_code = int(data.get("exit_code", 0 if ok else 1) or 0)
        stdout = str(data.get("stdout") or "")
        self.set_output(
            stdout,
            exit_code=self._exit_code,
            failed=not ok,
            path=str(data.get("output_path") or ""),
            line_count=int(data.get("line_count", 0) or 0),
        )
        if stdout.strip():
            # 有输出时保留完整卡片；若用户此前手动折叠则尊重其选择
            self._hint.update(self._hint_text())
        else:
            # 没有输出：收起成一行，只留结果行与操作提示
            self._fold(True)
        self._refresh_state()
        self.call_after_refresh(self._sync_height)

    # ------------------------------------------------------------ 交互
    def _act(self, action: str) -> None:
        self._focus_to(action)
        self.choose(action)

    def _refresh_focus(self) -> None:
        index = self._focus_index
        for position, option in enumerate(self._options):
            option.set_class(position == index, "-focus")

    def _focus_to(self, action: str) -> None:
        if action not in self._option_keys:
            return
        self._focus_index = self._option_keys.index(action)
        self._refresh_focus()

    def _move_focus(self, step: int) -> None:
        if self._state != "pending" or not self._options:
            return
        self._focus_index = (self._focus_index + step) % len(self._options)
        self._refresh_focus()

    def action_focus_prev(self) -> None:
        self._move_focus(-1)

    def action_focus_next(self) -> None:
        self._move_focus(1)

    def action_confirm(self) -> None:
        self.choose(self._option_keys[self._focus_index])

    def action_allow(self) -> None:
        self.choose("allow")

    def action_always(self) -> None:
        self.choose("always")

    def action_deny(self) -> None:
        self.choose("deny")

    def action_toggle(self) -> None:
        self._fold(self._open)
        if self._open:
            self.call_after_refresh(self.scroll_visible)
