"""聊天流的流式/思考/工具行渲染。"""

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
from textual.widgets import Collapsible, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from termui import PromptInput as TermPromptInput, TermUIApp

from alex.screens.ask_choice import AskChoice
from alex.daemon.client import DaemonClient, LinkError, LocalLink, ensure_daemon, prewarm_daemon
from alex.screens.confirm import ALLOW, ALWAYS, DENY, ConfirmScreen
from alex.config import CONFIG, ROOT, reload_config, workspace_path
from alex.screens.diff import DiffScreen, diff_lines, parse_diff, render_diff
from alex.screens.dir_picker import DirectoryPicker
from alex.core.images import grab_clipboard_image, split_images
from alex.screens.message_actions import COPY, REVERT, MessageActionsScreen
from alex.screens.permission import PermissionScreen
from alex.screens.picker import PickerScreen
from alex.settings import SettingsScreen
from alex.core.snapshots import Checkpoint
from alex.ui.themes import DEFAULT_THEME, THEMES, next_theme, theme_label, theme_name
from alex.ui.boot import LoadingOverlay, _gradient_banner, _hex_rgb, _render_boot_panel
from alex.ui.constants import (
    ASSISTANT,
    DEBUG_KINDS,
    LOCAL_TOOLS,
    PROMPT_PLACEHOLDER,
    QUEUE_CARD_LIMIT,
    UI_STATE_PATH,
    USER,
    _BOOT_BANNER,
    _BOOT_LABELS,
    _BOOT_ORDER,
    _BOOT_SPINNER,
    _BOX_END,
    _BOX_MID,
    _BOX_START,
    _BOX_V,
    _IMAGE_TOKEN,
    _TDC_MAX_LINES,
)
from alex.ui.markdown import MarkdownStatic, markdown_to_text, normalize_tables
from alex.ui.files import (
    Attachment,
    FileAttachmentCard,
    copy_file_to_clipboard,
    open_file,
    split_file_links,
)
from alex.ui.panels import ChatLog, ConciseSummary, GoalCard, QueueButton, QueueCard
from alex.ui.sources import format_bytes
from alex.ui.state import _load_ui_state, _save_ui_state
from alex.ui.widgets import (
    ChangesSummary,
    InlineDiffCard,
    PromptInput,
    ToolDiffCard,
    UserMessage,
    _changes_label,
    _changes_text,
    _format_tokens,
)



class ChatStreamMixin:
    """正文流、思考折叠、工具行与简洁模式收口。"""

    @staticmethod
    def _thinking_text(text: str, theme) -> Text:
        return Text(text, style=theme.secondary)

    # ---------------------------------------------------------- 简洁模式收口
    @property
    def _concise(self) -> bool:
        return getattr(self, "_display_mode", "developer") == "concise"

    def _begin_concise_summary(self) -> None:
        """每轮开头挂一张汇总卡，运行中只有这一行。"""
        card = ConciseSummary(self.current_theme)
        state = getattr(self, "_sources", None)
        if state is not None:
            card.set_rules(len(state.rules), state.rules_size)
        self._chat().add_widget(card)
        self._concise_card = card

    def _concise_card_widget(self) -> ConciseSummary | None:
        card = getattr(self, "_concise_card", None)
        return card if isinstance(card, ConciseSummary) else None

    def _finish_concise_summary(self, duration: float) -> None:
        card = self._concise_card_widget()
        if card is not None:
            card.finish(duration)

    def _begin_thinking(self) -> None:
        """一段思考开始：单独一个折叠行，默认收起。"""
        self._finish_thinking("")  # 兜底上一段未结束的思考
        if self._concise:
            self._thinking_box = None
            self._thinking_static = None
            card = self._concise_card_widget()
            if card is not None:
                card.set_activity("thinking")
            return
        body = Static("", classes="debug-body")
        box = Collapsible(
            body, title="Thinking…", collapsed=True, classes="debug"
        )
        self._chat().add_widget(box)
        self._thinking_box = box
        self._thinking_static = body

    def _update_thinking(self) -> None:
        if self._concise:
            return
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
        if self._concise:
            if text:
                card = self._concise_card_widget()
                if card is not None:
                    card.add_thinking(len(text), count_step=True)
            self._thinking_box = None
            self._thinking_static = None
            return
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

    def _rules_summary_widget(self) -> Collapsible | None:
        """开发者模式顶部的 rules 折叠行，只显示数量与文件大小。"""
        state = getattr(self, "_sources", None)
        if state is None or not state.rules:
            return None
        theme = self.current_theme
        body = Text(
            " · ".join(item.name for item in state.rules),
            style=theme.secondary,
        )
        title = (
            f"Rules · {len(state.rules)} loaded · "
            f"{format_bytes(state.rules_size)}"
        )
        return Collapsible(
            Static(body, classes="debug-body"),
            title=title,
            collapsed=True,
            classes="debug rule-summary",
        )

    @staticmethod
    def _skill_from_result(data: dict) -> str:
        label = str(data.get("label") or "")
        if label.startswith("Skill "):
            skill = label[6:].split(" · ", 1)[0].strip()
            return "" if skill in {"", "done"} else skill
        text = str(data.get("text") or "")
        if text.startswith("Skill "):
            skill = text[6:].split(" done", 1)[0].split(" · ", 1)[0].strip()
            return "" if skill in {"", "done"} else skill
        return ""

    def _skill_line(
        self, name: str, size: int, elapsed: float, theme
    ) -> Text:
        line = Text()
        line.append("◆ ", style=theme.warning)
        line.append(
            f"Skill {name} · read {format_bytes(size)} · {elapsed:.1f}s",
            style=theme.warning,
        )
        return line

    def _start_tool_row(self, tool_id: str, data: dict) -> None:
        """工具开始：立刻追加一行，完成时原地更新为结果。"""
        if self._concise:
            card = self._concise_card_widget()
            if card is not None:
                name = str(data.get("name") or data.get("label") or "tool")
                card.step(f"calling {name}")
            return
        theme = self.current_theme
        kind = str(data.get("kind", "tool"))
        line = self._event_line(kind, str(data.get("label", "")), theme)
        line.append(" …", style=theme.secondary)
        row = Static(line, classes=self._tool_row_classes())
        self._chat().add_widget(row)
        if tool_id:
            self._tool_rows[tool_id] = row

    def _tool_row_classes(self) -> str:
        return "tool-line tool-line-verifier" if self._verifier_mode else "tool-line"

    def _finish_tool_row(self, tool_id: str, data: dict) -> None:
        theme = self.current_theme
        ok = bool(data.get("ok", data.get("kind") == "result"))
        name = str(data.get("name") or "tool").strip() or "tool"
        skill = str(data.get("skill") or "").strip()
        if ok and not skill:
            skill = self._skill_from_result(data)
        elapsed = float(data.get("elapsed", 0.0) or 0.0)
        size = 0
        if skill:
            state = getattr(self, "_sources", None)
            if state is not None:
                item = state.mark_skill(skill, elapsed)
                size = item.size if item is not None else 0
            self._refresh_meta_line()
        if self._concise:
            card = self._concise_card_widget()
            if card is not None:
                if skill:
                    card.add_skill(skill, size, elapsed)
                else:
                    card.add_tool(name, ok, str(data.get("text", "")))
            if tool_id:
                self._tool_rows.pop(tool_id, None)
            return
        if skill:
            line = self._skill_line(skill, size, elapsed, theme)
        else:
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
            self._render_stream_segment(segment)
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
            chat.add_widget(MarkdownStatic(body, classes="md compaction-body"))

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
        return Static(
            Text(" · ".join(parts), style=self.current_theme.secondary),
            classes="turn-stats",
        )

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
