"""AlexApp 的界面外围：主题、任务与进度、目标卡、模型选择、命令面板。"""

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
from alex.ui.sources import load_sources
from alex.ui.activity import MIN_VISIBLE_SECONDS, format_duration
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
from alex.ui.panels import ChatLog, GoalCard, QueueButton, QueueCard
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




class ChromePaletteMixin:
    """命令面板动作。"""

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

    def _complete_palette(self) -> None:
        """Tab 只把高亮命令补进输入框，不直接执行。"""
        if not self._palette_matches:
            return
        palette = self.query_one("#commands", OptionList)
        index = palette.highlighted or 0
        if index >= len(self._palette_matches):
            return
        command = self._palette_matches[index][0]
        self._hide_palette()
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = f"{command} "
        prompt.focus()

    def action_palette_up(self) -> None:
        self._move_palette(-1)

    def action_palette_down(self) -> None:
        self._move_palette(1)

    def action_palette_accept(self) -> None:
        self._accept_palette()

    def action_palette_complete(self) -> None:
        self._complete_palette()
