"""AlexApp 的输入与命令：快捷键动作、队列、粘贴、发送、权限、设置、斜杠命令。"""

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


def _facade():
    """运行时取 ``alex.tui``（避免循环 import，同时让测试打桩生效）。"""
    from alex import tui as facade

    return facade

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




class InputCommandMixin:
    """斜杠命令处理。"""

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
            from alex.core.permissions import MODE_NAMES

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
