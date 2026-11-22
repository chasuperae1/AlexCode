"""聊天流里的 diff 卡片：单次工具 diff 与整轮 diff。"""

from __future__ import annotations

import re
import time

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual._wrap import compute_wrap_offsets
from textual.widgets import RichLog, Static
from termui import PromptInput as TermPromptInput

from alex.screens.confirm import ALLOW, ALWAYS, DENY
from alex.screens.diff import diff_lines, diff_palette, parse_diff, render_diff
from alex.core.snapshots import Checkpoint
from alex.ui.constants import PROMPT_PLACEHOLDER, _IMAGE_TOKEN, _TDC_MAX_LINES



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
        border: round $primary 35%;
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
        self._body = Static(
            Text("Loading diff…", style=self._theme.secondary), classes="diff-body"
        )
        self._refresh_header()

    def _hint_text(self) -> Text:
        label = "空格 收起" if self._open else "空格 展开"
        return Text(label, style=self._theme.secondary)

    def compose(self) -> ComposeResult:
        palette = diff_palette(self._theme.dark)
        yield self._head
        with self._wrap:
            legend = Text()
            legend.append("− deleted", style=palette.del_fg)
            legend.append("   ")
            legend.append("+ added", style=palette.add_fg)
            legend.append("   ·   ", style=palette.meta)
            legend.append("line numbers: before / after", style=palette.meta)
            yield Static(legend, classes="diff-legend")
            yield self._body

    def _refresh_header(self) -> None:
        checkpoint = self._checkpoint
        count = len(checkpoint.files)
        title = Text("▤  File changes", style=f"bold {self._theme.primary}")
        title.append(
            f" · {count} file{'s' if count != 1 else ''}",
            style=self._theme.secondary,
        )
        counts = Text()
        counts.append(f"+{checkpoint.additions}", style=self._theme.success)
        counts.append("  ")
        counts.append(f"−{checkpoint.deletions}", style=self._theme.error)
        self._title.update(title)
        self._count.update(counts)

    def set_diff(self, text: str) -> None:
        files = parse_diff(text)
        if not files:
            self._body.update(
                Text("No text changes (binary or empty).", style=self._theme.secondary)
            )
            return

        result = Text()
        palette = diff_palette(self._theme.dark)
        for file_index, item in enumerate(files):
            if file_index:
                result.append("\n")

            file_head = Text()
            file_head.append(f"▤  {item.path or '(unknown)'}", style=f"bold {self._theme.primary}")
            file_head.append(
                f"   +{item.additions}  −{item.deletions}", style=palette.meta
            )
            if file_index == 0:
                result.append_text(file_head)
            else:
                result.append_text(file_head)

            if item.binary:
                result.append("\n")
                result.append(Text("  binary file", style=f"italic {palette.meta}"))
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
                    line = Text(entry.text, style=f"bold {palette.hunk}")
                elif entry.kind == "meta":
                    line = Text(entry.text, style=f"italic {palette.meta}")
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
                        line = Text(style=f"{palette.add_fg} on {palette.add_bg}")
                        line.append(
                            f"{old} {new}  + ",
                            style=f"{palette.gutter} on {palette.add_bg}",
                        )
                        line.append(
                            entry.text,
                            style=f"{palette.add_fg} on {palette.add_bg}",
                        )
                    elif entry.kind == "del":
                        line = Text(style=f"{palette.del_fg} on {palette.del_bg}")
                        line.append(
                            f"{old} {new}  − ",
                            style=f"{palette.gutter} on {palette.del_bg}",
                        )
                        line.append(
                            entry.text,
                            style=f"{palette.del_fg} on {palette.del_bg}",
                        )
                    else:
                        line = Text(style=palette.context)
                        line.append(f"{old} {new}    {entry.text}")
                result.append_text(line)

        self._body.update(result)

    def set_error(self, message: str) -> None:
        self._body.update(Text(f"Could not load diff: {message}", style=self._theme.error))

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
        border: round $primary 35%;
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
        self._max_lines = _TDC_MAX_LINES
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
        return Text(label, style=self._theme.secondary)

    def _foot_label(self) -> Text:
        text = Text()
        text.append(f"修改完成 {self._clock} · ", style=self._theme.secondary)
        text.append(f"+{self._additions}", style=self._theme.success)
        text.append(" 新增  ", style=self._theme.secondary)
        text.append(f"−{self._deletions}", style=self._theme.error)
        text.append(" 删除", style=self._theme.secondary)
        return text

    def _legend(self) -> Text:
        text = Text()
        text.append("−", style=self._theme.error)
        text.append(" 删除   ", style=self._theme.secondary)
        text.append("+", style=self._theme.success)
        text.append(" 新增   ", style=self._theme.secondary)
        text.append("行号：修改前 / 修改后", style=self._theme.secondary)
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
        self._write_lines()

    def set_lines(self, lines: list[Text]) -> None:
        """异步取回 diff 后回填卡片，主题切换重建时也会走这里。"""
        self._lines = list(lines)
        self._path_static.update(self._header_path())
        self._counts_static.update(self._header_counts())
        self._foot_text.update(self._foot_label())
        self._write_lines()
        if self._open:
            self._code.styles.height = min(max(1, len(self._lines)), self._max_lines)

    def _write_lines(self) -> None:
        self._code.clear()
        if not self._lines:
            self._code.write(
                Text("No text changes (binary or empty).", style=self._theme.secondary)
            )
            return
        for line in self._lines:
            self._code.write(line)

    # ------------------------------------------------------------ 交互
    def action_toggle(self) -> None:
        self._open = not self._open
        self.set_class(self._open, "-open")
        self._hint_static.update(self._hint_text())
        if self._open:
            height = min(max(1, len(self._lines)), self._max_lines)
            self._code.styles.height = height
            self._code.scroll_home(animate=False)
            self._code.focus()
            # 展开后把卡片拉回视野：ChatLog 里还套了一层容器，直接 scroll_visible 无效
            try:
                chat = self.app.query_one("#chat")
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


class DiffAction(Static):
    """权限卡片底部的一个可点击动作。"""

    def __init__(self, label: Text, action: str, on_click, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._action = action
        self._on_click = on_click

    def on_click(self, event) -> None:
        self._on_click(self._action)
        event.stop()


class PermissionDiffCard(ToolDiffCard):
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
        self._foot_hint = Static("空格 收起", classes="pdc-hint")
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
            text.append("Permission required", style=f"bold {self._theme.warning}")
            text.append(
                f" · {self._agent_name} · {self.tool_name}",
                style=self._theme.secondary,
            )
            text.append(" · mode default", style=self._theme.secondary)
        elif state == "allowed":
            text.append("已允许 · 正在写入…", style=f"bold {self._theme.success}")
        elif state == "denied":
            text.append("已拒绝 · 文件未修改", style=f"bold {self._theme.error}")
        elif state == "failed":
            text.append(
                str(self._payload.get("fail_text") or "执行失败 · 文件未修改"),
                style=f"bold {self._theme.error}",
            )
        elif state == "done":
            text.append(
                f"修改完成 {self._payload.get('clock', '')} · "
                f"+{self._additions} 新增 · −{self._deletions} 删除",
                style=self._theme.secondary,
            )
        self._foot_label.update(text)

    def set_state(self, state: str) -> None:
        self._state = state
        self._refresh_state()

    def choose(self, action: str) -> None:
        if self._state != "pending":
            return
        if action == "always" and self._always is None:
            return
        choice = {"allow": ALLOW, "always": ALWAYS, "deny": DENY}.get(action, DENY)
        self.set_state("denied" if choice == DENY else "allowed")
        try:
            self._on_choice(choice)
        except Exception:
            self.set_state("failed")
            self._payload["fail_text"] = "权限回传失败 · 请重试"

    def resolve_external(self, choice: str) -> None:
        """另一个窗口先回应时，只更新卡片，不再回传一次。"""
        if self._state != "pending":
            return
        self.set_state("denied" if choice == DENY else "allowed")

    def finish(self, data: dict) -> None:
        """把权限卡原地变成正常的完成/失败 diff 卡，避免再挂第二张。"""
        ok = bool(data.get("ok", True))
        self._payload["clock"] = time.strftime("%H:%M:%S")
        if ok:
            self._state = "done"
        else:
            self._state = "failed"
            self._payload["fail_text"] = str(data.get("text") or "写入失败 · 文件未修改")
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
            self._foot_hint.update("空格 收起" if self._open else "空格 展开")
