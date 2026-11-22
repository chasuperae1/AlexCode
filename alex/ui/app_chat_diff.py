"""聊天流的 diff 卡片与历史回放。"""

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



class ChatDiffMixin:
    """单轮 diff、tool diff 和历史 checkpoint 回放。"""

    def _append_changes(self, checkpoint: Checkpoint) -> None:
        try:
            chat = self.query_one("#chat", ChatLog)
        except Exception:
            return
        if self._concise:
            # 简洁模式：改动并进汇总卡的 Changes 行，不再单独挂一行
            card = self._concise_card_widget()
            if card is not None:
                card.set_changes(
                    len(checkpoint.files),
                    checkpoint.additions,
                    checkpoint.deletions,
                )
                return
            chat.add_widget(ChangesSummary(checkpoint))
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
        permission = getattr(self, "_permission_card", None)
        name = str(data.get("name") or "").lower()
        if permission is not None and permission.tool_name.lower() == name:
            # 权限卡已经展示过这次改动的预览；完成后原地转成普通 diff 卡，
            # 不再追加第二张，否则同一次写入会出现两层相同 diff。
            permission.finish(data)
            self._permission_card = None
            self._turn_diff_cards += 1
            return
        diff_text = str(data.get("diff") or "")
        if not diff_text:
            return
        try:
            files = parse_diff(diff_text)
            if not files:
                return
            lines = diff_lines(diff_text, dark=self.current_theme.dark)
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
        if self._concise:
            card = self._concise_card_widget()
            if card is not None:
                card.add_diff(path, additions, deletions, len(files))
            self._turn_diff_cards += len(files)
            return
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

    def _history_tool_card(self, checkpoint: Checkpoint) -> ToolDiffCard | None:
        """历史回放的单文件 diff 卡：等 get_diff 回来后回填正文。"""
        if len(checkpoint.files) != 1:
            return None
        raw = checkpoint.files[0]
        try:
            path = Path(raw)
            if not path.is_absolute():
                path = Path(self._workspace or workspace_path()) / path
            shown = str(path)
        except Exception:
            shown = raw
        clock = (
            checkpoint.created_at[-8:]
            if len(checkpoint.created_at) >= 8
            else checkpoint.created_at
        ) or time.strftime("%H:%M")
        placeholder = [Text("Loading diff…", style=self.current_theme.secondary)]
        return ToolDiffCard(
            shown,
            (checkpoint.additions, checkpoint.deletions),
            placeholder,
            clock,
            self.current_theme,
        )

    async def _load_tool_card(
        self,
        exchange_id: int,
        card: ToolDiffCard,
    ) -> None:
        try:
            data = await self._rpc("get_diff", exchange_id=exchange_id)
        except Exception as exc:
            card.set_lines(
                [
                    Text(
                        f"Could not load diff: {exc}",
                        style=self.current_theme.error,
                    )
                ]
            )
            return
        text = str(data.get("text", ""))
        files = parse_diff(text)
        lines = diff_lines(text, dark=self.current_theme.dark) if text else []
        if len(files) == 1 and lines:
            # 路径与 +/− 数已经在卡片标题上，正文不再重复这一行
            lines = lines[1:]
        card.set_lines(lines)

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
