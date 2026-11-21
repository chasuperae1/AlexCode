"""Alex 的终端聊天界面：复用 termui 组件库 + AgentScope 流式事件。

运行：``python -m alex.tui``
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import frontmatter
from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual._wrap import compute_wrap_offsets
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Markdown, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from termui import PromptInput as TermPromptInput, TermUIApp

from .ask_choice import AskChoice
from .client import DaemonClient, LinkError, LocalLink, ensure_daemon, prewarm_daemon
from .confirm import ALLOW, ALWAYS, DENY, ConfirmScreen
from .config import CONFIG, ROOT, reload_config, workspace_path
from .diffs import DiffScreen, diff_lines, parse_diff, render_diff
from .dir_picker import DirectoryPicker
from .images import grab_clipboard_image, split_images
from .message_actions import COPY, REVERT, MessageActionsScreen
from .permission_screen import PermissionScreen
from .picker import PickerScreen
from .settings_screen import SettingsScreen
from .snapshots import Checkpoint
from .themes import DEFAULT_THEME, THEMES, next_theme, theme_label, theme_name

LOCAL_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "PowerShell", "Bash"}
DEBUG_KINDS = {"think", "tool", "mcp", "skill", "rule", "result", "error"}
USER = "user"
ASSISTANT = "assistant"
_IMAGE_TOKEN = re.compile(r"\[image-\d+\]")
PROMPT_PLACEHOLDER = "Message…  Enter to send · Shift+Enter newline · F6 paste image"
QUEUE_CARD_LIMIT = 10
UI_STATE_PATH = ROOT / "data" / "tui.json"


def _load_ui_state() -> dict:
    """TUI 自己的偏好（主题等），与 config.json 分开，避免被 daemon 回写覆盖。"""
    try:
        data = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_ui_state(**updates) -> None:
    data = _load_ui_state()
    data.update(updates)
    try:
        UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_STATE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


class PromptInput(TermPromptInput):
    """输入框：图片占位符用独立配色，退格整体删除。"""

    TOKEN_STYLE = Style(color="#c7d0ff", bgcolor="#2b3350", bold=True)
    # 输入框最多显示多少行内容（不含上下边框）
    MAX_VISIBLE_LINES = 6
    _last_height: int | None = None
    _last_highlight_text: str | None = None

    def _sync_height(self) -> None:
        """按实际显示行数（含自动换行）调整高度，最多 6 行。"""
        try:
            width = self.scrollable_content_region.size.width - self.gutter_width
        except Exception:
            width = 0
        if width <= 0:
            super()._sync_height()
            return
        try:
            lines = sum(
                len(compute_wrap_offsets(line, width, tab_size=4)) + 1
                for line in self.document.lines
            )
        except Exception:
            super()._sync_height()
            return
        lines = min(self.MAX_VISIBLE_LINES, max(1, lines))
        height = lines + 2  # 上下边框
        # 高度未变时跳过赋值，避免触发不必要的布局重排
        if height == self._last_height:
            return
        self._last_height = height
        if self.styles.height != height:
            self.styles.height = height

    def on_resize(self) -> None:
        self._last_height = None  # 窗口大小变化时清缓存
        self._sync_height()

    def action_paste(self) -> None:
        # TextArea.action_paste 是同步方法，异步逻辑放 worker 里跑
        self.run_worker(self._paste(), exclusive=True)

    async def _paste(self) -> None:
        handler = getattr(self.app, "paste_clipboard_image", None)
        if handler is not None and await handler():
            return
        super().action_paste()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._apply_token_style()

    def _apply_token_style(self) -> None:
        theme = getattr(self, "_theme", None)
        if theme is not None:
            theme.syntax_styles["image-token"] = self.TOKEN_STYLE

    def _set_theme(self, theme: str) -> None:
        super()._set_theme(theme)
        self._apply_token_style()

    def _build_highlight_map(self) -> None:
        super()._build_highlight_map()
        highlights = getattr(self, "_highlights", None)
        document = getattr(self, "document", None)
        if highlights is None or document is None:
            return
        # 文本未变时跳过高亮重算
        current_text = document.text
        if current_text == self._last_highlight_text:
            return
        self._last_highlight_text = current_text
        # 位置按 UTF-8 字节计算，和 TextArea 渲染高亮的方式一致
        for row, text in enumerate(document.lines):
            text = str(text)
            for match in _IMAGE_TOKEN.finditer(text):
                start = len(text[: match.start()].encode("utf-8"))
                end = len(text[: match.end()].encode("utf-8"))
                highlights[row].append((start, end, "image-token"))

    async def _on_key(self, event) -> None:
        # TextArea 自带 F6 = 选中当前行，这里改成粘贴剪贴板图片
        if event.key == "f6":
            event.stop()
            event.prevent_default()
            self.action_paste()
            return
        if event.key in {"backspace", "ctrl+h"} and self._delete_image_token():
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    def _delete_image_token(self) -> bool:
        """光标紧跟在 [image-N] 后面时，整个占位符一次删掉。"""
        selection = self.selection
        if selection.start != selection.end:
            return False
        row, col = self.cursor_location
        line = str(self.document.get_line(row))
        match = re.search(r"\[image-\d+\]$", line[:col])
        if match is None:
            return False
        self.delete((row, match.start()), (row, col))
        return True


class UserMessage(Static):
    """可点击的用户消息，点击后打开消息操作菜单。"""

    def __init__(
        self,
        renderable,
        *,
        exchange_id: int | None = None,
        message_text: str = "",
        **kwargs,
    ) -> None:
        super().__init__(renderable, **kwargs)
        self.exchange_id = exchange_id
        self.message_text = message_text


class ChangesSummary(Static):
    """每轮回复后的文件变更摘要，点击查看 diff。"""

    def __init__(self, checkpoint: Checkpoint) -> None:
        super().__init__(
            _changes_label(checkpoint), classes="changes-summary"
        )
        self.exchange_id = checkpoint.exchange_id


class InlineDiffCard(Vertical):
    """一轮回复后的整轮 diff（历史回放用）：默认收起，展开后封顶滚动。"""

    can_focus = True

    BINDINGS = [
        Binding("space", "toggle", "Expand", show=False),
        Binding("enter", "toggle", "Expand", show=False),
    ]

    DEFAULT_CSS = """
    InlineDiffCard {
        height: auto;
        margin: 0 0 1 2;
        padding: 0 1;
        background: transparent;
        border: round $primary 22%;
    }
    InlineDiffCard .diff-head { height: 1; }
    InlineDiffCard .diff-head:hover { background: $boost; }
    InlineDiffCard .diff-title { width: 1fr; }
    InlineDiffCard .diff-count { width: auto; }
    InlineDiffCard .diff-hint { width: auto; color: $text-muted; }
    InlineDiffCard .diff-wrap { height: auto; max-height: 11; display: none; }
    InlineDiffCard.-open .diff-wrap { display: block; }
    InlineDiffCard .diff-legend { height: 1; color: $text-muted; }
    InlineDiffCard .diff-body { height: auto; padding: 0 0 1 0; }
    InlineDiffCard:focus, InlineDiffCard:focus-within { border: round $primary; }
    """

    def __init__(self, checkpoint: Checkpoint, theme, **kwargs) -> None:
        super().__init__(classes="inline-diff-card", **kwargs)
        self._checkpoint = checkpoint
        self._theme = theme
        self._open = False
        self._title = Static(classes="diff-title")
        self._count = Static(classes="diff-count")
        self._hint = Static(self._hint_text(), classes="diff-hint")
        self._head = Horizontal(
            self._title, self._count, self._hint, classes="diff-head"
        )
        self._wrap = VerticalScroll(classes="diff-wrap")
        self._body = Static(Text("Loading diff…", style="dim"), classes="diff-body")
        self._refresh_header()

    def _hint_text(self) -> Text:
        label = "空格 收起" if self._open else "空格 展开"
        return Text(label, style=f"dim {self._theme.secondary}")

    def compose(self) -> ComposeResult:
        yield self._head
        with self._wrap:
            legend = Text()
            legend.append("− deleted", style="red")
            legend.append("   ")
            legend.append("+ added", style="green")
            legend.append("   ·   ", style="dim")
            legend.append("line numbers: before / after", style="dim")
            yield Static(legend, classes="diff-legend")
            yield self._body

    def _refresh_header(self) -> None:
        checkpoint = self._checkpoint
        count = len(checkpoint.files)
        title = Text("▤  File changes", style=f"bold {self._theme.primary}")
        title.append(f" · {count} file{'s' if count != 1 else ''}", style="dim")
        counts = Text()
        counts.append(f"+{checkpoint.additions}", style="green")
        counts.append("  ")
        counts.append(f"−{checkpoint.deletions}", style="red")
        self._title.update(title)
        self._count.update(counts)

    def set_diff(self, text: str) -> None:
        files = parse_diff(text)
        if not files:
            self._body.update(Text("No text changes (binary or empty).", style="dim"))
            return

        result = Text()
        for file_index, item in enumerate(files):
            if file_index:
                result.append("\n")

            file_head = Text()
            file_head.append(f"▤  {item.path or '(unknown)'}", style=f"bold {self._theme.primary}")
            file_head.append(f"   +{item.additions}  −{item.deletions}", style="dim")
            if file_index == 0:
                result.append_text(file_head)
            else:
                result.append_text(file_head)

            if item.binary:
                result.append("\n")
                result.append(Text("  binary file", style="dim italic"))
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
                result.append("\n")
                if entry.kind == "hunk":
                    line = Text(entry.text, style="bold #8fa1d9")
                elif entry.kind == "meta":
                    line = Text(entry.text, style="dim italic")
                else:
                    old = (
                        f"{entry.old_no:>{number_width}}"
                        if entry.old_no is not None
                        else " " * number_width
                    )
                    new = (
                        f"{entry.new_no:>{number_width}}"
                        if entry.new_no is not None
                        else " " * number_width
                    )
                    if entry.kind == "add":
                        line = Text(style="green on #12261a")
                        line.append(f"{old} {new}  + ", style="green on #12261a")
                        line.append(entry.text, style="green on #12261a")
                    elif entry.kind == "del":
                        line = Text(style="red on #2a1416")
                        line.append(f"{old} {new}  − ", style="red on #2a1416")
                        line.append(entry.text, style="red on #2a1416")
                    else:
                        line = Text(style="dim")
                        line.append(f"{old} {new}    {entry.text}")
                result.append_text(line)

        self._body.update(result)

    def set_error(self, message: str) -> None:
        self._body.update(Text(f"Could not load diff: {message}", style="yellow"))

    def action_toggle(self) -> None:
        self._open = not self._open
        self.set_class(self._open, "-open")
        self._hint.update(self._hint_text())
        if self._open:
            self._wrap.scroll_home(animate=False)
            self._wrap.focus()
        else:
            self.focus()

    def on_click(self, event) -> None:
        target = event.widget
        while target is not None:
            if target is self._head:
                self.action_toggle()
                event.stop()
                return
            target = target.parent


_TDC_MAX_LINES = 11


class ToolDiffCard(Vertical):
    """单个 Edit / Write 完成后的 diff：默认收起一行，展开限高、超出内部滚动。"""

    can_focus = True

    BINDINGS = [
        Binding("space", "toggle", "Expand", show=False),
        Binding("enter", "toggle", "Expand", show=False),
    ]

    DEFAULT_CSS = """
    ToolDiffCard {
        height: auto;
        margin: 0 0 1 2;
        padding: 0 1;
        background: transparent;
        border: round $primary 22%;
    }
    ToolDiffCard .tdc-head { height: 1; }
    ToolDiffCard .tdc-head:hover { background: $boost; }
    ToolDiffCard .tdc-path { width: 1fr; }
    ToolDiffCard .tdc-counts { width: auto; }
    ToolDiffCard .tdc-hint { width: auto; padding: 0 1 0 2; color: $text-muted; }
    ToolDiffCard .tdc-body { height: auto; display: none; }
    ToolDiffCard.-open .tdc-body { display: block; }
    ToolDiffCard .tdc-legend { height: 1; color: $text-muted; }
    ToolDiffCard .tdc-code {
        height: 1;
        background: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    ToolDiffCard .tdc-foot { height: 1; }
    ToolDiffCard .tdc-foot-text { width: 1fr; color: $text-muted; }
    ToolDiffCard .tdc-foot-hint { width: auto; color: $text-muted; }
    ToolDiffCard:focus, ToolDiffCard:focus-within { border: round $primary; }
    """

    def __init__(
        self,
        path: str,
        counts: tuple[int, int],
        lines: list[Text],
        clock: str,
        theme,
        **kwargs,
    ) -> None:
        super().__init__(classes="tool-diff", **kwargs)
        self._path = path
        self._additions, self._deletions = counts
        self._lines = lines
        self._clock = clock
        self._theme = theme
        self._open = False
        self._path_static = Static(self._header_path(), classes="tdc-path")
        self._counts_static = Static(self._header_counts(), classes="tdc-counts")
        self._hint_static = Static(self._hint_text(), classes="tdc-hint")
        self._head = Horizontal(
            self._path_static,
            self._counts_static,
            self._hint_static,
            classes="tdc-head",
        )
        self._code = RichLog(
            wrap=False,
            markup=False,
            highlight=False,
            auto_scroll=False,
            classes="tdc-code",
        )
        self._foot_text = Static(self._foot_label(), classes="tdc-foot-text")
        self._foot_hint = Static("空格 收起 · Esc 返回", classes="tdc-foot-hint")

    # ------------------------------------------------------------ 文案
    def _header_path(self) -> Text:
        text = Text()
        text.append("▤ ", style=f"bold {self._theme.primary}")
        text.append(self._path, style=self._theme.foreground)
        return text

    def _header_counts(self) -> Text:
        text = Text()
        text.append(f"+{self._additions}", style=self._theme.success)
        text.append("  ")
        text.append(f"−{self._deletions}", style=self._theme.error)
        return text

    def _hint_text(self) -> Text:
        label = "空格 收起" if self._open else "空格 展开"
        return Text(label, style=f"dim {self._theme.secondary}")

    def _foot_label(self) -> Text:
        text = Text()
        text.append(
            f"修改完成 {self._clock} · ", style=f"dim {self._theme.secondary}"
        )
        text.append(f"+{self._additions}", style=self._theme.success)
        text.append(" 新增  ", style=f"dim {self._theme.secondary}")
        text.append(f"−{self._deletions}", style=self._theme.error)
        text.append(" 删除", style=f"dim {self._theme.secondary}")
        return text

    def _legend(self) -> Text:
        text = Text()
        text.append("−", style=self._theme.error)
        text.append(" 删除   ", style=f"dim {self._theme.secondary}")
        text.append("+", style=self._theme.success)
        text.append(" 新增   ", style=f"dim {self._theme.secondary}")
        text.append("行号：修改前 / 修改后", style=f"dim {self._theme.secondary}")
        return text

    # ------------------------------------------------------------ 组装
    def compose(self) -> ComposeResult:
        yield self._head
        with Vertical(classes="tdc-body"):
            yield Static(self._legend(), classes="tdc-legend")
            yield self._code
            with Horizontal(classes="tdc-foot"):
                yield self._foot_text
                yield self._foot_hint

    def on_mount(self) -> None:
        if not self._lines:
            self._code.write(Text("No text changes (binary or empty).", style="dim"))
            return
        for line in self._lines:
            self._code.write(line)

    # ------------------------------------------------------------ 交互
    def action_toggle(self) -> None:
        self._open = not self._open
        self.set_class(self._open, "-open")
        self._hint_static.update(self._hint_text())
        if self._open:
            height = min(max(1, len(self._lines)), _TDC_MAX_LINES)
            self._code.styles.height = height
            self._code.scroll_home(animate=False)
            self._code.focus()
            # 展开后把卡片拉回视野：ChatLog 里还套了一层容器，直接 scroll_visible 无效
            try:
                chat = self.app.query_one("#chat", ChatLog)
                # 高度刚变，等布局刷两帧再滚，否则读到的是旧位置
                self.call_after_refresh(
                    self.call_after_refresh, self._reveal_in_chat, chat
                )
            except Exception:
                pass
        else:
            self._code.styles.height = 1
            self.focus()

    def _reveal_in_chat(self, chat: "ChatLog") -> None:
        """展开后让整张卡片落在视野内：能放下就最小滚动，放不下则顶对齐。"""
        content_top = chat.content_region.y
        content_bottom = chat.content_region.bottom
        delta = 0
        if self.region.bottom > content_bottom:
            delta = self.region.bottom - content_bottom
        if self.region.y - delta < content_top:
            delta = self.region.y - content_top
        if delta:
            chat.scroll_to(y=chat.scroll_offset.y + delta, animate=False)

    def on_click(self, event) -> None:
        target = event.widget
        while target is not None:
            if target is self._head:
                self.action_toggle()
                event.stop()
                return
            target = target.parent


def _format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def _changes_text(checkpoint: Checkpoint) -> str:
    count = len(checkpoint.files)
    label = f"{count} file{'s' if count != 1 else ''} changed"
    if checkpoint.additions or checkpoint.deletions:
        label += f" · +{checkpoint.additions} -{checkpoint.deletions}"
    return label


def _changes_label(checkpoint: Checkpoint) -> Text:
    return Text(
        f"✎ {_changes_text(checkpoint)} · click to view diff",
        style="dim",
    )


_BOX_START = set("┌╭╔")
_BOX_END = set("└╰╚")
_BOX_MID = set("├╞╟")
_BOX_V = set("│║")


def _box_rows_to_markdown(block: list[str]) -> str | None:
    """把一段框线表格转成 Markdown 管道表格；结构不合法时返回 None。"""
    groups: list[list[list[str]]] = [[]]
    for raw in block:
        stripped = raw.strip()
        if not stripped or stripped[0] in _BOX_START | _BOX_END:
            continue
        if stripped[0] in _BOX_MID:
            groups.append([])
            continue
        if stripped[0] not in _BOX_V:
            return None
        cells = [cell.strip() for cell in re.split(r"[│║]", stripped.strip("│║"))]
        rows = groups[-1]
        if rows and not cells[0] and any(cells[1:]):
            # 单元格内换行：首列为空、其余列有内容的行并入上一行
            cells.extend("" for _ in range(len(rows[-1]) - len(cells)))
            rows[-1] = [
                f"{old} {new}".strip() if new else old
                for old, new in zip(rows[-1], cells)
            ]
        else:
            rows.append(cells)
    groups = [rows for rows in groups if rows]
    if not groups:
        return None
    header = groups[0][0]
    body = groups[0][1:]
    for rows in groups[1:]:
        body.extend(rows)
    if not any(header) or not body:
        return None
    columns = len(header)

    def line(cells: list[str]) -> str:
        cells = (cells + [""] * columns)[:columns]
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"

    divider = "| " + " | ".join("---" for _ in range(columns)) + " |"
    return "\n".join([line(header), divider] + [line(row) for row in body])


def normalize_tables(source: str) -> str:
    """把手绘框线表格转成 Markdown 管道表格。

    Rich 的 Markdown 渲染会把连续的框线行当成普通段落、按空格合并，
    终端里就变成错位的长行；管道表格才能正常排版。
    """
    if not any(char in source for char in _BOX_START):
        return source
    lines = source.splitlines()
    out: list[str] = []
    index = 0
    fenced = False
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
        elif not fenced and stripped[:1] in _BOX_START:
            end = index + 1
            while end < len(lines) and lines[end].lstrip()[:1] not in _BOX_END:
                end += 1
            if end < len(lines):
                table = _box_rows_to_markdown(lines[index : end + 1])
                if table:
                    if out and out[-1].strip():
                        out.append("")
                    out.extend([table, ""])
                    index = end + 1
                    continue
        out.append(line)
        index += 1
    return "\n".join(out)


def markdown_to_text(source: str, width: int) -> Text:
    """把 Markdown 渲染成等宽排版后的 ``Text``。

    Textual 会把 Rich 渲染对象包成 ``RichVisual``，既不画选区高亮、也拿不到
    选区文本；转成 ``Text``（``Content`` 视觉）后由框架原生支持选中与复制。
    """
    from rich.markdown import Markdown as RichMarkdown

    source = normalize_tables(source)

    console = Console(
        width=width,
        color_system="truecolor",
        force_terminal=False,
        legacy_windows=False,
        highlight=False,
    )
    try:
        lines = console.render_lines(
            RichMarkdown(source), console.options.update(width=width), pad=False
        )
    except Exception:
        return Text(source)
    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        keep = len("".join(segment.text for segment in line).rstrip())
        for segment in line:
            if keep <= 0:
                break
            part = segment.text[:keep]
            text.append(part, segment.style)
            keep -= len(part)
    return text


class MarkdownStatic(Static):
    """Markdown 文本块：渲染为 ``Text``，可直接选中复制；流式阶段接收 ``Text``。"""

    def __init__(self, renderable="", **kwargs) -> None:
        super().__init__(Text(""), **kwargs)
        self._markdown: str | None = None
        self._render_width = -1
        self.update(renderable)

    def update(self, renderable="", **kwargs) -> None:
        if isinstance(renderable, str):
            self._markdown = renderable
            self._render_width = -1
            self._render_markdown()
            return
        self._markdown = None
        super().update(renderable, **kwargs)

    def on_mount(self) -> None:
        self._render_width = -1
        self._render_markdown()

    def on_resize(self, event) -> None:
        # 宽度变化要重新排版，否则长行折行位置会错
        if self._markdown is not None:
            self._render_markdown(event.size.width)

    def _render_markdown(self, width: int | None = None) -> None:
        if self._markdown is None:
            return
        if width is None:
            width = self.content_region.width or self.size.width
        if width <= 0 or width == self._render_width:
            return
        self._render_width = width
        super().update(markdown_to_text(self._markdown, width))


class ChatLog(VerticalScroll):
    """聊天区：增量挂载消息控件（Markdown 渲染），自动滚到底部。

    只更新正在流式输出的那一个控件，避免整段对话反复重排。
    """

    _suspend_scroll = False

    DEFAULT_CSS = """
    ChatLog {
        width: 1fr;
        height: 1fr;
        background: $background;
        padding: 1 1 0 1;
        scrollbar-size-vertical: 0;
    }
    ChatLog #chat-body {
        height: auto;
        width: 1fr;
    }
    ChatLog .md {
        background: $background;
        margin: 0 0 1 0;
        padding: 0;
    }
    ChatLog .role {
        color: $primary;
        text-style: bold;
        margin-top: 1;
    }
    ChatLog .role-user {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }
    ChatLog .user-bubble {
        background: $primary 18%;
        border: round $primary 18%;
        padding: 0 1;
        margin: 0;
    }
    ChatLog .user-bubble:hover {
        background: $primary 32%;
        border: round $primary 32%;
    }
    ChatLog .changes-summary {
        background: $background;
        color: $text-muted;
        padding: 0 2;
        margin: 0 0 1 0;
    }
    ChatLog .changes-summary:hover {
        color: $primary;
        text-style: underline;
    }
    ChatLog .compaction-title {
        margin-top: 1;
        color: $secondary;
        text-style: bold;
    }
    ChatLog .compaction-body {
        border-left: thick $secondary 40%;
        padding-left: 1;
    }
    ChatLog .turn-stats {
        background: $background;
        color: $text-muted;
        padding: 0;
        margin: 0 0 1 0;
        text-align: right;
    }
    ChatLog .ask-history {
        height: auto;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    ChatLog .compacting {
        height: 1;
        margin: 0 0 1 0;
        padding: 0;
    }
    ChatLog .debug {
        background: $background;
        border: none;
        padding-left: 0;
        margin: 1 2 0 0;
    }
    ChatLog .debug:focus-within {
        background-tint: $background 0%;
    }
    ChatLog .debug CollapsibleTitle {
        background: $background;
    }
    ChatLog .debug CollapsibleTitle:hover,
    ChatLog .debug CollapsibleTitle:focus {
        background: $background;
        color: $foreground;
    }
    ChatLog .debug-body {
        height: auto;
        padding: 0 1;
    }
    ChatLog .tool-line {
        height: auto;
        padding: 0 1;
        margin: 0;
    }
    ChatLog .tool-line-verifier {
        padding: 0 1 0 3;
    }
    ChatLog .role-verifier {
        color: $secondary;
        text-style: bold;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(id="chat-body")

    def clear(self) -> None:
        body = self.query_one("#chat-body")
        for child in list(body.children):
            child.remove()

    def on_click(self, event) -> None:
        if isinstance(event.widget, UserMessage):
            event.stop()
            handler = getattr(self.app, "open_message_actions", None)
            if handler is not None:
                handler(event.widget)
        elif isinstance(event.widget, ChangesSummary):
            event.stop()
            handler = getattr(self.app, "open_change_diff", None)
            if handler is not None:
                handler(event.widget)

    def add_static(self, renderable, classes: str = "") -> Static:
        widget = Static(renderable, classes=classes)
        self._mount(widget)
        return widget

    def add_markdown(self, content: str) -> Markdown:
        widget = Markdown(content, classes="md")
        self._mount(widget)
        return widget

    def add_widget(self, widget) -> None:
        self._mount(widget)

    def _mount(self, widget) -> None:
        self.query_one("#chat-body").mount(widget)
        if not self._suspend_scroll:
            self.call_after_refresh(self._pin_to_bottom)

    def _pin_to_bottom(self) -> None:
        """新控件挂上后贴底：高度要等布局完才算准，所以刷两帧。"""
        self.scroll_end(animate=False)
        self.call_after_refresh(self.scroll_end, animate=False)


class QueueButton(Static):
    """队列卡片底部的可点击动作。"""

    class Clicked(Message):
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, label: str, action: str, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._action = action

    def on_click(self, event) -> None:
        self.post_message(self.Clicked(self._action))
        event.stop()


class QueueCard(Vertical):
    """任务队列卡片：确认、留档、结果三种形态共用一套布局。"""

    DEFAULT_CSS = """
    QueueCard {
        height: auto;
        margin: 1 0 0 0;
        padding: 0 1;
        background: $background;
        border: round $primary 35%;
    }
    QueueCard .q-head { height: 1; }
    QueueCard .q-title { width: auto; }
    QueueCard .q-gap { width: 1fr; height: 1; }
    QueueCard .q-meta { width: auto; }
    QueueCard .q-hint { height: 1; color: $text-muted; padding-left: 3; }
    QueueCard .q-rows { height: auto; }
    QueueCard .q-warn { height: auto; }
    QueueCard .q-foot { height: 1; margin-top: 1; }
    QueueCard .q-act { width: auto; }
    QueueCard .q-key { width: auto; color: $text-muted; }
    QueueCard .q-sep { width: auto; color: $text-muted; }
    QueueCard .q-note { width: auto; color: $text-muted; }
    QueueCard .q-start { color: $primary; text-style: underline; }
    QueueCard .q-cancel { color: $text-muted; text-style: underline; }
    """

    def __init__(
        self,
        tasks: list[str],
        state: str = "confirming",
        results: list | None = None,
        seconds: int = 0,
        theme=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._tasks = [str(item) for item in tasks]
        self._results = results
        self._state = state
        self._seconds = seconds
        self._theme = theme
        self._offset = 0
        self._limit = QUEUE_CARD_LIMIT
        self._armed = False
        self._title = Static(classes="q-title")
        self._meta = Static(classes="q-meta")
        self._top = Static(classes="q-hint")
        self._rows = Static(classes="q-rows")
        self._bottom = Static(classes="q-hint")
        self._warn = Static(classes="q-warn")
        self._start = QueueButton("Start", "start", classes="q-act q-start")
        self._start_key = Static("(enter)", classes="q-key")
        self._sep = Static("·", classes="q-sep")
        self._cancel = QueueButton("Cancel", "cancel", classes="q-act q-cancel")
        self._cancel_key = Static("(esc)", classes="q-key")
        self._note = Static("", classes="q-note")
        self._foot = Horizontal(classes="q-foot")
        self._refresh()

    @property
    def state(self) -> str:
        return self._state

    def compose(self) -> ComposeResult:
        with Horizontal(classes="q-head"):
            yield self._title
            yield Static("", classes="q-gap")
            yield self._meta
        yield self._top
        yield self._rows
        yield self._bottom
        yield self._warn
        with self._foot:
            yield self._start
            yield self._start_key
            yield self._sep
            yield self._cancel
            yield self._cancel_key
            yield Static("", classes="q-gap")
            yield self._note

    def set_state(self, state: str) -> None:
        self._state = state
        if state != "confirming":
            self._armed = False
        self._refresh()

    def set_armed(self, armed: bool) -> None:
        if self._state != "confirming" or self._results is not None:
            return
        self._armed = armed
        self._refresh()

    def shift(self, delta: int) -> None:
        if self._results is not None or len(self._tasks) <= self._limit:
            return
        limit = max(0, len(self._tasks) - self._limit)
        offset = max(0, min(limit, self._offset + delta))
        if offset == self._offset:
            return
        self._offset = offset
        self._render_rows()

    def on_mouse_scroll_down(self, event) -> None:
        self.shift(1)
        event.stop()

    def on_mouse_scroll_up(self, event) -> None:
        self.shift(-1)
        event.stop()

    # ---------------------------------------------------------------- 渲染
    def _refresh(self) -> None:
        theme = self._theme
        count = len(self._results) if self._results is not None else len(self._tasks)
        suffix = "task" if count == 1 else "tasks"
        self._title.update(
            Text(f"Queue · {count} {suffix}", style=f"bold {theme.primary}")
        )
        self._meta.update(Text(self._meta_text(), style=theme.secondary))
        self._render_rows()
        self._render_footer()

    def _meta_text(self) -> str:
        if self._results is not None:
            done = sum(1 for item in self._results if item[1] == "done")
            failed = sum(1 for item in self._results if item[1] == "failed")
            skipped = sum(1 for item in self._results if item[1] == "skipped")
            text = f"FINISHED · {done} DONE"
            if failed:
                text += f" · {failed} FAILED"
            if skipped:
                text += f" · {skipped} SKIPPED"
            return text
        if self._state == "confirming":
            return "CANCEL REQUESTED" if self._armed else "AWAITING CONFIRMATION"
        return {
            "started": "STARTED",
            "cancelled": "NOT STARTED",
            "done": "DONE",
        }.get(self._state, "")

    def _render_rows(self) -> None:
        theme = self._theme
        if self._results is not None:
            text = Text()
            for index, item in enumerate(self._results, 1):
                subject = str(item[0])
                status = str(item[1])
                reason = str(item[2]) if len(item) > 2 else ""
                if index > 1:
                    text.append("\n")
                if status == "done":
                    text.append("✓ ", style=f"bold {theme.success}")
                    subject_style = theme.foreground
                elif status == "failed":
                    text.append("✗ ", style=f"bold {theme.error}")
                    subject_style = theme.error
                else:
                    text.append("– ", style=theme.secondary)
                    subject_style = theme.secondary
                text.append(f"{index:>2}  ", style=theme.secondary)
                text.append(subject, style=subject_style)
                if reason and status == "failed":
                    text.append(f" — {reason}", style=theme.secondary)
            self._rows.update(text)
            self._top.display = False
            self._bottom.display = False
            return

        total = len(self._tasks)
        scrollable = total > self._limit
        start = self._offset if scrollable else 0
        visible = (
            self._tasks[start : start + self._limit]
            if scrollable
            else self._tasks
        )
        text = Text()
        for index, subject in enumerate(visible, start=start + 1):
            if index > start + 1:
                text.append("\n")
            text.append(f"{index:>2}  ", style=theme.secondary)
            text.append(subject, style=theme.foreground)
        self._rows.update(text)

        above = start
        below = total - (start + len(visible))
        self._top.display = scrollable and above > 0
        self._bottom.display = scrollable and below > 0
        self._top.update(
            Text(f"↑ {above} more above", style=f"dim {theme.secondary}")
            if above
            else Text("")
        )
        self._bottom.update(
            Text(f"↓ {below} more below", style=f"dim {theme.secondary}")
            if below
            else Text("")
        )

    def _render_footer(self) -> None:
        theme = self._theme
        confirming = self._state == "confirming" and self._results is None
        for widget in (
            self._start,
            self._start_key,
            self._sep,
            self._cancel,
            self._cancel_key,
        ):
            widget.display = confirming
        self._warn.display = confirming and self._armed
        if confirming:
            self._warn.update(
                Text(
                    f"Discarding clears these {len(self._tasks)} tasks. "
                    "They cannot be resumed.",
                    style=theme.warning,
                )
            )
            if self._armed:
                self._cancel.update(Text("Discard", style=f"underline {theme.warning}"))
                self._cancel_key.update("(esc again)")
            else:
                self._cancel.update(Text("Cancel", style=f"underline {theme.secondary}"))
                self._cancel_key.update("(esc)")

        note = ""
        if self._results is not None:
            note = f"total {self._format_seconds(self._seconds)}"
        elif self._state == "started":
            note = "esc to stop"
        elif self._state == "cancelled":
            note = "nothing was run"
        self._note.update(note)
        self._foot.display = confirming or bool(note)

    @staticmethod
    def _format_seconds(total: int) -> str:
        total = max(0, int(total))
        if total < 60:
            return f"{total}s"
        minutes, seconds = divmod(total, 60)
        if minutes < 60:
            return f"{minutes}m {seconds:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"


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

    def update_payload(self, payload: dict, theme=None) -> None:
        self._payload = payload
        if theme is not None:
            self._theme = theme
        self._sync_classes()
        self._body.update(self._render_content())
        self._refresh_actions()

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


_BOOT_SPINNER = "◐◓◑◒"
# 启动页顶部的字符画（宽度约 67，窗口太小时自动省略；按行上下渐变色）
_BOOT_BANNER = [
    "   █████╗ ██╗     ███████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗",
    "  ██╔══██╗██║     ██╔════╝╚██╗██╔╝██╔════╝██╔═══██╗██╔══██╗██╔════╝",
    "  ███████║██║     █████╗   ╚███╔╝ ██║     ██║   ██║██║  ██║█████╗",
    "  ██╔══██║██║     ██╔══╝   ██╔██╗ ██║     ██║   ██║██║  ██║██╔══╝",
    "  ██║  ██║███████╗███████╗██╔╝ ██╗╚██████╗╚██████╔╝██████╔╝███████╗",
    "  ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
]
# 启动面板的显示顺序：后两项（Skills & rules、Session history）提前
_BOOT_ORDER = ("skills", "session", "daemon", "model", "mcp")
# 阶段未上报时的占位文案，保证加载页从第一帧就是固定布局
_BOOT_LABELS = {
    "skills": "Skills & rules",
    "session": "Session history",
    "daemon": "Background daemon",
    "model": "Model & tools",
    "mcp": "MCP servers",
}


def _hex_rgb(value: str) -> tuple[int, int, int] | None:
    """把 #rrggbb 解析成 RGB；非十六进制颜色返回 None。"""
    value = value.strip()
    if len(value) != 7 or not value.startswith("#"):
        return None
    try:
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None


def _gradient_banner(lines: list[str], start: str, end: str) -> Text:
    """字符画按行做上下渐变，颜色无法解析时退回单色。"""
    start_rgb = _hex_rgb(start)
    end_rgb = _hex_rgb(end)
    if start_rgb is None or end_rgb is None:
        return Text("\n".join(lines), style=start)
    total = max(1, len(lines) - 1)
    text = Text()
    for index, line in enumerate(lines):
        ratio = index / total
        rgb = tuple(
            round(a + (b - a) * ratio) for a, b in zip(start_rgb, end_rgb)
        )
        if index:
            text.append("\n")
        text.append(line, style="#{:02x}{:02x}{:02x}".format(*rgb))
    return text


def _render_boot_panel(
    stages: list[dict],
    frame: int = 0,
    theme=None,
    show_banner: bool = True,
):
    """启动面板：字符画 + 阶段列表 + 进度条 + 锁定提示。颜色跟随当前主题。"""
    from rich.console import Group
    from rich.table import Table

    primary = str(getattr(theme, "primary", "") or "#6d93ff")
    accent = str(getattr(theme, "accent", "") or primary)
    muted = str(getattr(theme, "secondary", "") or "#8a90a0")
    success = str(getattr(theme, "success", "") or "#5fb98a")
    warning = str(getattr(theme, "warning", "") or "#d9b26a")
    error = str(getattr(theme, "error", "") or "#e8788a")
    track = str(getattr(theme, "panel", "") or "#262a33")

    top: list = []
    if show_banner:
        top = [_gradient_banner(_BOOT_BANNER, primary, accent), Text("")]

    head = Text()
    head.append("Starting Alex", style="bold")
    head.append("   please wait", style="dim")

    lock = Text(
        "⌁ Input is locked until loading finishes",
        style=muted,
    )

    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=2)
    table.add_column(ratio=1)
    table.add_column(justify="right")

    finished = 0
    for stage in stages:
        status = str(stage.get("status", "idle"))
        label = str(stage.get("label", ""))
        detail = str(stage.get("detail", ""))
        if status == "done":
            dot, color, name_style, state_style = "●", success, muted, muted
            state = "ready"
            finished += 1
        elif status == "warn":
            dot, color, name_style, state_style = (
                "●", warning, warning, warning
            )
            state = detail or "warning"
            finished += 1
        elif status == "failed":
            dot, color, name_style, state_style = "●", error, error, error
            state = detail or "failed"
            finished += 1
        elif status == "active":
            dot = _BOOT_SPINNER[frame % len(_BOOT_SPINNER)]
            color, name_style, state_style = primary, "bold", primary
            state = "starting…"
        else:
            dot, color, name_style, state_style = "○", muted, muted, muted
            state = "waiting"
        table.add_row(
            Text(dot, style=color),
            Text(label, style=name_style),
            Text(state, style=state_style),
        )

    total = max(1, len(stages))
    filled = int(34 * finished / total)
    progress = Text()
    progress.append("█" * filled, style=primary)
    progress.append("░" * (34 - filled), style=track)
    progress.append(f"  {finished} of {total} · {finished * 100 // total}%", style=muted)

    return Group(*top, head, Text(""), table, Text(""), progress, Text(""), lock)


class LoadingOverlay(Vertical):
    """启动遮罩：覆盖整屏，加载完成前拦截所有交互。"""

    DEFAULT_CSS = """
    LoadingOverlay {
        layer: overlay;
        width: 100%;
        height: 100%;
        background: $background 78%;
        align: center middle;
    }
    LoadingOverlay #loading-panel {
        width: 74;
        max-width: 96%;
        height: auto;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="loading-panel")

    def update_stages(self, stages: list[dict], frame: int = 0) -> None:
        theme = getattr(self.app, "current_theme", None)
        size = self.app.size
        show_banner = size.width >= 76 and size.height >= 28
        self.query_one("#loading-panel", Static).update(
            _render_boot_panel(stages, frame, theme, show_banner)
        )


class AlexApp(TermUIApp):
    """Alex 对话界面。"""

    TITLE = "Alex"
    SUB_TITLE = "AgentScope · termui"

    COMMANDS = [
        ("/model", "Switch model"),
        ("/permission", "Choose permission mode"),
        ("/settings", "Open settings"),
        ("/cd", "Browse / change workspace"),
        ("/init memory", "Clear all memory"),
        ("/reload", "Reload config and tools"),
        ("/image", "Send an image file"),
        ("/compact", "Compress the context now"),
        ("/queue", "Run independent tasks in sequence"),
        ("/goal", "Run until the goal passes verification"),
        ("/clear", "Clear the session"),
        ("/theme", "Choose the theme"),
        ("/help", "Show help"),
        ("/exit", "Quit Alex"),
    ]

    BINDINGS = [
        ("ctrl+t", "cycle_theme", "Theme"),
        ("ctrl+l", "clear_screen", "Clear"),
        ("ctrl+o", "toggle_debug", "Debug log"),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+v", "paste_image", "Paste image"),
        # cmd / Windows Terminal 会把 Ctrl+V 当成终端自己的粘贴，应用收不到，
        # 留终端不占用的键给截图粘贴用
        ("alt+v", "paste_image", "Paste image"),
        ("f6", "paste_image", "Paste image"),
        Binding("up", "palette_up", "Previous command", priority=True, show=False),
        Binding("down", "palette_down", "Next command", priority=True, show=False),
        Binding("enter", "palette_accept", "Select command", priority=True, show=False),
        Binding("tab", "palette_accept", "Select command", priority=True, show=False),
        # 队列确认阶段复用方向键 / 回车，不影响命令面板
        Binding("up", "queue_scroll_up", "Scroll queue", priority=True, show=False),
        Binding("down", "queue_scroll_down", "Scroll queue", priority=True, show=False),
        Binding("enter", "queue_start", "Start queue", priority=True, show=False),
        Binding("enter", "goal_confirm", "Confirm goal", priority=True, show=False),
        Binding("m", "goal_edit", "Edit goal", priority=True, show=False),
        Binding("escape", "escape", "Stop", priority=True, show=False),
    ]

    CSS = """
    /* 纯黑背景，与 cmd 窗口融为一体 */
    Screen { background: $background; color: $foreground; layers: base overlay; }
    #main {
        layer: base;
        height: 1fr;
        padding: 0;
    }
    #topbar {
        height: auto;
        border: round $primary 30%;
        padding: 0 1;
        margin-top: 1;
    }
    #meta {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    .source-link {
        width: auto;
        height: 1;
        margin-right: 3;
        color: $text-muted;
    }
    #settings-link {
        margin-right: 0;
    }
    #settings-spinner {
        width: 2;
        height: 1;
        color: $primary;
        display: none;
    }
    .source-link:hover {
        color: $primary;
        text-style: underline;
    }
    #tasks {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        display: none;
    }
    #tasks.-visible { display: block; }
    #progress-tag {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $primary;
        display: none;
    }
    #progress-tag.-visible { display: block; }
    #progress-tag.-warn { color: $warning; }
    #progress-tag.-ok { color: $success; }
    #progress-tag.-bad { color: $error; }
    #progress-tag.-muted { color: $secondary; }
    #chat {
        height: 1fr;
        border: round $primary 30%;
        margin-top: 1;
    }
    #bottom { height: auto; background: $background; margin-top: 1; }
    #commands {
        height: auto;
        max-height: 8;
        display: none;
        background: $surface;
        border: tall $primary 40%;
        padding: 0 1;
        margin: 0 0 1 0;
        scrollbar-size-vertical: 0;
    }
    #commands.-visible { display: block; }
    #commands > .option-list--option-highlighted {
        background: $primary 30%;
        color: $foreground;
    }
    #bottom-toolbar {
        height: 1;
        align-vertical: middle;
    }
    #bottom-spacer { width: 1fr; height: 1; }
    #prompt { background: $background; }
    .queue-line {
        padding: 0 1;
        margin-top: 1;
    }
    .queue-summary {
        padding: 0 1;
        margin-top: 1;
    }
    #prompt .text-area--placeholder { color: $text-muted; }
    #statusbar {
        height: 1;
        padding: 0 1;
        margin: 1 0 1 0;
    }
    #workspace-line {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    #jump {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $primary;
        text-style: underline;
    }
    #jump:hover {
        color: $accent;
        text-style: bold underline;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.history: list[tuple[str, str]] = []
        self.stream_text = ""
        self.thinking = ""
        self._tool_args: dict[str, str] = {}
        self._tool_names: dict[str, str] = {}
        self._stream_dirty = False
        self._thinking_ticks = 0
        self._stream_md = None
        # 当前正文块从 stream_text 的哪个位置开始（回答选择题后另起一块）
        self._stream_offset = 0
        # 本轮是否收到过正文增量（没有时在结束时用全文兜底）
        self._turn_text_streamed = False
        # 本轮已经挂出的工具级 diff 卡片数量，用来避免结束时再重复一张整轮大卡
        self._turn_diff_cards = 0
        # 思考独立折叠：每段思考一个 Collapsible；工具与子代理按顺序单行追加
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows: dict[str, Static] = {}
        self.agent = None
        self.toolkit = None
        self.mcps = []
        self._mcp_tools_by_server: dict[str, list[str]] = {}
        self.link = None
        self._no_daemon = False
        self._busy = False
        self._boot: dict[str, dict[str, str]] = {
            "daemon": {
                "label": "Background daemon",
                "status": "idle",
                "detail": "",
            }
        }
        self._boot_frame = 0
        self._loading_visible = True
        self._confirm_screen = None
        self._confirm_request_id = ""
        self._confirm_resolved = False
        self._ask_widget = None
        self._ask_request_id = ""
        self._compacting_line = None
        self._palette_visible = False
        self._palette_matches: list[tuple[str, str]] = []
        self._activity = ""
        self._reply_worker = None
        self._backend = None
        # 权限门在后台初始化时创建（其依赖链约 2s，不想挡住首屏）
        self._permission_gate = None
        self._snapshots: SnapshotStore | None = None
        self._checkpoints: list[Checkpoint] = []
        self._snapshot_warned = False
        self._rewinding = False
        self._exchange_seq = 0
        self._stop_armed = False
        self._stop_timer = None
        self._activity_before_stop = ""
        self._stopping = False
        self._queue_confirming = False
        self._queue_armed = False
        self._queue_total = 0
        self._queue_card_widget: QueueCard | None = None
        self._goal_card_widget: GoalCard | None = None
        self._goal_confirming = False
        self._goal_editing = False
        self._goal_phase = ""
        self._verifier_mode = False
        self._meta_base = ""
        self._workspace = str(workspace_path())  # 跟随 daemon 推送实时更新
        self._spinner_index = 0
        self._spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        # 粘贴的截图：占位符 -> 本地文件
        self._pasted_images: dict[str, Path] = {}
        self._paste_seq = 0
        # 设置页数据预取缓存
        self._settings_cache: dict | None = None
        self._model_catalog: dict[str, dict] = {}
        self._settings_spinner_frame = 0
        self._settings_spinner_timer = None
        # 性能优化：缓存与节流
        self._chat_ref: ChatLog | None = None  # 缓存 ChatLog 引用

    # ------------------------------------------------------------------ 布局
    def compose(self) -> ComposeResult:
        with Vertical(id="main"):
            with Horizontal(id="topbar"):
                yield Static("", id="meta")
                yield Static("", id="tasks")
                yield Static("", id="settings-spinner")
                yield Static("Settings", id="settings-link", classes="source-link")
            yield ChatLog(id="chat")
            with Vertical(id="bottom"):
                yield OptionList(id="commands")
                with Horizontal(id="bottom-toolbar"):
                    yield Static("", id="progress-tag")
                    yield Static("", id="bottom-spacer")
                    yield Static("Jump to latest", id="jump")
                yield PromptInput(
                    id="prompt",
                    placeholder=PROMPT_PLACEHOLDER,
                )
            with Horizontal(id="statusbar"):
                yield Static("", id="workspace-line")
        yield LoadingOverlay(id="loading")

    async def on_mount(self) -> None:
        super().on_mount()
        for spec in THEMES.values():
            self.register_theme(spec.to_textual_theme())
        saved = str(_load_ui_state().get("theme") or CONFIG.get("theme") or "")
        self.theme = saved if saved in THEMES else DEFAULT_THEME
        self.set_interval(0.15, self._flush_stream)
        self.set_interval(0.2, self._tick_spinner)
        prompt = self.query_one("#prompt", PromptInput)
        prompt.focus()
        # 初始化（尤其 MCP 子进程）放在后台，界面先渲染出来
        prompt.disabled = True
        self._meta_base = "Starting…"
        self._refresh_meta_line()
        self._refresh_loading()
        self.run_worker(self._initialize(), exclusive=True)

    def _boot_set(self, stage: str, status: str, detail: str = "") -> None:
        entry = self._boot.get(stage)
        if entry is None:
            entry = {"label": stage, "status": "idle", "detail": ""}
            self._boot[stage] = entry
        entry["status"] = status
        entry["detail"] = detail
        self._refresh_loading()

    def _refresh_loading(self) -> None:
        if not self._loading_visible:
            return
        try:
            # 阶段未上报的先用占位项补齐，加载页从第一帧到结束都是同一套布局
            stages = []
            for key in _BOOT_ORDER:
                entry = self._boot.get(key) or {
                    "label": _BOOT_LABELS[key],
                    "status": "idle",
                    "detail": "",
                }
                stages.append({"stage": key, **entry})
            self.query_one("#loading", LoadingOverlay).update_stages(
                stages, self._boot_frame
            )
        except Exception:
            pass

    def _hide_loading(self) -> None:
        if not self._loading_visible:
            return
        self._loading_visible = False
        try:
            self.query_one("#loading", LoadingOverlay).remove()
        except Exception:
            pass

    async def _initialize(self) -> None:
        """连接会话（后台 daemon 或同进程），之后全部由事件驱动渲染。"""
        self._boot_set("daemon", "active", "connecting…")
        try:
            if self._no_daemon:
                self.link = LocalLink.create()
            else:
                self.link = await ensure_daemon(ROOT)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            self._init_failed(str(exc))
            return
        self._boot_set(
            "daemon",
            "done",
            "in-process" if isinstance(self.link, LocalLink) else "connected",
        )
        self.link.on_event(self._on_session_event)
        if isinstance(self.link, DaemonClient):
            self.link.on_close(self._on_link_closed)
        try:
            await self.link.startup()
        except Exception as exc:
            self._init_failed(str(exc))

    def _init_failed(self, message: str) -> None:
        """初始化失败：必须解除遮罩，否则界面永远停在连接中。"""
        self._boot_set("daemon", "failed", message)
        self._hide_loading()
        try:
            self.query_one("#prompt", PromptInput).disabled = False
        except Exception:
            pass
        self.notify(
            f"Init failed: {message}", title="Init failed", severity="error", timeout=10
        )

    def _on_link_closed(self) -> None:
        self.notify(
            "Disconnected from the background session",
            title="Disconnected",
            severity="warning",
            timeout=8,
        )

    async def _rpc(self, method: str, **params):
        """调用会话；连接失效时自动重连并重试一次。"""
        if method != "describe":
            self._settings_cache = None
        if self.link is None:
            await self._reconnect()
        try:
            return await self.link.rpc(method, **params)
        except LinkError:
            await self._reconnect()
            return await self.link.rpc(method, **params)

    async def _reconnect(self) -> None:
        old = self.link
        if isinstance(old, DaemonClient):
            try:
                await old.close()
            except Exception:
                pass
        try:
            self.link = await ensure_daemon(ROOT)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            self.link = None
            raise LinkError(f"reconnect failed: {exc}") from exc
        self.link.on_event(self._on_session_event)
        if isinstance(self.link, DaemonClient):
            self.link.on_close(self._on_link_closed)
        await self.link.startup()
        self.notify("Reconnected to the background session", timeout=2)

    # ---------------------------------------------------------------- 事件驱动
    def _on_session_event(self, kind: str, data: dict) -> None:
        try:
            self.call_later(self._handle_event, kind, data)
        except Exception:
            self._handle_event(kind, data)

    def _handle_event(self, kind: str, data: dict) -> None:
        if kind == "snapshot":
            self._apply_snapshot(data)
        elif kind == "boot":
            self._boot_set(
                str(data.get("stage", "")),
                str(data.get("status", "idle")),
                str(data.get("detail", "")),
            )
        elif kind == "exchange_start":
            self._exchange_seq = int(data.get("exchange_id", self._exchange_seq))
            self._turn_text_streamed = False
            self._turn_diff_cards = 0
            self._verifier_mode = False
            self._goal_phase = ""
            self._set_progress({"text": ""})
            self.history.append((USER, data.get("text", "")))
            self._append_block(USER, data.get("text", ""), self._exchange_seq)
        elif kind == "thinking_start":
            self.thinking = ""
            self._thinking_ticks = 0
            self._thinking_started = time.monotonic()
            self._close_stream_block()
            self._begin_thinking()
            self._set_activity("Thinking…")
        elif kind == "thinking_delta":
            self.thinking += data.get("delta", "")
            self._stream_dirty = True
        elif kind == "thinking_end":
            text = str(data.get("text", "") or self.thinking).strip()
            self._finish_thinking(text)
            if text:
                self.history.append(("think", text))
            self.thinking = ""
            self._stream_dirty = True
        elif kind == "text_delta":
            self.stream_text += data.get("delta", "")
            self._turn_text_streamed = True
            self._begin_stream()
            self._stream_dirty = True
        elif kind == "tool_start":
            tool_id = str(data.get("id", ""))
            self._close_stream_block()
            self._start_tool_row(tool_id, data)
            self._set_activity(f"Calling {data.get('name', '')}")
        elif kind == "tool_label":
            # 参数到位后补全任务名（如 Subagent 的描述），此时工具可能还在运行
            row = self._tool_rows.get(str(data.get("id", "")))
            if row is not None:
                theme = self.current_theme
                line = self._event_line(
                    str(data.get("kind", "tool")),
                    str(data.get("label", "")),
                    theme,
                )
                line.append(" …", style=f"dim {theme.secondary}")
                row.update(line)
        elif kind == "tool_result":
            tool_id = str(data.get("id", ""))
            text = str(data.get("text", ""))
            self._finish_tool_row(tool_id, data)
            # Edit / Write 一完成就把这次的 diff 挂到该工具行下面（默认收起）
            self._append_tool_diff(data)
            if text:
                self.history.append(
                    (str(data.get("kind", "result")), text)
                )
            self._set_activity(text)
        elif kind == "reply_end":
            self._finish_reply(data)
            self.run_worker(self._prefetch_settings(), exclusive=False)
        elif kind == "confirm_request":
            self.run_worker(self._ask_confirm(data), exclusive=True)
        elif kind == "confirm_end":
            # 另一个窗口先回应时，关掉本窗口的重复弹窗
            if (
                str(data.get("request_id", "")) == self._confirm_request_id
                and self._confirm_screen is not None
            ):
                self._confirm_resolved = True
                try:
                    self.pop_screen()
                except Exception:
                    pass
            self._set_activity("")
        elif kind == "ask_request":
            self._show_ask(data)
        elif kind == "ask_end":
            self._resolve_ask(data)
        elif kind == "compaction":
            self._append_compaction(
                str(data.get("summary", "")), str(data.get("detail", ""))
            )
        elif kind == "compacting":
            self._show_compacting(bool(data.get("active")))
        elif kind == "history_reset":
            self.history = [tuple(item) for item in data.get("history", [])]
            self.stream_text = ""
            self.thinking = ""
            self._stream_offset = 0
            self._render_history()
        elif kind == "checkpoints":
            self._checkpoints = [
                Checkpoint(**item) for item in data.get("checkpoints", [])
            ]
        elif kind == "changes":
            try:
                checkpoint = Checkpoint(**data.get("checkpoint", {}))
            except TypeError:
                return
            self._append_changes(checkpoint)
        elif kind == "meta":
            self._meta_base = str(data.get("label", ""))
            ws = data.get("workspace")
            if ws:
                self._workspace = str(ws)
                reload_config()
            self._refresh_meta_line()
        elif kind == "tasks":
            self._refresh_tasks_ui(data)
        elif kind == "progress":
            self._set_progress(data)
        elif kind == "goal_card":
            self._apply_goal_card(data)
        elif kind == "goal_verify_start":
            self._enter_verifier()
        elif kind == "goal_round":
            self._exit_verifier()
        elif kind == "goal_end":
            self._goal_phase = ""
            self._verifier_mode = False
        elif kind == "queue":
            index = int(data.get("index", 0))
            total = int(data.get("total", 0))
            label = f"Queue {index}/{total}" if total else ""
            self._append_block(
                "queue", f"{label} · {data.get('subject', '')}"
            )
            self._refresh_meta_line()
        elif kind == "queue_card":
            state = str(data.get("state") or "")
            tasks = [str(item) for item in data.get("tasks") or []]
            card = self._queue_card_widget
            if card is None and state == "confirming":
                card = QueueCard(
                    tasks, state="confirming", theme=self.current_theme
                )
                self._append_widget(card)
                self._queue_card_widget = card
            elif card is not None:
                card.set_state(state)
            if state == "confirming":
                self._set_queue_confirming(True, len(tasks))
            else:
                self._set_queue_confirming(False)
                if state in {"done", "cancelled"}:
                    self._queue_card_widget = None
            self._refresh_meta_line()
        elif kind == "queue_result":
            card = self._build_queue_result(
                json.dumps(data, ensure_ascii=False)
            )
            if card is not None:
                self._append_widget(card)
            self._refresh_meta_line()
        elif kind == "queue_done":
            self._append_block("queue_summary", str(data.get("text", "")))
            self._refresh_meta_line()
        elif kind == "status":
            self._set_busy(bool(data.get("busy")))
            self._stopping = bool(data.get("stopping", False))
            self._set_activity(str(data.get("activity", "")))
            phase = str(data.get("phase", ""))
            if phase in {"idle", "failed"}:
                if self._queue_confirming:
                    self._set_queue_confirming(False)
                prompt = self.query_one("#prompt", PromptInput)
                prompt.disabled = False
                prompt.placeholder = PROMPT_PLACEHOLDER
                self._goal_editing = False
                # 选择题把焦点交给了卡片，回复结束后交还输入框
                if self._ask_widget is None:
                    prompt.focus()
            elif phase == "confirming_goal":
                self._goal_confirming = True
                prompt = self.query_one("#prompt", PromptInput)
                prompt.disabled = not self._goal_editing
                prompt.placeholder = (
                    "修改目标后按 Enter 重新确认…"
                    if self._goal_editing
                    else "等待目标确认…"
                )
            elif phase == "running":
                self.query_one("#prompt", PromptInput).placeholder = PROMPT_PLACEHOLDER
            # 遮罩只服务于初始化；任务运行中重连时直接进入界面
            if phase != "initializing":
                self._hide_loading()
        elif kind == "notice":
            self.notify(
                str(data.get("message", "")),
                title=str(data.get("title", "")) or None,
                severity=str(data.get("severity", "information")),
                timeout=float(data.get("timeout", 4)),
            )

    def _apply_snapshot(self, data: dict) -> None:
        for item in data.get("boot", []) or []:
            stage = str(item.get("stage", ""))
            entry = self._boot.get(stage)
            if entry is None:
                entry = {
                    "label": str(item.get("label", stage)),
                    "status": "idle",
                    "detail": "",
                }
                self._boot[stage] = entry
            entry["status"] = str(item.get("status", "idle"))
            entry["detail"] = str(item.get("detail", ""))
        phase = str(data.get("phase", ""))
        # 遮罩只服务于初始化；其余状态（运行中重连等）直接进入界面
        if phase == "initializing":
            self._refresh_loading()
        else:
            self._hide_loading()
        self.history = [tuple(item) for item in data.get("history", [])]
        self.stream_text = str(data.get("stream_text", "") or "")
        self.thinking = str(data.get("thinking", "") or "")
        self._meta_base = str(data.get("meta", "") or "")
        ws = data.get("workspace")
        if ws:
            self._workspace = str(ws)
            reload_config()
        self._exchange_seq = int(data.get("exchange_seq", 0) or 0)
        self._checkpoints = [
            Checkpoint(**item) for item in data.get("checkpoints", []) or []
        ]
        queue = data.get("queue") or {}
        self._set_queue_confirming(
            bool(queue.get("confirming")),
            int(queue.get("total", 0) or 0),
        )
        self._verifier_mode = False
        self._goal_phase = str(data.get("goal_phase", "") or "")
        goal_confirmation = data.get("goal_confirmation") or {}
        self._goal_confirming = bool(goal_confirmation.get("active"))
        self._goal_editing = False
        progress = data.get("progress")
        if isinstance(progress, dict):
            self._set_progress(progress)
        self._render_history()
        self._refresh_meta_line()
        self._refresh_tasks_ui(data.get("tasks") or {})
        self._set_busy(bool(data.get("busy")))
        self._set_activity(str(data.get("activity", "") or ""))
        self.query_one("#prompt", PromptInput).disabled = phase not in {
            "idle",
            "failed",
        } and not self._goal_confirming
        if self._goal_confirming:
            self.query_one("#prompt", PromptInput).disabled = True
            self.query_one("#prompt", PromptInput).placeholder = (
                "等待目标确认…"
            )
        if phase == "idle" and self._settings_cache is None:
            self.run_worker(self._prefetch_settings(), exclusive=False)
        if phase != "initializing" and self.stream_text:
            self._begin_stream()
            self._stream_dirty = True
        pending = data.get("pending_confirm")
        if pending:
            self.run_worker(self._ask_confirm(pending), exclusive=True)
        pending_ask = data.get("pending_ask")
        if pending_ask:
            self._show_ask(pending_ask)

    async def _ask_confirm(self, payload: dict) -> None:
        """把会话里的权限请求变成弹窗，结果回传给 daemon。"""
        screen = ConfirmScreen(
            agent_name=str(payload.get("agent", "Alex")),
            tool_name=str(payload.get("tool", "Tool")),
            detail=str(payload.get("detail", "")),
            reason=str(payload.get("reason", "")),
            allow_always=bool(payload.get("allow_always", True)),
            diff_text=str(payload.get("diff", "")),
        )
        request_id = str(payload.get("request_id", ""))
        self._confirm_screen = screen
        self._confirm_request_id = request_id
        self._confirm_resolved = False
        self._set_activity("Waiting for approval…")
        try:
            choice = await self.push_screen_wait(screen)
        except asyncio.CancelledError:
            raise
        except Exception:
            choice = DENY
        finally:
            self._confirm_screen = None
            self._confirm_request_id = ""
            self._set_activity("")
        if self._confirm_resolved or self.link is None:
            # 另一个窗口已经回应了这个请求
            return
        try:
            await self._rpc(
                "confirm", request_id=request_id, choice=choice or DENY
            )
        except (LinkError, Exception):
            pass

    # ------------------------------------------------------------ 选择题提问
    def _show_ask(self, payload: dict) -> None:
        """把会话里的待答问题挂到对话流末尾。"""
        request_id = str(payload.get("request_id", ""))
        if not request_id:
            return
        if self._ask_request_id == request_id and self._ask_widget is not None:
            return
        if self._ask_widget is not None:
            self._ask_widget.mark_answer("已取消", skipped=True)
        widget = AskChoice(payload, self._answer_ask)
        self._ask_widget = widget
        self._ask_request_id = request_id
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        chat.add_widget(widget)
        self._set_activity("Waiting for your answer…")

    def _answer_ask(
        self,
        request_id: str,
        choices: list[str],
        other: str,
        skipped: bool,
    ) -> None:
        images: list[Path] = []
        for token, path in self._pasted_images.items():
            if token in other:
                images.append(path)
                other = other.replace(token, "")
        other = " ".join(other.split())
        self.run_worker(
            self._answer_ask_rpc(request_id, choices, other, skipped, images),
            exclusive=False,
        )

    async def _answer_ask_rpc(
        self,
        request_id: str,
        choices: list[str],
        other: str,
        skipped: bool,
        images: list[Path] | None = None,
    ) -> None:
        try:
            await self._rpc(
                "answer_ask",
                request_id=request_id,
                choices=choices,
                other=other,
                skipped=skipped,
                images=[str(item) for item in (images or [])],
            )
        except Exception as exc:
            self.notify(f"Answer failed: {exc}", severity="warning", timeout=4)
            if (
                self._ask_widget is not None
                and self._ask_request_id == request_id
            ):
                self._ask_widget.reset_submission()

    def _resolve_ask(self, data: dict) -> None:
        """问题已作答（本窗口或另一窗口），收起为一行。"""
        request_id = str(data.get("request_id", ""))
        if (
            self._ask_request_id
            and request_id
            and request_id != self._ask_request_id
        ):
            return
        summary = str(data.get("summary", "") or "已跳过")
        skipped = bool(data.get("skipped"))
        widget = self._ask_widget
        if widget is not None:
            widget.mark_answer(summary, skipped)
        else:
            self._append_block("ask", summary)
        self.history.append(("ask", summary))
        self._ask_widget = None
        self._ask_request_id = ""
        self._seal_stream()
        self._restore_prompt_focus()

    def _seal_stream(self) -> None:
        """作答后封口正文块与思考组，后续内容另起新块排在答案之后。"""
        segment = self.stream_text[self._stream_offset:]
        if self._stream_md is not None and segment:
            self._stream_md.update(segment)
        self._stream_md = None
        self._stream_offset = len(self.stream_text)
        if self._thinking_box is not None:
            self._finish_thinking(self.thinking.strip())
        self.thinking = ""
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0

    def _restore_prompt_focus(self) -> None:
        try:
            self.query_one("#prompt", PromptInput).focus()
        except Exception:
            pass

    def _show_compacting(self, active: bool) -> None:
        """上下文压缩进行中：聊天区末尾挂一行临时提示。"""
        if active:
            if self._compacting_line is not None:
                return
            try:
                chat = self._chat_ref or self.query_one("#chat", ChatLog)
            except Exception:
                return
            self._chat_ref = chat
            self._compacting_line = chat.add_static(
                Text("↺ Compacting context…", style=self.current_theme.secondary),
                classes="compacting",
            )
        else:
            line, self._compacting_line = self._compacting_line, None
            if line is not None:
                line.remove()

    def _finish_reply(self, data: dict) -> None:
        self._verifier_mode = False
        self._goal_phase = ""
        if self._ask_widget is not None:
            self._ask_widget.mark_answer("已取消", skipped=True)
            self._ask_widget = None
            self._ask_request_id = ""
        self._show_compacting(False)
        segment = self.stream_text[self._stream_offset:]
        streamed = self._turn_text_streamed
        self.stream_text = ""
        self._turn_text_streamed = False
        text = str(data.get("text", "") or "")
        if text:
            self.history.append((ASSISTANT, text))
            if segment:
                if self._stream_md is None:
                    self._begin_stream()
                if self._stream_md is not None:
                    self._stream_md.update(segment)
            elif not streamed:
                # 模型没有流式输出，只在结束时给出全文
                self._begin_stream()
                if self._stream_md is not None:
                    self._stream_md.update(text)
        self._stream_md = None
        self._stream_offset = 0
        if self._thinking_box is not None:
            self._finish_thinking(self.thinking.strip())
        self.thinking = ""
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows.clear()
        self._set_activity("")
        self._append_turn_stats(
            float(data.get("duration", 0.0)),
            int(data.get("input_tokens", 0)),
            int(data.get("output_tokens", 0)),
            int(data.get("cache_tokens", 0)),
            bool(data.get("compacted", False)),
        )

    async def on_unmount(self) -> None:
        if self.link is not None:
            await self.link.close()

    def on_resize(self, event) -> None:
        """窄屏时压缩聊天区左右留白。"""
        chat = self.query_one("#chat", ChatLog)
        pad = 1 if event.size.width < 80 else 2
        chat.styles.padding = (1, pad, 0, pad)

    # ------------------------------------------------------------------ 渲染
    def _render_history(self) -> None:
        """重建整个聊天区：用户消息 → 折叠的调试组 → 回答。"""
        chat = self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        self._ask_widget = None
        self._ask_request_id = ""
        self._compacting_line = None
        self._queue_card_widget = None
        self._goal_card_widget = None
        chat._suspend_scroll = True
        chat.clear()
        theme = self.current_theme
        self._stream_md = None
        self._stream_offset = 0
        self._turn_text_streamed = False
        self._turn_diff_cards = 0
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows = {}
        remaining = list(self._checkpoints)
        # 收集所有控件后批量挂载，避免逐个 mount 触发 N 次布局重排
        body = chat.query_one("#chat-body")
        widgets = []
        inline_diffs: list[tuple[int, InlineDiffCard]] = []
        index = 0
        current_exchange: int | None = None
        while index < len(self.history):
            kind, content = self.history[index]
            if kind in DEBUG_KINDS:
                # 思考单独折叠；工具 / 子代理每行一条，保持原始顺序
                if kind == "think":
                    widgets.append(
                        Collapsible(
                            Static(
                                self._thinking_text(content, theme),
                                classes="debug-body",
                            ),
                            title=f"Thinking · {len(content):,} chars",
                            collapsed=True,
                            classes="debug",
                        )
                    )
                elif kind in ("result", "error"):
                    widgets.append(
                        Static(
                            self._tool_line(
                                content, kind == "result", theme
                            ),
                            classes="tool-line",
                        )
                    )
                else:
                    widgets.append(
                        Static(
                            self._event_line(kind, content, theme),
                            classes="tool-line",
                        )
                    )
                index += 1
                continue
            if kind == USER:
                exchange_id = None
                for position, checkpoint in enumerate(remaining):
                    if self._same_prompt(content, checkpoint.prompt):
                        exchange_id = checkpoint.exchange_id
                        remaining.pop(position)
                        break
                widgets.extend(self._make_block_widgets(USER, content, exchange_id))
                current_exchange = exchange_id
            else:
                widgets.extend(self._make_block_widgets(kind, content))
                if kind == ASSISTANT and current_exchange is not None:
                    checkpoint = self._checkpoint_for(current_exchange)
                    if checkpoint is not None:
                        stats_widget = self._make_turn_stats(
                            checkpoint.duration,
                            checkpoint.input_tokens,
                            checkpoint.output_tokens,
                            checkpoint.cache_input_tokens,
                            checkpoint.compacted,
                        )
                        if stats_widget is not None:
                            widgets.append(stats_widget)
                        if checkpoint.files:
                            diff_card = InlineDiffCard(
                                checkpoint, self.current_theme
                            )
                            widgets.append(diff_card)
                            inline_diffs.append(
                                (checkpoint.exchange_id, diff_card)
                            )
            index += 1
        # 一次性挂载所有控件
        if widgets:
            body.mount_all(widgets)
        for exchange_id, diff_card in inline_diffs:
            self.run_worker(
                self._load_inline_diff(exchange_id, diff_card),
                exclusive=False,
            )
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows = {}
        if self.thinking:
            self._begin_thinking()
        if self.stream_text:
            self._begin_stream()
        chat._suspend_scroll = False

    def _jump_to_latest(self) -> None:
        self.query_one("#chat", ChatLog).scroll_end(animate=False)

    def on_click(self, event) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "jump":
            self._jump_to_latest()
        elif widget_id == "settings-link":
            self._open_settings()

    def _append_block(
        self,
        kind: str,
        content: str,
        exchange_id: int | None = None,
    ) -> None:
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        for widget in self._make_block_widgets(kind, content, exchange_id):
            chat.add_widget(widget)

    def _append_widget(self, widget) -> None:
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        chat.add_widget(widget)

    def _make_block_widgets(
        self,
        kind: str,
        content: str,
        exchange_id: int | None = None,
    ) -> list:
        """生成消息控件列表（不挂载），供批量和逐条两种模式复用。"""
        theme = self.current_theme
        widgets = []
        if kind == USER:
            widgets.append(
                Static(
                    Text("You", style=f"bold {theme.accent}"), classes="role-user"
                )
            )
            widgets.append(
                UserMessage(
                    Text(content, style=theme.foreground),
                    classes="user-bubble",
                    exchange_id=exchange_id,
                    message_text=content,
                )
            )
        elif kind == ASSISTANT:
            widgets.append(
                Static(
                    Text("Alex", style=f"bold {theme.primary}"), classes="role"
                )
            )
            widgets.append(Markdown(content, classes="md"))
        elif kind == "queue":
            line = Text(content, style=f"bold {theme.secondary}")
            widgets.append(Static(line, classes="queue-line"))
        elif kind == "queue_summary":
            widgets.append(self._queue_summary_widget(content))
        elif kind == "queue_card":
            card = self._build_queue_card(content)
            if card is not None:
                widgets.append(card)
        elif kind == "queue_result":
            card = self._build_queue_result(content)
            if card is not None:
                widgets.append(card)
        elif kind == "goal_card":
            card = self._build_goal_card(content)
            if card is not None:
                widgets.append(card)
        elif kind == "verifier":
            widgets.append(
                Static(
                    Text("Verifier", style=f"bold {theme.secondary}"),
                    classes="role-verifier",
                )
            )
        elif kind == "ask":
            skipped = content.startswith("已跳过") or content.startswith("已取消")
            line = Text()
            line.append(
                "⤼ " if skipped else "✓ ",
                style=f"bold {theme.secondary if skipped else theme.success}",
            )
            line.append(content, style=theme.secondary if skipped else theme.foreground)
            widgets.append(Static(line, classes="ask-history"))
        return widgets

    def _build_queue_card(self, content: str) -> QueueCard | None:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        tasks = [str(item) for item in payload.get("tasks") or []]
        state = str(payload.get("state") or "confirming")
        card = QueueCard(tasks, state=state, theme=self.current_theme)
        # 进行中的计划卡保留引用，结束后原地改成 DONE / NOT STARTED
        if state in {"confirming", "started"}:
            self._queue_card_widget = card
        return card

    def _build_goal_card(self, content: str) -> GoalCard | None:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        card = GoalCard(payload, self.current_theme)
        # 进行中的卡片保留引用，后续事件原地更新
        if str(payload.get("phase") or "") not in {
            "passed",
            "stopped",
            "impossible",
            "failed",
        }:
            self._goal_card_widget = card
        return card

    def _build_queue_result(self, content: str) -> QueueCard | None:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        results = payload.get("results") or []
        seconds = int(payload.get("seconds", 0) or 0)
        return QueueCard(
            [],
            state="result",
            results=results,
            seconds=seconds,
            theme=self.current_theme,
        )

    def _queue_summary_widget(self, content: str) -> Static:
        theme = self.current_theme
        lines = content.splitlines()
        out = Text()
        if lines:
            out.append(lines[0], style=f"bold {theme.primary}")
        for line in lines[1:]:
            out.append("\n")
            if line.startswith("✓"):
                out.append("✓", style=f"bold {theme.success}")
                out.append(line[1:], style=theme.secondary)
            elif line.startswith("✗"):
                out.append("✗", style=f"bold {theme.error}")
                out.append(line[1:], style=theme.error)
            elif line.startswith("–"):
                out.append(line, style=f"dim {theme.secondary}")
            else:
                out.append(line, style=theme.secondary)
        return Static(out, classes="queue-summary")

    def _set_activity(self, text: str) -> None:
        """记录当前动作；具体进度由工具行和底部状态栏展示。"""
        self._activity = text

    # ---------------------------------------------------------- 思考 / 工具行
    def _chat(self) -> ChatLog:
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        return chat

    @staticmethod
    def _thinking_text(text: str, theme) -> Text:
        return Text(text, style=f"dim {theme.secondary}")

    def _begin_thinking(self) -> None:
        """一段思考开始：单独一个折叠行，默认收起。"""
        self._finish_thinking("")  # 兜底上一段未结束的思考
        body = Static("", classes="debug-body")
        box = Collapsible(
            body, title="Thinking…", collapsed=True, classes="debug"
        )
        self._chat().add_widget(box)
        self._thinking_box = box
        self._thinking_static = body

    def _update_thinking(self) -> None:
        if self._thinking_static is None or self._thinking_box is None:
            return
        theme = self.current_theme
        text = self.thinking
        if len(text) > self._STREAM_TRUNCATE:
            text = "… (earlier content hidden)\n" + text[-self._STREAM_TAIL:]
        self._thinking_static.update(self._thinking_text(text, theme))
        elapsed = time.monotonic() - self._thinking_started
        self._thinking_box.title = f"Thinking · {elapsed:.1f}s"

    def _finish_thinking(self, text: str) -> None:
        """结束当前思考段：有内容定稿为折叠行，无内容直接移除。"""
        box, body = self._thinking_box, self._thinking_static
        self._thinking_box = None
        self._thinking_static = None
        theme = self.current_theme
        if box is None:
            if not text:
                return
            body = Static(self._thinking_text(text, theme), classes="debug-body")
            box = Collapsible(
                body,
                title=f"Thinking · {len(text):,} chars",
                collapsed=True,
                classes="debug",
            )
            self._chat().add_widget(box)
            return
        if not text:
            box.remove()
            return
        elapsed = (
            time.monotonic() - self._thinking_started
            if self._thinking_started
            else 0.0
        )
        if body is not None:
            body.update(self._thinking_text(text, theme))
        box.title = f"Thinking · {elapsed:.1f}s · {len(text):,} chars"
        box.collapsed = True

    @staticmethod
    def _tool_line(text: str, ok: bool, theme) -> Text:
        """完成后的工具行：✓/✗ + 内容，Subagent 名称用主色。"""
        line = Text()
        line.append(
            "✓ " if ok else "✗ ", style=theme.success if ok else theme.error
        )
        if text.startswith("Subagent"):
            head, sep, tail = text.partition(" · ")
            line.append(head, style=theme.primary)
            if sep:
                line.append(sep + tail, style=theme.foreground)
        else:
            line.append(text, style=theme.foreground)
        return line

    def _start_tool_row(self, tool_id: str, data: dict) -> None:
        """工具开始：立刻追加一行，完成时原地更新为结果。"""
        theme = self.current_theme
        kind = str(data.get("kind", "tool"))
        line = self._event_line(kind, str(data.get("label", "")), theme)
        line.append(" …", style=f"dim {theme.secondary}")
        row = Static(line, classes=self._tool_row_classes())
        self._chat().add_widget(row)
        if tool_id:
            self._tool_rows[tool_id] = row

    def _tool_row_classes(self) -> str:
        return "tool-line tool-line-verifier" if self._verifier_mode else "tool-line"

    def _finish_tool_row(self, tool_id: str, data: dict) -> None:
        theme = self.current_theme
        ok = bool(data.get("ok", data.get("kind") == "result"))
        line = self._tool_line(str(data.get("text", "")), ok, theme)
        row = self._tool_rows.pop(tool_id, None) if tool_id else None
        if row is None:
            self._chat().add_widget(
                Static(line, classes=self._tool_row_classes())
            )
        else:
            row.update(line)

    def _close_stream_block(self) -> None:
        """思考或工具开始前封口当前正文块，之后的文字另起一块。

        否则同一轮里工具之后的回答会继续写入工具行上方的旧 Markdown，
        造成正文与工具行顺序颠倒。封口前先补刷尚未渲染的尾部文字，
        避免节流导致最后一小段被丢掉。
        """
        if self._stream_md is not None:
            segment = self.stream_text[self._stream_offset:]
            if len(segment) > self._STREAM_TRUNCATE:
                segment = (
                    "… (earlier content hidden)\n" + segment[-self._STREAM_TAIL:]
                )
            self._stream_md.update(segment)
            self._stream_md = None
            self._stream_offset = len(self.stream_text)

    def _begin_stream(self) -> None:
        """回答容器延迟到首个文本 delta 再挂载，直接接在消息流末尾。"""
        if self._stream_md is not None:
            return
        chat = self._chat()
        theme = self.current_theme
        if not self._verifier_mode:
            role = Static(
                Text("Alex", style=f"bold {theme.primary}"), classes="role"
            )
            chat.add_widget(role)
        md = MarkdownStatic(
            Text(self.stream_text[self._stream_offset:] + " ▍"), classes="md"
        )
        chat.add_widget(md)
        self._stream_md = md

    _STREAM_TRUNCATE = 5000
    _STREAM_TAIL = 3000

    def _flush_stream(self) -> None:
        """节流刷新：流式阶段用纯文本渲染，结束时再一次性转成 Markdown。

        长回复（>5000 字符）只显示尾部，避免对巨量字符串做布局计算。
        """
        if not self._stream_dirty:
            return
        self._stream_dirty = False
        if self._stream_md is not None:
            display = self.stream_text[self._stream_offset:]
            if len(display) > self._STREAM_TRUNCATE:
                display = "… (earlier content hidden)\n" + display[-self._STREAM_TAIL:]
            self._stream_md.update(Text(display + " ▍"))
        if self.thinking:
            self._thinking_ticks += 1
            if self._thinking_ticks % 3 == 0:
                self._update_thinking()

    @staticmethod

    def _event_line(kind: str, content: str, theme) -> Text:
        """事件流的一行：工具 / MCP / 技能 / 规则 / 结果。"""
        styles = {
            "tool": ("▸ ", theme.secondary),
            "mcp": ("▸ ", theme.accent),
            "skill": ("◆ ", theme.warning),
            "rule": ("· ", theme.secondary),
            "result": ("  ✓ ", theme.success),
            "error": ("  ✗ ", theme.error),
        }
        prefix, color = styles.get(kind, ("· ", theme.secondary))
        return Text(f"{prefix}{content}", style=color)

    def watch_theme(self) -> None:
        # Markdown 与事件行都按主题变量着色，切换主题后整体重建
        self._render_history()

    def action_toggle_debug(self) -> None:
        """Ctrl+O 展开/收起最近一段思考。"""
        boxes = list(self.query(Collapsible))
        if boxes:
            boxes[-1].collapsed = not boxes[-1].collapsed

    def action_cycle_theme(self) -> None:
        """Ctrl+T 按注册顺序循环切换五套主题。"""
        self._set_theme(next_theme(self.theme))

    def _set_theme(self, name: str, *, notify: bool = True) -> None:
        if name not in THEMES:
            return
        self.theme = name
        if _load_ui_state().get("theme") != name:
            _save_ui_state(theme=name)
        if notify:
            self.notify(f"Theme: {theme_label(name)}", timeout=2)
        try:
            self.query_one("#workspace-line", Static).update(self._workspace_line())
        except Exception:
            pass

    def _open_theme_picker(self) -> None:
        rows = [
            (theme_label(name), name == self.theme) for name in THEMES
        ]
        self.push_screen(
            PickerScreen(
                "Choose theme",
                rows,
                "↑/↓ move · Enter apply · Esc cancel",
            ),
            self._on_theme_picked,
        )

    def _on_theme_picked(self, label: str | None) -> None:
        if label:
            self._set_theme(theme_name(label))

    def _refresh_tasks_ui(self, data: dict) -> None:
        bar = self.query_one("#tasks", Static)
        if not data.get("visible"):
            bar.set_class(False, "-visible")
            return
        bar.update(str(data.get("summary", "")))
        bar.set_class(True, "-visible")

    # ------------------------------------------------------------ 目标进度
    def _set_progress(self, data: dict) -> None:
        """输入框左上角的一行进度，目标与队列共用。"""
        try:
            tag = self.query_one("#progress-tag", Static)
        except Exception:
            return
        text = str(data.get("text") or "")
        tone = str(data.get("tone") or "primary")
        classes = ("-warn", "-ok", "-bad", "-muted")
        if not text:
            tag.update("")
            tag.set_class(False, "-visible", *classes)
            return
        tag.update(Text(text))
        for name in classes:
            tag.set_class(name == f"-{tone}", name)
        tag.set_class(True, "-visible")

    def _apply_goal_card(self, data: dict) -> None:
        terminal = {"passed", "stopped", "impossible", "failed", "cancelled"}
        self._goal_confirming = str(data.get("phase") or "") == "awaiting_confirmation"
        card = self._goal_card_widget
        if card is None:
            card = GoalCard(data, self.current_theme)
            self._append_widget(card)
            self._goal_card_widget = card
        else:
            card.update_payload(data, self.current_theme)
        if str(data.get("phase") or "") in terminal:
            self._goal_card_widget = None
        if self._goal_confirming:
            try:
                prompt = self.query_one("#prompt", PromptInput)
                prompt.disabled = not self._goal_editing
                prompt.placeholder = (
                    "修改目标后按 Enter 重新确认…"
                    if self._goal_editing
                    else "等待目标确认…"
                )
            except Exception:
                pass

    def _enter_verifier(self) -> None:
        """验收开始：另起 Verifier 角色行，后续内容缩进显示。"""
        self._goal_phase = "verifying"
        if self._verifier_mode:
            return
        self._close_stream_block()
        self._verifier_mode = True
        theme = self.current_theme
        self._append_widget(
            Static(
                Text("Verifier", style=f"bold {theme.secondary}"),
                classes="role-verifier",
            )
        )

    def _exit_verifier(self) -> None:
        """验收结束、进入下一轮执行：恢复 Alex 角色与正文块。"""
        self._goal_phase = "executing"
        if not self._verifier_mode:
            return
        self._close_stream_block()
        self._verifier_mode = False

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not busy:
            self._stop_armed = False
            self._stopping = False
        self._refresh_meta_line()

    def _set_queue_confirming(self, active: bool, total: int = 0) -> None:
        """切换确认阶段：锁定输入框，并恢复提示文案。"""
        self._queue_confirming = active
        if active:
            self._queue_total = total
        else:
            self._queue_armed = False
            if self._queue_card_widget is not None:
                self._queue_card_widget.set_armed(False)
        try:
            prompt = self.query_one("#prompt", PromptInput)
        except Exception:
            return
        prompt.disabled = active
        prompt.placeholder = (
            "Waiting for confirmation…" if active else PROMPT_PLACEHOLDER
        )

    # ------------------------------------------------------------ 上下文统计
    def _workspace_line(self):
        """Idle: workspace path. Busy: only the spinner + Esc hint."""
        if self._queue_confirming:
            theme = self.current_theme
            count = self._queue_total
            suffix = "task" if count == 1 else "tasks"
            if self._queue_armed:
                return Text(
                    "tasks will be cleared · esc again to discard · enter to start",
                    style=f"bold {theme.warning}",
                )
            return Text(
                f"{count} {suffix} ready · enter to start · esc to cancel",
                style=theme.secondary,
            )
        if not self._busy:
            return f"Workspace: {self._workspace} · {theme_label(self.theme)}"
        frame = self._spinner_frames[self._spinner_index % len(self._spinner_frames)]
        if self._stopping:
            label = "stopping…"
        elif self._stop_armed:
            label = "press esc again to confirm"
        else:
            label = "press esc to stop"
        if self._goal_phase:
            label = f"{self._goal_phase} · {label}"
        theme = self.current_theme
        line = Text()
        line.append(frame, style=f"bold {theme.accent}")
        line.append(
            f" {label}",
            style=f"bold {theme.warning}"
            if self._stop_armed or self._stopping
            else theme.secondary,
        )
        return line

    def _refresh_meta_line(self) -> None:
        try:
            self.query_one("#meta", Static).update(self._meta_base)
            self.query_one("#workspace-line", Static).update(self._workspace_line())
        except Exception:
            pass

    def _tick_spinner(self) -> None:
        if self._loading_visible:
            self._boot_frame += 1
            self._refresh_loading()
            return
        if not self._busy:
            return
        self._spinner_index += 1
        self._refresh_meta_line()

    async def _open_model_picker(self) -> None:
        if self.link is None:
            return
        if self._busy:
            self.notify(
                "Busy — wait for the current reply to finish",
                severity="warning",
                timeout=2,
            )
            return
        try:
            catalog = await self._rpc("model_catalog")
        except Exception as exc:
            self.notify(
                f"Model list failed: {exc}",
                title="Model list failed",
                severity="error",
                timeout=6,
            )
            return
        if not catalog:
            self.notify("No model profiles configured", timeout=4)
            return
        self._model_catalog = {str(item["name"]): item for item in catalog}
        rows = [
            (str(item["name"]), bool(item.get("current"))) for item in catalog
        ]
        self.push_screen(
            PickerScreen(
                "Choose model",
                rows,
                "↑/↓ move · Enter select · Esc cancel",
            ),
            self._on_model_picked,
        )

    def _on_model_picked(self, name: str | None) -> None:
        if not name:
            return
        item = self._model_catalog.get(name, {})
        levels = [str(level) for level in item.get("levels") or []]
        if not levels:
            self.run_worker(self._switch_model(name))
            return
        current = item.get("level")
        rows = [(level, level == current) for level in levels]
        self.push_screen(
            PickerScreen(
                "Reasoning effort",
                rows,
                "↑/↓ move · Enter apply · Esc back",
            ),
            lambda level: self._on_effort_picked(name, level),
        )

    def _on_effort_picked(self, name: str, level: str | None) -> None:
        if level is None:
            self.run_worker(self._open_model_picker())
            return
        self.run_worker(self._switch_model(name, level))

    async def _switch_model(
        self, name: str, reasoning_level: str | None = None
    ) -> None:
        if self.link is None:
            return
        try:
            result = await self._rpc(
                "switch_model", name=name, reasoning_level=reasoning_level
            )
        except Exception as exc:
            self.notify(
                f"Switch failed: {exc}", title="Switch failed", severity="error", timeout=6
            )
            return
        effort = result.get("reasoning")
        suffix = f" · {effort}" if effort else ""
        if result.get("changed"):
            self.notify(f"Switched to {name}{suffix}", timeout=4)
        else:
            self.notify(f"Already using {name}{suffix}", timeout=2)

    # ------------------------------------------------------------ 命令补全
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """命令面板打开时才接管方向键、回车和 Tab。"""
        if action == "escape":
            # 弹窗自己处理 Esc（取消/关闭），否则会被全局“停止回复”吞掉
            return not isinstance(self.screen, ModalScreen)
        if action.startswith("palette_"):
            return self._palette_visible
        if action.startswith("queue_"):
            return self._queue_confirming
        if action == "goal_confirm":
            return self._goal_confirming and not self._goal_editing
        if action == "goal_edit":
            return self._goal_confirming and not self._goal_editing
        return True

    def _show_palette(self, prefix: str) -> None:
        matches = [
            (command, description)
            for command, description in self.COMMANDS
            if command.startswith(prefix.lower())
        ]
        if not matches:
            self._hide_palette()
            return
        palette = self.query_one("#commands", OptionList)
        if matches != self._palette_matches:
            self._palette_matches = matches
            palette.clear_options()
            palette.add_options(
                [
                    Option(f"{command:<10} {description}", id=command)
                    for command, description in matches
                ]
            )
        palette.highlighted = 0
        palette.add_class("-visible")
        self._palette_visible = True

    def _hide_palette(self) -> None:
        self._palette_visible = False
        self._palette_matches = []
        self.query_one("#commands", OptionList).remove_class("-visible")

    def _move_palette(self, delta: int) -> None:
        if not self._palette_matches:
            return
        palette = self.query_one("#commands", OptionList)
        current = palette.highlighted or 0
        palette.highlighted = (current + delta) % len(self._palette_matches)

    def _accept_palette(self) -> None:
        if not self._palette_matches:
            return
        palette = self.query_one("#commands", OptionList)
        index = palette.highlighted or 0
        if index >= len(self._palette_matches):
            return
        command = self._palette_matches[index][0]
        self._hide_palette()
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = ""
        self._handle_command(command)

    def action_palette_up(self) -> None:
        self._move_palette(-1)

    def action_palette_down(self) -> None:
        self._move_palette(1)

    def action_palette_accept(self) -> None:
        self._accept_palette()

    def action_queue_scroll_up(self) -> None:
        if self._queue_card_widget is not None:
            self._queue_card_widget.shift(-1)

    def action_queue_scroll_down(self) -> None:
        if self._queue_card_widget is not None:
            self._queue_card_widget.shift(1)

    def action_queue_start(self) -> None:
        self._confirm_queue()

    def action_goal_confirm(self) -> None:
        if self._goal_confirming and not self._goal_editing and self.link is not None:
            self.run_worker(self._goal_rpc("goal_confirm"), exclusive=False)

    def action_goal_edit(self) -> None:
        if not self._goal_confirming or self._goal_editing:
            return
        state = self._goal_card_widget
        if state is None:
            return
        self._goal_editing = True
        prompt = self.query_one("#prompt", PromptInput)
        prompt.disabled = False
        prompt.placeholder = "修改目标后按 Enter 重新确认…"
        prompt.value = str(state._payload.get("goal") or "")
        prompt.focus()
        self._refresh_meta_line()

    async def _goal_rpc(self, method: str, **params) -> None:
        try:
            await self._rpc(method, **params)
        except Exception as exc:
            self.notify(f"Goal action failed: {exc}", severity="warning", timeout=4)

    async def _update_goal_request(self, text: str) -> None:
        try:
            await self._rpc("goal_update", text=text)
        except Exception as exc:
            self.notify(f"Goal update failed: {exc}", severity="error", timeout=5)
            return
        self._goal_editing = False
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = ""
        prompt.disabled = True
        prompt.placeholder = "等待目标确认…"
        prompt.focus()
        self._refresh_meta_line()

    def _focused_tool_card(self) -> ToolDiffCard | InlineDiffCard | None:
        """焦点（或其祖先）落在一张展开的 diff 卡片上时返回它。"""
        widget = self.focused
        while widget is not None:
            if isinstance(widget, (ToolDiffCard, InlineDiffCard)) and widget._open:
                return widget
            widget = widget.parent
        return None

    def action_escape(self) -> None:
        """Esc: close the command palette, or arm/cancel a running reply."""
        if isinstance(self.screen, DirectoryPicker):
            self.screen.dismiss(None)
            return
        card = self._focused_tool_card()
        if card is not None:
            card.action_toggle()
            # 收起后把焦点还给输入框，用户可以直接接着打字
            try:
                self.query_one("#prompt", PromptInput).focus()
            except Exception:
                pass
            return
        if self._ask_widget is not None:
            self._ask_widget.handle_escape()
            return
        if self._goal_confirming:
            if self._goal_editing:
                self._goal_editing = False
                prompt = self.query_one("#prompt", PromptInput)
                prompt.value = ""
                prompt.disabled = True
                prompt.placeholder = "等待目标确认…"
                self._refresh_meta_line()
            elif self.link is not None:
                self.run_worker(self._goal_rpc("goal_cancel"), exclusive=False)
            return
        if self._queue_confirming:
            self._queue_cancel_step()
            return
        if self._palette_visible:
            self._hide_palette()
            return
        if not self._busy:
            return
        if self._stop_armed:
            self._stop_armed = False
            if self._stop_timer is not None:
                self._stop_timer.stop()
                self._stop_timer = None
            self._stop_reply()
            return
        self._stop_armed = True
        self._activity_before_stop = self._activity
        self._set_activity("Press Esc again to stop")
        self._refresh_meta_line()
        self.notify("Press Esc again to stop", severity="warning", timeout=3)
        if self._stop_timer is not None:
            self._stop_timer.stop()
        self._stop_timer = self.set_timer(3.0, self._reset_stop_armed)

    def _reset_stop_armed(self) -> None:
        self._stop_armed = False
        self._stop_timer = None
        if self._busy and self._activity == "Press Esc again to stop":
            self._set_activity(self._activity_before_stop or "Working…")
        self._refresh_meta_line()

    def _stop_reply(self) -> None:
        """请求会话中断当前回复；子进程由会话侧清理。"""
        self._stopping = True
        self._refresh_meta_line()
        self._set_activity("Stopping…")
        if self.link is not None:
            self.run_worker(self._interrupt(), exclusive=True)

    async def _interrupt(self) -> None:
        try:
            await self._rpc("interrupt")
        except Exception as exc:
            self.notify(f"Stop failed: {exc}", severity="warning", timeout=4)

    # ------------------------------------------------------------ 队列确认
    def _confirm_queue(self) -> None:
        if not self._queue_confirming:
            return
        self._queue_armed = False
        if self._queue_card_widget is not None:
            self._queue_card_widget.set_armed(False)
        self._refresh_meta_line()
        if self.link is not None:
            self.run_worker(self._queue_rpc("queue_confirm"), exclusive=False)

    def _queue_cancel_step(self) -> None:
        """First Esc warns, second Esc discards the tasks."""
        if not self._queue_confirming:
            return
        if not self._queue_armed:
            self._queue_armed = True
            if self._queue_card_widget is not None:
                self._queue_card_widget.set_armed(True)
            self._refresh_meta_line()
            return
        self._queue_armed = False
        self._refresh_meta_line()
        if self.link is not None:
            self.run_worker(self._queue_rpc("queue_cancel"), exclusive=False)

    async def _queue_rpc(self, method: str) -> None:
        try:
            await self._rpc(method)
        except Exception as exc:
            self.notify(f"Queue action failed: {exc}", severity="warning", timeout=4)

    def on_queue_button_clicked(self, message: QueueButton.Clicked) -> None:
        message.stop()
        if message.action.startswith("goal_"):
            if message.action == "goal_confirm":
                self.action_goal_confirm()
            elif message.action == "goal_edit":
                self.action_goal_edit()
            elif message.action == "goal_cancel":
                if self.link is not None:
                    self.run_worker(self._goal_rpc("goal_cancel"), exclusive=False)
            return
        if not self._queue_confirming:
            return
        if message.action == "start":
            self._confirm_queue()
        else:
            self._queue_cancel_step()

    def action_palette_cancel(self) -> None:
        self._hide_palette()

    def on_text_area_changed(self, event) -> None:
        value = event.text_area.text
        if self._goal_editing:
            if self._palette_visible:
                self._hide_palette()
            return
        if value.startswith("/") and " " not in value:
            self._show_palette(value)
        elif self._palette_visible:
            self._hide_palette()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "commands":
            return
        command = event.option.id
        if not command:
            return
        prompt = self.query_one("#prompt", PromptInput)
        self._hide_palette()
        prompt.value = ""
        prompt.focus()
        self._handle_command(str(command))

    # ------------------------------------------------------------------ 对话
    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        # AI 工作时允许继续输入，但发送要等这一轮结束
        if (self._busy and not self._goal_editing) or self._rewinding:
            self.notify("Busy — wait for the current operation", severity="warning", timeout=2)
            return
        value = event.value.strip()
        prompt = self.query_one("#prompt", PromptInput)
        if self._goal_editing:
            if not value:
                self.notify("Goal cannot be empty", severity="warning", timeout=3)
                return
            prompt.value = ""
            self.run_worker(self._update_goal_request(value), exclusive=False)
            return
        prompt.value = ""
        if not value or self.link is None:
            return
        if value.startswith("/"):
            self._handle_command(value)
            return
        text, images = split_images(value, workspace_path())
        images = [*images, *self._pasted_images_in(text)]
        if not text and not images:
            return
        self._start_exchange(text, images)

    # ------------------------------------------------------------------ 截图
    async def paste_clipboard_image(self, target=None) -> bool:
        """剪贴板是图片时存成文件并插入 ``[image-N]`` 占位符。"""
        dest = ROOT / "data" / "pasted" / f"paste-{int(time.time() * 1000)}.png"
        try:
            path = await asyncio.to_thread(grab_clipboard_image, dest)
        except Exception:
            return False
        if path is None:
            return False
        self._paste_seq += 1
        token = f"[image-{self._paste_seq}]"
        self._pasted_images[token] = path
        prompt = target if target is not None else self.query_one(
            "#prompt", PromptInput
        )
        insert = getattr(prompt, "insert_text_at_cursor", None)
        if insert is not None:
            insert(token)
        else:
            prompt.insert(token)
        prompt.focus()
        if not CONFIG.get("model", {}).get("vision", False):
            self.notify(
                "Current model has no vision — the image will be passed as a file path",
                severity="warning",
                timeout=4,
            )
        return True

    def action_paste_image(self) -> None:
        """焦点不在输入框时，Ctrl+V 也能粘贴截图。"""
        self.run_worker(self._paste_image_feedback())

    async def _paste_image_feedback(self) -> None:
        if not await self.paste_clipboard_image():
            self.notify(
                "No image found in the clipboard", severity="warning", timeout=2
            )

    def _pasted_images_in(self, text: str) -> list[Path]:
        return [path for token, path in self._pasted_images.items() if token in text]

    def _start_exchange(self, text: str, images: list[Path]) -> None:
        """把消息发给会话；渲染由 exchange_start 事件驱动。"""
        if self._busy or self._rewinding:
            self.notify(
                "Busy — wait for the current operation",
                severity="warning",
                timeout=2,
            )
            return
        if self.link is None:
            return
        self.run_worker(self._submit(text, images), exclusive=True)

    async def _submit(self, text: str, images: list[Path]) -> None:
        try:
            await self._rpc(
                "submit", text=text, images=[str(item) for item in images]
            )
        except LinkError as exc:
            self.notify(f"Send failed: {exc}", severity="error", timeout=5)

    async def _send_queue(self, text: str) -> None:
        if self.link is None:
            return
        try:
            await self._rpc("queue", text=text)
        except Exception as exc:
            self.notify(f"Queue failed: {exc}", severity="error", timeout=5)

    async def _send_goal(self, text: str) -> None:
        if self.link is None:
            return
        try:
            await self._rpc("goal", text=text)
        except Exception as exc:
            self.notify(f"Goal failed: {exc}", severity="error", timeout=5)

    async def _choose_permission(self) -> None:
        current = str(CONFIG.get("permission_mode", "accept"))
        mode = await self.push_screen_wait(PermissionScreen(current))
        if mode:
            self._set_permission_mode(mode)

    def _set_permission_mode(self, mode: str) -> None:
        self.run_worker(self._apply_permission_mode(mode), exclusive=True)

    async def _apply_permission_mode(self, mode: str) -> None:
        if self.link is None:
            return
        try:
            await self._rpc("set_permission_mode", mode=mode)
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=4)
            return
        self.notify(f"Permission mode: {mode}", timeout=4)

    def _open_settings(self) -> None:
        self._start_settings_spinner()
        self.run_worker(self._open_settings_async(), exclusive=True)

    def _start_settings_spinner(self) -> None:
        """Settings 文字左侧显示加载指示，和对话中的 spinner 同款。"""
        spinner = self.query_one("#settings-spinner", Static)
        self._settings_spinner_frame = 0
        spinner.update(self._settings_spinner_text())
        spinner.display = True
        if self._settings_spinner_timer is None:
            self._settings_spinner_timer = self.set_interval(
                0.12, self._tick_settings_spinner
            )

    def _settings_spinner_text(self) -> Text:
        frame = self._spinner_frames[
            self._settings_spinner_frame % len(self._spinner_frames)
        ]
        return Text(frame, style=f"bold {self.current_theme.accent}")

    def _tick_settings_spinner(self) -> None:
        self._settings_spinner_frame += 1
        self.query_one("#settings-spinner", Static).update(
            self._settings_spinner_text()
        )

    def _stop_settings_spinner(self) -> None:
        if self._settings_spinner_timer is not None:
            self._settings_spinner_timer.stop()
            self._settings_spinner_timer = None
        self.query_one("#settings-spinner", Static).display = False

    async def _open_settings_async(self) -> None:
        started = time.monotonic()
        if self.link is None:
            self._stop_settings_spinner()
            return
        # 预取的 describe 直接复用，点击后只差一次弹窗挂载
        data = self._settings_cache
        if data is None:
            try:
                data = await self._rpc("describe")
            except Exception as exc:
                self._stop_settings_spinner()
                self.notify(
                    f"Cannot open settings: {exc}", severity="error", timeout=5
                )
                return
            self._settings_cache = data
        screen = SettingsScreen(
            [tuple(item) for item in data.get("sections", [])],
            skills=[tuple(item) for item in data.get("skills", [])],
            rules=[tuple(item) for item in data.get("rules", [])],
            skill_details=data.get("skill_details") or {},
            rule_details=data.get("rule_details") or {},
            mcp_servers=[tuple(item) for item in data.get("mcp_servers", [])],
            mcp_tools=[tuple(item) for item in data.get("mcp_tools", [])],
            tokens=data.get("tokens") or {},
            memory=data.get("memory") or {},
            on_visibility_change=self._set_agent_visibility,
        )
        # 数据往往瞬间到手，保底展示一小段时间，让加载指示肉眼可见
        remain = 0.35 - (time.monotonic() - started)
        if remain > 0:
            await asyncio.sleep(remain)
        self._stop_settings_spinner()
        changed = await self.push_screen_wait(screen)
        # 关闭后数据可能已变：清缓存并后台刷新，下次打开依旧秒开
        self._settings_cache = None
        self.run_worker(self._prefetch_settings(), exclusive=False)
        if not changed:
            return
        kinds = screen.changed_kinds
        if kinds and kinds <= {"skill", "rule"}:
            # 只改了技能/规则：重建 agent 即可，不必重启 MCP
            try:
                await self._rpc("rebuild_agent")
                self.notify("Applied skill/rule changes", timeout=3)
            except Exception as exc:
                self.notify(
                    f"Apply failed: {exc}", severity="error", timeout=6
                )
        else:
            await self._reload()

    async def _prefetch_settings(self) -> None:
        """空闲时预先拉取设置页数据，点击 Settings 时无需等待。"""
        if self.link is None:
            return
        try:
            self._settings_cache = await self._rpc("describe")
        except Exception:
            self._settings_cache = None

    def _set_agent_visibility(self, kind: str, item: str, visible: bool) -> None:
        if self.link is None:
            return
        self.run_worker(
            self._rpc(
                "set_visibility", kind=kind, item=item, visible=visible
            )
        )

    def _handle_command(self, raw: str) -> None:
        command, _, arg = raw.partition(" ")
        command = command.lower()
        arg = arg.strip()
        if command in {"/exit", "/quit"}:
            self.exit()
        elif command == "/model":
            if arg and self._rewinding:
                self.notify(
                    "Busy — wait for the restore to finish",
                    severity="warning",
                    timeout=2,
                )
            elif arg:
                self.run_worker(self._switch_model(arg))
            else:
                self.run_worker(self._open_model_picker())
        elif command == "/permission":
            from .permissions import MODE_NAMES

            if not arg:
                self.run_worker(self._choose_permission())
            elif arg.lower() not in MODE_NAMES:
                self.notify(
                    f"Unknown mode {arg}; choose with /permission",
                    severity="error",
                    timeout=4,
                )
            else:
                self._set_permission_mode(arg.lower())
        elif command == "/settings":
            self._open_settings()
        elif command == "/cd":
            if self._busy or self._rewinding:
                self.notify(
                    "Busy — change workspace after this reply",
                    severity="warning",
                    timeout=2,
                )
            elif arg:
                self.run_worker(self._change_workspace(arg), exclusive=True)
            else:
                self.push_screen(
                    DirectoryPicker(workspace_path(create=True)),
                    self._on_directory_picked,
                )
        elif command == "/init":
            if arg.lower() != "memory":
                self.notify("Usage: /init memory", severity="warning", timeout=3)
            elif self._busy or self._rewinding:
                self.notify(
                    "Busy — clear memory after this reply",
                    severity="warning",
                    timeout=2,
                )
            else:
                self.run_worker(self._init_memory(), exclusive=True)
        elif command == "/help":
            self.notify(
                "/model choose model & reasoning · /permission choose permission mode"
                " · /settings skills / rules / memory / tokens / MCP"
                " · /cd browse workspace · /init memory clear memory"
                " · paste an image path or /image <path> [prompt]"
                " · F6 / Alt+V paste clipboard screenshot"
                " · click a user message to restore it"
                " · click a change summary to view its diff"
                " · /reload reload config · /clear clear session · /theme choose theme"
                " · /compact compress context"
                " · /queue run independent tasks in order"
                " · /goal work until an independent verifier passes it"
                " · Shift+Enter newline · /exit quit",
                severity="warning",
                timeout=10,
            )
        elif command == "/theme":
            self._open_theme_picker()
        elif command == "/reload":
            if self._busy or self._rewinding:
                self.notify(
                    "Busy — reload after this reply", severity="warning", timeout=2
                )
            else:
                self.run_worker(self._reload(), exclusive=True)
        elif command == "/image":
            if not arg:
                self.notify(
                    "Usage: /image <path> [prompt]",
                    severity="warning",
                    timeout=4,
                )
            else:
                text, images = split_images(arg, workspace_path())
                if not images:
                    self.notify(
                        f"Image not found: {arg.split()[0]}",
                        severity="error",
                        timeout=5,
                    )
                else:
                    self._start_exchange(text, images)
        elif command == "/compact":
            if self._busy or self._rewinding:
                self.notify(
                    "Busy — compact after this reply",
                    severity="warning",
                    timeout=2,
                )
            else:
                self.run_worker(self._compact_context(), exclusive=True)
        elif command == "/queue":
            if self._busy or self._rewinding:
                self.notify(
                    "Busy — queue after this reply",
                    severity="warning",
                    timeout=2,
                )
            elif not arg:
                self.notify(
                    "Usage: /queue task A; task B; task C",
                    severity="warning",
                    timeout=4,
                )
            else:
                self.run_worker(self._send_queue(arg), exclusive=True)
        elif command == "/goal":
            if self._busy or self._rewinding:
                self.notify(
                    "Busy — goal after this reply",
                    severity="warning",
                    timeout=2,
                )
            elif not arg:
                self.notify(
                    "Usage: /goal <what must be achieved>",
                    severity="warning",
                    timeout=4,
                )
            else:
                self.run_worker(self._send_goal(arg), exclusive=True)
        elif command == "/clear":
            if self._busy or self._rewinding:
                self.notify(
                    "Busy — clear after this reply",
                    severity="warning",
                    timeout=2,
                )
            else:
                self.run_worker(self._clear_session(), exclusive=True)
        else:
            self.notify(f"Unknown command: {command}", severity="error", timeout=3)

    def _on_directory_picked(self, path: Path | None) -> None:
        if path is not None:
            self.run_worker(self._change_workspace(str(path)), exclusive=True)

    async def _change_workspace(self, raw: str) -> None:
        if self.link is None:
            return
        try:
            result = await self._rpc("change_workspace", raw=raw)
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=6)
            return
        reload_config()  # daemon 侧已写盘，客户端需同步刷新本地 CONFIG
        self._refresh_meta_line()
        self.notify(f"Workspace: {result.get('workspace', raw)}", timeout=4)

    async def _init_memory(self) -> None:
        if self.link is None:
            return
        try:
            await self._rpc("clear_memory")
        except Exception as exc:
            self.notify(
                f"Clear memory failed: {exc}", severity="error", timeout=6
            )
            return
        self.notify("Memory cleared", timeout=4)

    async def _reload(self) -> None:
        if self.link is None:
            return
        try:
            result = await self._rpc("reload")
        except Exception as exc:
            self.notify(f"Reload failed: {exc}", severity="error", timeout=6)
            return
        self.notify(
            f"Reloaded: {result.get('mcp_servers', 0)} MCP server(s) · "
            f"model {result.get('model', '?')}",
            timeout=3,
        )

    async def _compact_context(self) -> None:
        if self.link is None:
            return
        self._set_busy(True)
        self._set_activity("Compacting context…")
        try:
            await self._rpc("compact")
        except Exception as exc:
            self.notify(str(exc), severity="warning", timeout=5)
        finally:
            self._set_activity("")
            self._set_busy(False)

    async def _clear_session(self) -> None:
        if self.link is None:
            return
        try:
            await self._rpc("clear_session")
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=5)

    def _append_compaction(self, summary: str, detail: str = "") -> None:
        """在聊天区标记本轮发生过上下文压缩，并展示摘要。"""
        try:
            chat = self.query_one("#chat", ChatLog)
        except Exception:
            return
        theme = self.current_theme
        label = "↺ Context compacted"
        if detail:
            label += f" · {detail}"
        chat.add_static(
            Text(label, style=f"bold {theme.secondary}"),
            classes="compaction-title",
        )
        body = summary.strip()
        if body:
            chat.add_widget(Markdown(body, classes="md compaction-body"))

    def _make_turn_stats(
        self,
        duration: float,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int = 0,
        compacted: bool = False,
    ) -> Static | None:
        """构造本轮耗时与 token 用量的展示控件（无用量时返回 None）。"""
        if not (input_tokens or output_tokens):
            return None
        parts = [f"{duration:.1f}s"]
        if input_tokens:
            label = f"↑{_format_tokens(input_tokens)}"
            if cache_tokens:
                label += f" ({_format_tokens(cache_tokens)} cached)"
            parts.append(label)
        if output_tokens:
            parts.append(f"↓{_format_tokens(output_tokens)}")
        if compacted:
            parts.append("context compacted")
        return Static(Text(" · ".join(parts), style="dim"), classes="turn-stats")

    def _append_turn_stats(
        self,
        duration: float,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int = 0,
        compacted: bool = False,
    ) -> None:
        """在回答下方显示本轮耗时与 token 用量。"""
        widget = self._make_turn_stats(
            duration, input_tokens, output_tokens, cache_tokens, compacted
        )
        if widget is None:
            return
        try:
            chat = self.query_one("#chat", ChatLog)
        except Exception:
            return
        chat.add_widget(widget)

    def _append_changes(self, checkpoint: Checkpoint) -> None:
        try:
            chat = self.query_one("#chat", ChatLog)
        except Exception:
            return
        if self._turn_diff_cards:
            # 本轮已经逐条展示过改动，这里只留一行可点击的汇总，避免重复整块 diff
            chat.add_widget(ChangesSummary(checkpoint))
            return
        card = InlineDiffCard(checkpoint, self.current_theme)
        chat.add_widget(card)
        self.run_worker(
            self._load_inline_diff(checkpoint.exchange_id, card),
            exclusive=False,
        )

    def _append_tool_diff(self, data: dict) -> None:
        """把单次 Edit / Write 的 diff 立刻挂到聊天流末尾。"""
        diff_text = str(data.get("diff") or "")
        if not diff_text:
            return
        try:
            files = parse_diff(diff_text)
            if not files:
                return
            lines = diff_lines(diff_text)
            if len(files) == 1 and lines:
                # 路径与 +/− 数已经在卡片标题上，正文不再重复这一行
                lines = lines[1:]
        except Exception:
            return
        additions = sum(item.additions for item in files)
        deletions = sum(item.deletions for item in files)
        path = files[0].path or "(unknown)"
        if len(files) > 1:
            path = f"{path}  +{len(files) - 1} 个文件"
        try:
            chat = self._chat()
        except Exception:
            return
        chat.add_widget(
            ToolDiffCard(
                path,
                (additions, deletions),
                lines,
                time.strftime("%H:%M:%S"),
                self.current_theme,
            )
        )
        self._turn_diff_cards += 1

    async def _load_inline_diff(
        self,
        exchange_id: int,
        card: InlineDiffCard,
    ) -> None:
        try:
            data = await self._rpc("get_diff", exchange_id=exchange_id)
        except Exception as exc:
            card.set_error(str(exc))
            return
        card.set_diff(str(data.get("text", "")))

    def _checkpoint_for(self, exchange_id: int) -> Checkpoint | None:
        for checkpoint in self._checkpoints:
            if checkpoint.exchange_id == exchange_id:
                return checkpoint
        return None

    @staticmethod

    def _same_prompt(showing: str, recorded: str) -> bool:
        """图片消息展示时附带 [image] 行，匹配时允许这层后缀。"""
        if not recorded:
            return showing.startswith("[image]")
        if showing == recorded:
            return True
        return showing.startswith(recorded + "\n") or recorded.startswith(
            showing + "\n"
        )

    def open_change_diff(self, widget) -> None:
        """点击变更摘要：展示这一轮的 diff，并提供回滚。"""
        exchange_id = getattr(widget, "exchange_id", None)
        if exchange_id is None or self.link is None:
            return
        self.run_worker(self._show_changes_diff(int(exchange_id)), exclusive=True)

    async def _show_changes_diff(self, exchange_id: int) -> None:
        try:
            data = await self._rpc("get_diff", exchange_id=exchange_id)
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=5)
            return
        action = await self.push_screen_wait(
            DiffScreen(
                str(data.get("prompt", "")),
                str(data.get("text", "")),
                summary=(
                    f"{data.get('files', 0)} file(s) changed · "
                    f"+{data.get('additions', 0)} -{data.get('deletions', 0)}"
                ),
            )
        )
        if action == REVERT:
            await self._undo(int(data.get("index", 0)))

    def open_message_actions(self, widget) -> None:
        """点击用户消息时打开操作菜单。"""
        if self._busy or self._rewinding:
            self.notify(
                "Busy — wait for the current operation",
                severity="warning",
                timeout=2,
            )
            return
        exchange_id = getattr(widget, "exchange_id", None)
        prompt = getattr(widget, "message_text", "")
        index = None
        if exchange_id is not None:
            for position, checkpoint in enumerate(self._checkpoints):
                if checkpoint.exchange_id == exchange_id:
                    index = position
                    break
        can_revert = index is not None
        self.push_screen(
            MessageActionsScreen(can_revert=can_revert),
            lambda action: self._on_message_action(action, index, prompt),
        )

    def _on_message_action(
        self,
        action: str | None,
        index: int | None,
        prompt: str,
    ) -> None:
        if action == REVERT and index is not None:
            self.run_worker(self._undo(index), exclusive=True)
        elif action == COPY:
            try:
                self.copy_to_clipboard(prompt)
                self.notify("Message copied", timeout=2)
            except Exception as exc:
                self.notify(
                    f"Copy failed: {exc}", severity="error", timeout=3
                )

    async def _undo(self, index: int | None = None) -> None:
        self._rewinding = True
        try:
            await self._undo_inner(index)
        finally:
            self._rewinding = False

    async def _undo_inner(self, index: int | None = None) -> None:
        if self.link is None:
            return
        try:
            result = await self._rpc("undo", index=index)
        except Exception as exc:
            self.notify(str(exc), severity="warning", timeout=5)
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = str(result.get("prompt", ""))
        prompt.focus()
        self.notify(
            f"Restored to this message · {result.get('files', 0)} file(s) reverted",
            timeout=5,
        )

    def _commit_revert(self) -> None:
        if self.link is not None:
            self.run_worker(self._rpc("commit_revert"))


def _acquire_instance_lock():
    """单实例锁：同一个工作区只允许一个 Alex 窗口。"""
    path = ROOT / "data" / "alex.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write("alex\n")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> None:
    import argparse

    lock = _acquire_instance_lock()
    if lock is None:
        print(
            "Alex is already running in another window. "
            "Close it before starting a new one.",
            flush=True,
        )
        raise SystemExit(1)

    parser = argparse.ArgumentParser(prog="alex.tui")
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="run the agent in this process instead of a background daemon",
    )
    args = parser.parse_args()

    # Textual 运行期间会把 sys.stdout/stderr 换成 _PrintCapture（fileno() == -1），
    # 而 MCP 的 stdio 客户端把导入时的 sys.stderr 作为子进程的 stderr。
    # 这里在启动前把它指向真实日志文件，并在此时预热导入，子进程才能正常拉起。
    log_path = ROOT / "data" / "alex.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stderr = log_file
    if args.no_daemon:
        import mcp.client.stdio  # noqa: F401  绑定有效的 stderr
    else:
        # daemon 模式下 MCP 跑在 daemon 进程里，TUI 不必预热导入（省约 1.4s）；
        # 先把 daemon 拉起来，让它的初始化与界面启动并行。
        prewarm_daemon(ROOT)

    app = AlexApp()
    app._no_daemon = args.no_daemon
    # Textual 接管全屏前先给出一行反馈，避免点击后黑屏一段时间像没反应
    print("Starting Alex…", flush=True)
    app.run()


if __name__ == "__main__":
    main()
