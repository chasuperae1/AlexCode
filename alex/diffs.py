"""Unified diff 的生成、解析与展示（权限确认、变更摘要共用）。

展示参考 opencode：按文件分组，带行号和增删统计，
新增行绿色背景、删除行红色背景，可滚动查看。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static

MAX_DIFF_LINES = 120

REVERT = "revert"
CLOSE = "close"

ADD_BG = "#12261a"
DEL_BG = "#2a1416"


@dataclass
class DiffLine:
    """diff 里的一行。"""

    kind: Literal["add", "del", "ctx", "hunk", "meta"]
    text: str
    old_no: int | None = None
    new_no: int | None = None


@dataclass
class FileDiff:
    """一个文件的 diff。"""

    path: str
    additions: int = 0
    deletions: int = 0
    lines: list[DiffLine] = field(default_factory=list)
    binary: bool = False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def text_diff(path: str, old: str, new: str) -> str:
    """按修改前后的完整内容生成 unified diff。"""
    if old == new:
        return ""
    diff = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if not diff:
        return ""
    if len(diff) > MAX_DIFF_LINES:
        hidden = len(diff) - MAX_DIFF_LINES
        diff = diff[:MAX_DIFF_LINES] + [f"… {hidden} more line(s)"]
    return "\n".join(diff)


def tool_diff(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    base: Path | None = None,
) -> str:
    """为 Write / Edit 生成 unified diff；其他工具返回空字符串。"""
    name = (tool_name or "").lower()
    raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
    path = str(raw_path).strip()
    if not path:
        return ""
    target = Path(path).expanduser()
    if base is not None and not target.is_absolute():
        target = base / target
    if name == "write":
        old = _read_text(target)
        new = str(tool_input.get("content", ""))
    elif name == "edit":
        old = str(tool_input.get("old_string", ""))
        new = str(tool_input.get("new_string", ""))
    else:
        return ""
    return text_diff(path, old, new)


def diff_stat(text: str) -> tuple[int, int]:
    """统计 diff 文本里的新增 / 删除行数（忽略文件头）。"""
    additions = deletions = 0
    for line in text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MOJIBAKE = re.compile(r"(?:[\u00c0-\u00ff][\u0080-\u00bf]|â€|ï»¿)")


def looks_mojibake(text: str) -> bool:
    """粗判文本是否是 UTF-8 被按 Latin-1 解码后的乱码。"""
    return bool(_MOJIBAKE.search(text or ""))


def _clean_path(value: str) -> str:
    value = value.strip()
    if "\t" in value:
        value = value.split("\t", 1)[0]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    for prefix in ("a/", "b/"):
        if value.startswith(prefix):
            return value[2:]
    return value


def parse_diff(text: str) -> list[FileDiff]:
    """把 unified diff 拆成按文件分组、带行号的结构。"""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_no = new_no = 0
    in_hunk = False

    def start(path: str = "") -> FileDiff:
        nonlocal current, in_hunk
        current = FileDiff(path=path)
        files.append(current)
        in_hunk = False
        return current

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split()
            path = _clean_path(parts[-1]) if len(parts) >= 4 else ""
            start(path)
            continue
        if raw.startswith("--- "):
            if current is None:
                start()
            path = raw[4:].strip()
            if path != "/dev/null" and current is not None and not current.path:
                current.path = _clean_path(path)
            continue
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path != "/dev/null" and current is not None:
                current.path = _clean_path(path)
            continue
        if current is None:
            start("(unknown)")
        assert current is not None
        if raw.startswith("Binary files ") or raw.endswith(" differ"):
            current.binary = True
            current.lines.append(DiffLine("meta", raw))
            in_hunk = False
            continue
        match = _HUNK.match(raw)
        if match:
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            current.lines.append(DiffLine("hunk", raw))
            in_hunk = True
            continue
        if not in_hunk:
            current.lines.append(DiffLine("meta", raw))
            continue
        if raw.startswith("+"):
            current.lines.append(
                DiffLine("add", raw[1:], new_no=new_no)
            )
            current.additions += 1
            new_no += 1
        elif raw.startswith("-"):
            current.lines.append(
                DiffLine("del", raw[1:], old_no=old_no)
            )
            current.deletions += 1
            old_no += 1
        elif raw.startswith("\\"):
            current.lines.append(DiffLine("meta", raw))
        elif raw.startswith(" "):
            current.lines.append(
                DiffLine("ctx", raw[1:], old_no=old_no, new_no=new_no)
            )
            old_no += 1
            new_no += 1
        else:
            current.lines.append(DiffLine("meta", raw))
    return [item for item in files if item.path or item.lines]


def diff_lines(text: str, width: int | None = None) -> list[Text]:
    """把 diff 渲染成逐行 Rich Text，供 RichLog 或 Static 使用。"""
    files = parse_diff(text)
    rendered: list[Text] = []
    if not files:
        return rendered
    for file_index, item in enumerate(files):
        if file_index:
            rendered.append(Text(""))
        header = Text()
        header.append(f" {item.path or '(unknown)'}", style="bold cyan")
        if item.additions or item.deletions or not item.binary:
            header.append(
                f"   +{item.additions} -{item.deletions}", style="bold"
            )
        rendered.append(header)
        if item.binary:
            rendered.append(Text("  binary file", style="dim italic"))
            continue
        max_no = max(
            (
                max(entry.old_no or 0, entry.new_no or 0)
                for entry in item.lines
            ),
            default=1,
        )
        number_width = max(3, len(str(max_no)))
        for entry in item.lines:
            if entry.kind == "hunk":
                line = Text(entry.text, style="bold #5fafff")
            elif entry.kind == "meta":
                line = Text(entry.text, style="dim italic")
            else:
                gutter = (
                    f"{entry.old_no:>{number_width}} "
                    if entry.old_no is not None
                    else " " * (number_width + 1)
                )
                gutter += (
                    f"{entry.new_no:>{number_width}} "
                    if entry.new_no is not None
                    else " " * (number_width + 1)
                )
                if entry.kind == "add":
                    line = Text(style=f"green on {ADD_BG}")
                    line.append(gutter, style=f"dim on {ADD_BG}")
                    line.append("+ " + entry.text, style=f"green on {ADD_BG}")
                elif entry.kind == "del":
                    line = Text(style=f"red on {DEL_BG}")
                    line.append(gutter, style=f"dim on {DEL_BG}")
                    line.append("- " + entry.text, style=f"red on {DEL_BG}")
                else:
                    line = Text(style="dim")
                    line.append(gutter + "  " + entry.text)
            if width:
                line.truncate(max(8, width), overflow="ellipsis")
            rendered.append(line)
    return rendered


def render_diff(text: str, width: int | None = None) -> Text:
    """把 diff 文本渲染成带颜色、行号和文件分组的 Rich Text。"""
    lines = diff_lines(text, width)
    if not lines:
        return Text("No text changes (binary or empty).", style="dim")
    result = Text()
    for index, line in enumerate(lines):
        if index:
            result.append("\n")
        result.append_text(line)
    return result


class DiffScreen(ModalScreen[str | None]):
    """全屏查看一轮对话的 diff，可直接回滚这次变更。"""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+c", "close", "Close", show=False),
        Binding("r", "revert", "Revert", show=False),
        Binding("j", "scroll_down", "Scroll down", show=False),
        Binding("k", "scroll_up", "Scroll up", show=False),
        Binding("g", "home", "Top", show=False),
        Binding("shift+g", "end", "Bottom", show=False),
    ]

    DEFAULT_CSS = """
    DiffScreen {
        align: center middle;
        background: black 60%;
    }
    DiffScreen #diff-panel {
        width: 98%;
        height: 92%;
        border: round $primary;
        background: $background;
        padding: 0 1;
    }
    DiffScreen #diff-title {
        color: $primary;
        text-style: bold;
        height: 1;
    }
    DiffScreen #diff-prompt {
        height: 1;
        color: $text-muted;
    }
    DiffScreen #diff-warning {
        height: 1;
        color: $warning;
    }
    DiffScreen #diff-body {
        height: 1fr;
        background: $background;
        scrollbar-size-vertical: 1;
    }
    DiffScreen #diff-actions {
        height: 1;
    }
    DiffScreen #diff-hint {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    DiffScreen .diff-action {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $primary;
    }
    DiffScreen .diff-action:hover {
        color: $accent;
        text-style: underline;
    }
    """

    def __init__(
        self,
        prompt: str,
        diff_text: str,
        *,
        summary: str = "",
        can_revert: bool = True,
    ) -> None:
        super().__init__()
        self._prompt = prompt
        self._diff_text = diff_text
        self._summary = summary
        self._can_revert = can_revert

    def compose(self) -> ComposeResult:
        preview = self._prompt.replace("\n", " ").strip() or "(empty message)"
        if len(preview) > 240:
            preview = preview[:239] + "…"
        files = parse_diff(self._diff_text)
        additions = sum(item.additions for item in files)
        deletions = sum(item.deletions for item in files)
        if files:
            title = (
                f"Changes · {len(files)} file(s) · "
                f"+{additions} -{deletions}"
            )
        else:
            title = "Changes"
        hint = "j/k or ↑/↓ scroll · g/G top/bottom · Esc close"
        if self._can_revert:
            hint = "j/k or ↑/↓ scroll · r revert · Esc close"
        with Vertical(id="diff-panel"):
            yield Static(title, id="diff-title")
            yield Static(preview, id="diff-prompt")
            if looks_mojibake(self._diff_text):
                yield Static(
                    "File content looks double-encoded (mojibake); "
                    "this is in the file, not the diff view.",
                    id="diff-warning",
                )
            yield RichLog(
                id="diff-body",
                wrap=False,
                markup=False,
                highlight=False,
                auto_scroll=False,
            )
            with Horizontal(id="diff-actions"):
                yield Static(hint, id="diff-hint")
                if self._can_revert:
                    yield Static(
                        "Revert", id="diff-revert", classes="diff-action"
                    )
                yield Static("Close", id="diff-close", classes="diff-action")

    def on_mount(self) -> None:
        body = self.query_one("#diff-body", RichLog)
        width = max(20, self.app.size.width - 8)
        lines = diff_lines(self._diff_text, width=width)
        if not lines:
            body.write(Text("No text changes (binary or empty).", style="dim"))
        else:
            for line in lines:
                body.write(line)
        body.scroll_home(animate=False)
        body.focus()

    def on_click(self, event) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "diff-close":
            self.dismiss(None)
        elif widget_id == "diff-revert":
            self.action_revert()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_revert(self) -> None:
        if self._can_revert:
            self.dismiss(REVERT)

    def action_scroll_down(self) -> None:
        self.query_one("#diff-body", RichLog).scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self.query_one("#diff-body", RichLog).scroll_up(animate=False)

    def action_home(self) -> None:
        self.query_one("#diff-body", RichLog).scroll_home(animate=False)

    def action_end(self) -> None:
        self.query_one("#diff-body", RichLog).scroll_end(animate=False)
