"""聊天历史重建：把 history/checkpoint 还原成消息、工具行和 diff 卡。"""

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
from alex.ui.panels import ChatLog, ConciseSummary, GoalCard, QueueButton, QueueCard
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




class HistoryMixin:
    """历史回放与代码卡恢复。"""

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
        self._concise_card = None
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows = {}
        remaining = list(self._checkpoints)
        # 重启后从 state 重建的 history 会丢掉 tool 行，history_start 不再等于
        # 新列表下标；按时间顺序把末尾的用户消息与末尾的 checkpoint 对齐。
        user_indices = [
            position
            for position, item in enumerate(self.history)
            if item[0] == USER
        ]
        ordered_checkpoints = sorted(
            self._checkpoints,
            key=lambda item: (item.history_start, item.exchange_id),
        )
        tail = ordered_checkpoints[-len(user_indices):] if user_indices else []
        checkpoint_by_user = dict(zip(user_indices[-len(tail):], tail))
        # 收集所有控件后批量挂载，避免逐个 mount 触发 N 次布局重排
        body = chat.query_one("#chat-body")
        widgets = []
        if not self._concise:
            rules_widget = self._rules_summary_widget()
            if rules_widget is not None:
                widgets.append(rules_widget)
        inline_diffs: list[tuple[int, InlineDiffCard]] = []
        tool_cards: list[tuple[int, ToolDiffCard]] = []
        tool_card_done: set[int] = set()
        index = 0
        current_exchange: int | None = None
        pending: list[tuple[str, str]] = []

        def build_card(records, checkpoint=None):
            """把一轮历史过程还原成汇总卡（默认收起）。"""
            card = ConciseSummary(theme)
            state = getattr(self, "_sources", None)
            if state is not None:
                card.set_rules(len(state.rules), state.rules_size)
            for record_kind, record_content in records:
                if record_kind == "think":
                    card.add_thinking(len(record_content), count_step=True)
                    continue
                ok = record_kind == "result"
                if ok and record_content.startswith("Skill "):
                    skill = (
                        record_content[6:]
                        .split(" done", 1)[0]
                        .split(" · ", 1)[0]
                        .strip()
                    )
                    seconds = 0.0
                    tail = record_content.rsplit(" · ", 1)[-1].strip()
                    if tail.endswith("s"):
                        try:
                            seconds = float(tail[:-1])
                        except ValueError:
                            seconds = 0.0
                    card.step()
                    if state is not None:
                        state.mark_skill(skill, seconds)
                        size = state.skill_size(skill)
                    else:
                        size = 0
                    card.add_skill(skill, size, seconds)
                    continue
                name = (
                    record_content.split(" done")[0][:24]
                    if ok
                    else record_kind.title()
                )
                card.step()
                card.add_tool(name, ok, record_content)
            duration = checkpoint.duration if checkpoint is not None else 0.0
            card.finish(duration)
            if checkpoint is not None and checkpoint.files:
                card.set_changes(
                    len(checkpoint.files),
                    checkpoint.additions,
                    checkpoint.deletions,
                )
            return card

        def flush_pending() -> None:
            """上一轮没有回答就结束时，也要留一张卡。"""
            nonlocal pending
            if not pending:
                return
            card = build_card(pending)
            widgets.append(card)
            pending = []

        while index < len(self.history):
            kind, content = self.history[index]
            if kind in ("rule", "skill"):
                # 来源已由顶部 Rules 行 / 摘要卡统一呈现，旧历史里的公告行不再重复
                index += 1
                continue
            if kind in DEBUG_KINDS:
                if self._concise:
                    pending.append((kind, content))
                    index += 1
                    continue
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
                    if kind == "result" and content.startswith("Skill "):
                        skill = (
                            content[6:]
                            .split(" done", 1)[0]
                            .split(" · ", 1)[0]
                            .strip()
                        )
                        seconds = 0.0
                        tail = content.rsplit(" · ", 1)[-1]
                        if tail.endswith("s"):
                            try:
                                seconds = float(tail[:-1])
                            except ValueError:
                                seconds = 0.0
                        state = getattr(self, "_sources", None)
                        size = state.skill_size(skill) if state is not None else 0
                        widgets.append(
                            Static(
                                self._skill_line(skill, size, seconds, theme),
                                classes="tool-line",
                            )
                        )
                    else:
                        widgets.append(
                            Static(
                                self._tool_line(
                                    content, kind == "result", theme
                                ),
                                classes="tool-line",
                            )
                        )
                    if (
                        kind == "result"
                        and current_exchange is not None
                        and current_exchange not in tool_card_done
                        and content.startswith(("Write done", "Edit done"))
                    ):
                        checkpoint = self._checkpoint_for(current_exchange)
                        card = (
                            self._history_tool_card(checkpoint)
                            if checkpoint is not None
                            else None
                        )
                        if card is not None:
                            widgets.append(card)
                            tool_cards.append((current_exchange, card))
                            tool_card_done.add(current_exchange)
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
                if self._concise and pending:
                    flush_pending()
                exchange_id = None
                mapped = checkpoint_by_user.get(index)
                if mapped is not None and mapped in remaining:
                    exchange_id = mapped.exchange_id
                    remaining.remove(mapped)
                else:
                    for position, checkpoint in enumerate(remaining):
                        if checkpoint.history_start == index or self._same_prompt(
                            content, checkpoint.prompt
                        ):
                            exchange_id = checkpoint.exchange_id
                            remaining.pop(position)
                            break
                widgets.extend(self._make_block_widgets(USER, content, exchange_id))
                current_exchange = exchange_id
            else:
                if self._concise and kind != ASSISTANT and pending:
                    flush_pending()
                checkpoint = None
                if kind == ASSISTANT and current_exchange is not None:
                    checkpoint = self._checkpoint_for(current_exchange)
                if self._concise and kind == ASSISTANT and pending:
                    widgets.append(build_card(pending, checkpoint))
                    pending = []
                if (
                    kind == ASSISTANT
                    and checkpoint is not None
                    and len(checkpoint.files) == 1
                    and checkpoint.exchange_id not in tool_card_done
                ):
                    card = self._history_tool_card(checkpoint)
                    if card is not None:
                        widgets.append(card)
                        tool_cards.append((checkpoint.exchange_id, card))
                        tool_card_done.add(checkpoint.exchange_id)
                widgets.extend(self._make_block_widgets(kind, content))
                if kind == ASSISTANT and checkpoint is not None:
                    stats_widget = self._make_turn_stats(
                        checkpoint.duration,
                        checkpoint.input_tokens,
                        checkpoint.output_tokens,
                        checkpoint.cache_input_tokens,
                        checkpoint.compacted,
                    )
                    if stats_widget is not None:
                        widgets.append(stats_widget)
                    if (
                        checkpoint.files
                        and not self._concise
                        and len(checkpoint.files) > 1
                    ):
                        diff_card = InlineDiffCard(
                            checkpoint, self.current_theme
                        )
                        widgets.append(diff_card)
                        inline_diffs.append(
                            (checkpoint.exchange_id, diff_card)
                        )
            index += 1
        if self._concise:
            flush_pending()
        # 一次性挂载所有控件
        if widgets:
            body.mount_all(widgets)
        for exchange_id, diff_card in inline_diffs:
            self.run_worker(
                self._load_inline_diff(exchange_id, diff_card),
                exclusive=False,
            )
        for exchange_id, tool_card in tool_cards:
            self.run_worker(
                self._load_tool_card(exchange_id, tool_card),
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
