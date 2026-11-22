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


from alex.ui.app_input_queue import InputQueueMixin
from alex.ui.app_input_settings import InputSettingsMixin
from alex.ui.app_input_commands import InputCommandMixin
class InputMixin(InputQueueMixin, InputSettingsMixin, InputCommandMixin):
    """输入与命令：快捷键动作、队列、粘贴、发送、权限、设置、斜杠命令。"""


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

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        # AI 工作时允许继续输入，但发送要等这一轮结束
        if (self._busy and not self._goal_editing) or self._rewinding:
            self.notify("Busy — wait for the current operation", severity="warning", timeout=2)
            return
        if self._permission_pending:
            self.notify(
                "Permission required — choose Allow or Deny first",
                severity="warning",
                timeout=3,
            )
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

    async def paste_clipboard_image(self, target=None) -> bool:
        """剪贴板是图片时存成文件并插入 ``[image-N]`` 占位符。"""
        dest = ROOT / "data" / "pasted" / f"paste-{int(time.time() * 1000)}.png"
        try:
            path = await asyncio.to_thread(_facade().grab_clipboard_image, dest)
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
        if self._permission_pending:
            self.notify(
                "Permission required — choose Allow or Deny first",
                severity="warning",
                timeout=3,
            )
            return
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
