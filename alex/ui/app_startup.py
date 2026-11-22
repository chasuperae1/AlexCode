"""AlexApp 的启动流程：连接 daemon、初始化、加载遮罩、断线重连。"""

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
    """运行时取 ``alex.tui``：

    这些模块由 tui.py 导入，模块级 import 会形成循环；同时测试是在
    facade 上打桩（``tui.ensure_daemon`` 等），只有经由 facade 解析才生效。
    """
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


class StartupMixin:
    """启动流程：连接 daemon、初始化、加载遮罩、断线重连。"""

    def _boot_set(self, stage: str, status: str, detail: str = "") -> None:
        entry = self._boot.get(stage)
        if entry is None:
            entry = {"label": stage, "status": "idle", "detail": ""}
            self._boot[stage] = entry
        entry["status"] = status
        entry["detail"] = detail
        self._refresh_loading()

    def _refresh_loading(self) -> None:
        if not self._loading_visible:
            return
        try:
            # 阶段未上报的先用占位项补齐，加载页从第一帧到结束都是同一套布局
            stages = []
            for key in _BOOT_ORDER:
                entry = self._boot.get(key) or {
                    "label": _BOOT_LABELS[key],
                    "status": "idle",
                    "detail": "",
                }
                stages.append({"stage": key, **entry})
            self.query_one("#loading", LoadingOverlay).update_stages(
                stages, self._boot_frame
            )
        except Exception:
            pass

    def _hide_loading(self) -> None:
        if not self._loading_visible:
            return
        self._loading_visible = False
        try:
            self.query_one("#loading", LoadingOverlay).remove()
        except Exception:
            pass

    async def _initialize(self) -> None:
        """连接会话（后台 daemon 或同进程），之后全部由事件驱动渲染。"""
        self._boot_set("daemon", "active", "connecting…")
        try:
            if self._no_daemon:
                self.link = LocalLink.create()
            else:
                self.link = await _facade().ensure_daemon(ROOT)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            self._init_failed(str(exc))
            return
        self._boot_set(
            "daemon",
            "done",
            "in-process" if isinstance(self.link, LocalLink) else "connected",
        )
        self.link.on_event(self._on_session_event)
        if isinstance(self.link, DaemonClient):
            self.link.on_close(self._on_link_closed)
        try:
            await self.link.startup()
        except Exception as exc:
            self._init_failed(str(exc))

    def _init_failed(self, message: str) -> None:
        """初始化失败：必须解除遮罩，否则界面永远停在连接中。"""
        self._boot_set("daemon", "failed", message)
        self._hide_loading()
        try:
            self.query_one("#prompt", PromptInput).disabled = False
        except Exception:
            pass
        self.notify(
            f"Init failed: {message}", title="Init failed", severity="error", timeout=10
        )

    def _on_link_closed(self) -> None:
        self.notify(
            "Disconnected from the background session",
            title="Disconnected",
            severity="warning",
            timeout=8,
        )

    async def _rpc(self, method: str, **params):
        """调用会话；连接失效时自动重连并重试一次。"""
        if method != "describe":
            self._settings_cache = None
        if self.link is None:
            await self._reconnect()
        try:
            return await self.link.rpc(method, **params)
        except LinkError:
            await self._reconnect()
            return await self.link.rpc(method, **params)

    async def _reconnect(self) -> None:
        old = self.link
        if isinstance(old, DaemonClient):
            try:
                await old.close()
            except Exception:
                pass
        try:
            self.link = await _facade().ensure_daemon(ROOT)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            self.link = None
            raise LinkError(f"reconnect failed: {exc}") from exc
        self.link.on_event(self._on_session_event)
        if isinstance(self.link, DaemonClient):
            self.link.on_close(self._on_link_closed)
        await self.link.startup()
        self.notify("Reconnected to the background session", timeout=2)

    def _on_session_event(self, kind: str, data: dict) -> None:
        try:
            self.call_later(self._handle_event, kind, data)
        except Exception:
            self._handle_event(kind, data)
