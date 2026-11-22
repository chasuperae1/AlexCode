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




class ChromeThemeMixin:
    """主题切换与主题选择器。"""

    def watch_theme(self) -> None:
        # Markdown 与事件行都按主题变量着色，切换主题后整体重建
        if self._permission_pending:
            # 重建会清空聊天区，把尚未回应的权限卡一起抹掉
            return
        self._render_history()

    def action_cycle_theme(self) -> None:
        """Ctrl+T 按注册顺序循环切换五套主题。"""
        self._set_theme(next_theme(self.theme))

    def _set_theme(self, name: str, *, notify: bool = True) -> None:
        if name not in THEMES:
            return
        if self._permission_pending and name != self.theme:
            self.notify(
                "Permission required — resolve it before changing theme",
                severity="warning",
                timeout=3,
            )
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
