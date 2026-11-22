"""聊天流主体：消息块、附件、消息操作；细节按职责拆到 app_chat_*。"""

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

from alex.ui.app_chat_stream import ChatStreamMixin
from alex.ui.app_chat_diff import ChatDiffMixin

class ChatMixin(ChatStreamMixin, ChatDiffMixin):
    """聊天流入口：流式、diff、附件与消息操作通过组合提供。"""

    def _append_block(
        self,
        kind: str,
        content: str,
        exchange_id: int | None = None,
    ) -> None:
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        for widget in self._make_block_widgets(kind, content, exchange_id):
            chat.add_widget(widget)

    def _append_widget(self, widget) -> None:
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        chat.add_widget(widget)

    def _make_block_widgets(
        self,
        kind: str,
        content: str,
        exchange_id: int | None = None,
    ) -> list:
        """生成消息控件列表（不挂载），供批量和逐条两种模式复用。"""
        theme = self.current_theme
        widgets = []
        if kind == USER:
            widgets.append(
                Static(
                    Text("You", style=f"bold {theme.accent}"), classes="role-user"
                )
            )
            widgets.append(
                UserMessage(
                    Text(content, style=theme.foreground),
                    classes="user-bubble",
                    exchange_id=exchange_id,
                    message_text=content,
                )
            )
        elif kind == ASSISTANT:
            widgets.append(
                Static(
                    Text("Alex", style=f"bold {theme.primary}"), classes="role"
                )
            )
            widgets.extend(self._assistant_content_widgets(content))
        elif kind == "queue":
            line = Text(content, style=f"bold {theme.secondary}")
            widgets.append(Static(line, classes="queue-line"))
        elif kind == "queue_summary":
            widgets.append(self._queue_summary_widget(content))
        elif kind == "queue_card":
            card = self._build_queue_card(content)
            if card is not None:
                widgets.append(card)
        elif kind == "queue_result":
            card = self._build_queue_result(content)
            if card is not None:
                widgets.append(card)
        elif kind == "goal_card":
            card = self._build_goal_card(content)
            if card is not None:
                widgets.append(card)
        elif kind == "verifier":
            widgets.append(
                Static(
                    Text("Verifier", style=f"bold {theme.secondary}"),
                    classes="role-verifier",
                )
            )
        elif kind == "ask":
            skipped = content.startswith("已跳过") or content.startswith("已取消")
            line = Text()
            line.append(
                "⤼ " if skipped else "✓ ",
                style=f"bold {theme.secondary if skipped else theme.success}",
            )
            line.append(content, style=theme.secondary if skipped else theme.foreground)
            widgets.append(Static(line, classes="ask-history"))
        return widgets

    def _assistant_content_widgets(self, content: str) -> list:
        """把回答里的文件链接换成附件卡，其余文字仍走 Markdown。"""
        parts = split_file_links(content)
        if not any(isinstance(part, Attachment) for part in parts):
            return [MarkdownStatic(content, classes="md")] if content else []
        widgets: list = []
        for part in parts:
            if isinstance(part, Attachment):
                widgets.append(FileAttachmentCard(part, self.current_theme))
                continue
            if part.strip():
                widgets.append(MarkdownStatic(part, classes="md"))
        return widgets

    def _render_stream_segment(self, content: str) -> None:
        """流式正文收尾：有文件链接时在正文位置换成卡片并移除流式块。"""
        md = self._stream_md
        if md is None:
            return
        parts = split_file_links(content)
        if not any(isinstance(part, Attachment) for part in parts):
            md.update(content)
            return
        widgets: list = []
        for part in parts:
            if isinstance(part, Attachment):
                widgets.append(FileAttachmentCard(part, self.current_theme))
            elif part.strip():
                widgets.append(MarkdownStatic(part, classes="md"))
        parent = md.parent
        if parent is not None and widgets:
            parent.mount_all(widgets, before=md)
            md.remove()
        else:
            md.update(content)
        self._stream_md = None

    def _build_queue_card(self, content: str) -> QueueCard | None:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        tasks = [str(item) for item in payload.get("tasks") or []]
        state = str(payload.get("state") or "confirming")
        card = QueueCard(tasks, state=state, theme=self.current_theme)
        # 进行中的计划卡保留引用，结束后原地改成 DONE / NOT STARTED
        if state in {"confirming", "started"}:
            self._queue_card_widget = card
        return card

    def _build_goal_card(self, content: str) -> GoalCard | None:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        card = GoalCard(payload, self.current_theme)
        # 进行中的卡片保留引用，后续事件原地更新
        if str(payload.get("phase") or "") not in {
            "passed",
            "stopped",
            "impossible",
            "failed",
        }:
            self._goal_card_widget = card
        return card

    def _build_queue_result(self, content: str) -> QueueCard | None:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        results = payload.get("results") or []
        seconds = int(payload.get("seconds", 0) or 0)
        return QueueCard(
            [],
            state="result",
            results=results,
            seconds=seconds,
            theme=self.current_theme,
        )

    def _queue_summary_widget(self, content: str) -> Static:
        theme = self.current_theme
        lines = content.splitlines()
        out = Text()
        if lines:
            out.append(lines[0], style=f"bold {theme.primary}")
        for line in lines[1:]:
            out.append("\n")
            if line.startswith("✓"):
                out.append("✓", style=f"bold {theme.success}")
                out.append(line[1:], style=theme.secondary)
            elif line.startswith("✗"):
                out.append("✗", style=f"bold {theme.error}")
                out.append(line[1:], style=theme.error)
            elif line.startswith("–"):
                out.append(line, style=theme.secondary)
            else:
                out.append(line, style=theme.secondary)
        return Static(out, classes="queue-summary")

    def _set_activity(self, text: str) -> None:
        """记录当前动作；具体进度由工具行和底部状态栏展示。"""
        self._activity = text

    def _chat(self) -> ChatLog:
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        return chat


    def file_attachment_action(self, action: str, attachment: Attachment) -> None:
        """附件卡三操作：Open / Copy / Copy path。"""
        path = attachment.path
        if action == "open":
            self.run_worker(self._open_attachment(path), exclusive=False)
            return
        if action == "copy-path":
            try:
                self.copy_to_clipboard(str(path))
                self.notify("Path copied", timeout=2)
            except Exception as exc:
                self.notify(f"Copy path failed: {exc}", severity="error", timeout=4)
            return
        if action == "copy":
            self.run_worker(self._copy_attachment(path), exclusive=False)

    async def _open_attachment(self, path: Path) -> None:
        try:
            await asyncio.to_thread(open_file, path)
            self.notify(f"Opened {path.name}", timeout=2)
            # 外部程序刚启动时可能短暂改写终端；强制整屏重绘一次，
            # 避免它留下的半帧残影继续叠在聊天区上。
            await asyncio.sleep(0.15)
            self.refresh(repaint=True, layout=True)
        except Exception as exc:
            self.notify(f"Open failed: {exc}", severity="error", timeout=4)

    async def _copy_attachment(self, path: Path) -> None:
        try:
            await asyncio.to_thread(copy_file_to_clipboard, path)
            self.notify(f"Copied {path.name}", timeout=2)
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error", timeout=4)

    def open_message_actions(self, widget) -> None:
        """点击用户消息时打开操作菜单。"""

        """点击用户消息时打开操作菜单。"""
        if self._busy or self._rewinding:
            self.notify(
                "Busy — wait for the current operation",
                severity="warning",
                timeout=2,
            )
            return
        exchange_id = getattr(widget, "exchange_id", None)
        prompt = getattr(widget, "message_text", "")
        index = None
        if exchange_id is not None:
            for position, checkpoint in enumerate(self._checkpoints):
                if checkpoint.exchange_id == exchange_id:
                    index = position
                    break
        can_revert = index is not None
        self.push_screen(
            MessageActionsScreen(can_revert=can_revert),
            lambda action: self._on_message_action(action, index, prompt),
        )

    def _on_message_action(
        self,
        action: str | None,
        index: int | None,
        prompt: str,
    ) -> None:
        if action == REVERT and index is not None:
            self.run_worker(self._undo(index), exclusive=True)
        elif action == COPY:
            try:
                self.copy_to_clipboard(prompt)
                self.notify("Message copied", timeout=2)
            except Exception as exc:
                self.notify(
                    f"Copy failed: {exc}", severity="error", timeout=3
                )

    async def _undo(self, index: int | None = None) -> None:
        self._rewinding = True
        try:
            await self._undo_inner(index)
        finally:
            self._rewinding = False

    async def _undo_inner(self, index: int | None = None) -> None:
        if self.link is None:
            return
        try:
            result = await self._rpc("undo", index=index)
        except Exception as exc:
            self.notify(str(exc), severity="warning", timeout=5)
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = str(result.get("prompt", ""))
        prompt.focus()
        self.notify(
            f"Restored to this message · {result.get('files', 0)} file(s) reverted",
            timeout=5,
        )

    def _commit_revert(self) -> None:
        if self.link is not None:
            self.run_worker(self._rpc("commit_revert"))
