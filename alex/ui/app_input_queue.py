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




class InputQueueMixin:
    """队列、目标确认、Esc 停止与队列 RPC。"""

    def action_queue_scroll_up(self) -> None:
        if self._queue_card_widget is not None:
            self._queue_card_widget.shift(-1)

    def action_queue_scroll_down(self) -> None:
        if self._queue_card_widget is not None:
            self._queue_card_widget.shift(1)

    def action_queue_start(self) -> None:
        if self._permission_pending:
            self.notify(
                "Permission required — choose Allow or Deny first",
                severity="warning",
                timeout=3,
            )
            return
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
        if self._permission_pending and self._permission_card is not None:
            # 权限卡继承 ToolDiffCard，Esc 必须先 Deny，而不是折叠卡片
            self._permission_card.action_deny()
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
        if self._goal_phase in {"understanding", "clarifying"}:
            if self.link is not None:
                self.run_worker(self._goal_rpc("goal_cancel"), exclusive=False)
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
