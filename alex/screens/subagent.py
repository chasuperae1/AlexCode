"""子代理子会话整页。

列表本身在交互区里（``alex/ui/subagent_board.py``），这里只保留点进某一个
子代理后打开的只读子会话页。
"""

from __future__ import annotations

from typing import Any, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from alex.core.formatting import format_duration
from alex.ui.activity import SHELL_TOOLS
from alex.ui.markdown import MarkdownStatic
from alex.ui.widgets import PermissionDiffCard, ShellPermissionCard


class SubagentScreen(ModalScreen[None]):
    """子会话整页：与主界面同构，但没有输入框。"""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
    ]

    DEFAULT_CSS = """
    SubagentScreen { background: $background; }
    SubagentScreen #subagent-page {
        width: 100%;
        height: 100%;
        padding: 0 0;
    }
    SubagentScreen #subagent-meta {
        height: 1;
        padding: 0 1;
        margin-top: 1;
        color: $text-muted;
    }
    SubagentScreen #subagent-rules {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    SubagentScreen #subagent-scroll {
        height: 1fr;
        border: round $primary 30%;
        margin-top: 1;
        padding: 0 1;
        scrollbar-size-vertical: 0;
    }
    SubagentScreen #subagent-chat {
        height: auto;
        width: 1fr;
    }
    SubagentScreen #subagent-perm {
        height: auto;
        width: 1fr;
    }
    SubagentScreen .subagent-role {
        height: auto;
        margin-top: 1;
        color: $primary;
        text-style: bold;
    }
    SubagentScreen .subagent-think {
        height: auto;
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    SubagentScreen .subagent-tool {
        height: auto;
        padding: 0 1;
        color: $foreground;
    }
    SubagentScreen .subagent-md {
        height: auto;
        margin: 0 0 1 0;
    }
    SubagentScreen .subagent-steps {
        height: auto;
        margin: 1 0 1 0;
        padding: 0 1;
        border: round $primary 22%;
        color: $secondary;
    }
    SubagentScreen #subagent-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-align: right;
    }
    SubagentScreen #subagent-status {
        height: 1;
        padding: 0 1;
        margin: 1 0;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        record: dict[str, Any],
        theme,
        on_permission: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.subagent_id = str(record.get("id") or "")
        self._record = dict(record)
        self._theme = theme
        self._on_permission = on_permission
        self._dirty = False
        self._perm_request_id = ""

    # ------------------------------------------------------------ 布局
    def compose(self) -> ComposeResult:
        with Vertical(id="subagent-page"):
            yield Static(self._meta_text(), id="subagent-meta")
            yield Static(self._rules_text(), id="subagent-rules")
            with VerticalScroll(id="subagent-scroll"):
                yield Vertical(id="subagent-chat")
                yield Vertical(id="subagent-perm")
            yield Static(self._hint_text(), id="subagent-hint")
            yield Static(self._status_text(), id="subagent-status")

    def on_mount(self) -> None:
        self._render_now()
        self.set_interval(0.15, self._flush)

    def on_unmount(self) -> None:
        clear = getattr(self.app, "_clear_subagent_screen", None)
        if clear is not None:
            clear()

    def set_record(self, record: dict[str, Any]) -> None:
        self._record = dict(record)
        self._dirty = True

    def _flush(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self._render_now()

    # ------------------------------------------------------------ 文案
    def _meta_text(self) -> Text:
        theme = self._theme
        text = Text()
        text.append("◂ main", style=f"bold {theme.primary}")
        text.append(" / ", style=theme.secondary)
        text.append(str(self._record.get("label") or "Subagent"), style=f"bold {theme.foreground}")
        model = str(self._record.get("model") or "")
        if model:
            text.append(f" · {model}", style=theme.secondary)
        text.append(" · subagent", style=theme.secondary)
        return text

    def _rules_text(self) -> Text:
        theme = self._theme
        state = getattr(self.app, "_sources", None)
        count = len(getattr(state, "rules", []) or [])
        size = int(getattr(state, "rules_size", 0) or 0)
        label = f"▶ Rules · {count} loaded"
        if size:
            from alex.ui.sources import format_bytes

            label += f" · {format_bytes(size)}"
        return Text(label, style=theme.secondary)

    def _hint_text(self) -> Text:
        return Text("read-only · no input · esc back to main", style=self._theme.secondary)

    def _status_text(self) -> Text:
        theme = self._theme
        record = self._record
        status = str(record.get("status") or "running")
        waiting = bool(record.get("waiting"))
        text = Text()
        if waiting:
            text.append("⏹ waiting for permission", style=f"bold {theme.warning}")
        elif status == "running":
            text.append("◐ running", style=f"bold {theme.primary}")
        elif status == "completed":
            text.append("✓ done", style=f"bold {theme.success}")
        elif status == "interrupted":
            text.append("✗ interrupted", style=f"bold {theme.error}")
        else:
            text.append("✗ failed", style=f"bold {theme.error}")
        seconds = float(record.get("elapsed") or 0.0)
        if seconds:
            text.append(f" · {format_duration(seconds)}", style=theme.secondary)
        steps = int(record.get("steps") or 0)
        if steps:
            text.append(f" · {steps} tools", style=theme.secondary)
        if status in {"failed", "interrupted"} and record.get("error"):
            text.append(f" · {str(record.get('error'))[:120]}", style=theme.error)
        return text

    # ------------------------------------------------------------ 渲染
    def _render_now(self) -> None:
        try:
            self.query_one("#subagent-meta", Static).update(self._meta_text())
            self.query_one("#subagent-rules", Static).update(self._rules_text())
            self.query_one("#subagent-status", Static).update(self._status_text())
        except Exception:
            return
        self._render_chat()
        self._render_permission()

    def _render_chat(self) -> None:
        try:
            chat = self.query_one("#subagent-chat", Vertical)
        except Exception:
            return
        chat.remove_children()
        theme = self._theme
        widgets: list = []
        widgets.append(Static(Text("Task · from main", style=f"bold {theme.accent}"), classes="subagent-role"))
        prompt = str(self._record.get("prompt") or "").strip()
        if prompt:
            widgets.append(MarkdownStatic(prompt, classes="subagent-md"))
        transcript = self._record.get("transcript") or []
        text_seen = False
        for item in transcript:
            kind = str(item.get("kind") or "")
            if kind == "think":
                delta = str(item.get("delta") or "").strip()
                if delta:
                    widgets.append(
                        Static(
                            Text(f"◌ Thought: {delta}", style=theme.secondary),
                            classes="subagent-think",
                        )
                    )
            elif kind == "text":
                delta = str(item.get("delta") or "")
                if delta.strip():
                    text_seen = True
                    widgets.append(MarkdownStatic(delta, classes="subagent-md"))
            elif kind == "tool":
                if str(item.get("phase") or "") != "end":
                    continue
                ok = str(item.get("state") or "") == "success"
                line = Text()
                line.append("✓ " if ok else "✗ ", style=theme.success if ok else theme.error)
                line.append(str(item.get("name") or "Tool"), style=theme.foreground)
                elapsed = float(item.get("elapsed") or 0.0)
                if elapsed:
                    line.append(f" · {elapsed:.1f}s", style=theme.secondary)
                widgets.append(Static(line, classes="subagent-tool"))
        if not text_seen and str(self._record.get("text") or "").strip():
            widgets.append(MarkdownStatic(str(self._record.get("text")), classes="subagent-md"))
        elif not text_seen and str(self._record.get("result") or "").strip():
            widgets.append(
                MarkdownStatic(str(self._record.get("result")), classes="subagent-md")
            )
        status = str(self._record.get("status") or "running")
        if status in {"completed", "failed", "interrupted"}:
            steps = int(self._record.get("steps") or 0)
            seconds = float(self._record.get("elapsed") or 0.0)
            summary = Text()
            summary.append(f"{steps} steps", style=f"bold {self._theme.primary}")
            summary.append(
                f" · {self._record.get('label') or 'Subagent'}"
                f" · {format_duration(seconds)}",
                style=self._theme.secondary,
            )
            widgets.append(Static(summary, classes="subagent-steps"))
        try:
            chat.mount_all(widgets)
        except Exception:
            pass

    def _render_permission(self) -> None:
        try:
            host = self.query_one("#subagent-perm", Vertical)
        except Exception:
            return
        payload = self._record.get("permission")
        waiting = bool(self._record.get("waiting")) and isinstance(payload, dict)
        existing = getattr(self, "_perm_card", None)
        request_id = str((payload or {}).get("request_id") or "") if waiting else ""
        if existing is not None and request_id == self._perm_request_id:
            return
        if existing is not None:
            try:
                existing.remove()
            except Exception:
                pass
            self._perm_card = None
            self._perm_request_id = ""
        if not waiting:
            return
        tool = str(payload.get("tool") or "")
        card_class = ShellPermissionCard if tool in SHELL_TOOLS else PermissionDiffCard
        card = card_class(payload, self._theme, self._child_choice)
        self._perm_card = card
        self._perm_request_id = request_id
        try:
            host.mount(card)
            self.call_after_refresh(card.scroll_visible)
        except Exception:
            self._perm_card = None

    def _child_choice(self, choice: str) -> None:
        if self._on_permission is not None:
            self._on_permission(choice)

    # ------------------------------------------------------------ 交互
    def action_back(self) -> None:
        self.dismiss(None)

    def on_click(self, event) -> None:
        if getattr(event.widget, "id", None) == "subagent-meta":
            # 点 meta 行左侧的 ◂ main 返回
            self.action_back()
