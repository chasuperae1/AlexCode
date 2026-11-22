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




class InputSettingsMixin:
    """设置页打开、加载与显示模式切换。"""

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
            display_mode=getattr(self, "_display_mode", "developer"),
            on_visibility_change=self._set_agent_visibility,
            on_display_mode_change=self._set_display_mode,
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
        if kinds == {"preference"}:
            # 只改了显示模式：已即时写入 UI 偏好，无需触碰 daemon
            self.notify("Display mode saved", timeout=3)
        elif kinds and kinds <= {"skill", "rule"}:
            # 只改了技能/规则：重建 agent 即可，不必重启 MCP
            try:
                await self._rpc("rebuild_agent")
                self._refresh_sources()
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

    def _set_display_mode(self, mode: str) -> None:
        """切换交互区显示模式：只存 TUI 偏好，下一轮渲染时生效。"""
        mode = "concise" if str(mode) == "concise" else "developer"
        if mode == getattr(self, "_display_mode", "developer"):
            return
        self._display_mode = mode
        _save_ui_state(display_mode=mode)
