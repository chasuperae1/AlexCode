"""Session 事件分发主体：快照、确认、流式收尾；路由与历史已拆出。"""

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
from textual.widgets import Collapsible, Markdown, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from termui import PromptInput as TermPromptInput, TermUIApp

from alex.screens.ask_choice import AskChoice
from alex.daemon.client import DaemonClient, LinkError, LocalLink, ensure_daemon, prewarm_daemon
from alex.screens.confirm import ALLOW, ALWAYS, DENY
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
    PermissionDiffCard,
    PromptInput,
    ToolDiffCard,
    UserMessage,
    _changes_label,
    _changes_text,
    _format_tokens,
)


from alex.ui.app_event_router import EventRouterMixin
from alex.ui.app_history import HistoryMixin
class EventsMixin(EventRouterMixin, HistoryMixin):
    """事件分发入口：路由、快照、确认与历史回放通过组合提供。"""


    def _apply_snapshot(self, data: dict) -> None:
        for item in data.get("boot", []) or []:
            stage = str(item.get("stage", ""))
            entry = self._boot.get(stage)
            if entry is None:
                entry = {
                    "label": str(item.get("label", stage)),
                    "status": "idle",
                    "detail": "",
                }
                self._boot[stage] = entry
            entry["status"] = str(item.get("status", "idle"))
            entry["detail"] = str(item.get("detail", ""))
        phase = str(data.get("phase", ""))
        # 遮罩只服务于初始化；其余状态（运行中重连等）直接进入界面
        if phase == "initializing":
            self._refresh_loading()
        else:
            self._hide_loading()
        self.history = [tuple(item) for item in data.get("history", [])]
        self.stream_text = str(data.get("stream_text", "") or "")
        self.thinking = str(data.get("thinking", "") or "")
        self._meta_base = str(data.get("meta", "") or "")
        ws = data.get("workspace")
        if ws:
            self._workspace = str(ws)
            reload_config()
        self._exchange_seq = int(data.get("exchange_seq", 0) or 0)
        self._checkpoints = [
            Checkpoint(**item) for item in data.get("checkpoints", []) or []
        ]
        queue = data.get("queue") or {}
        self._set_queue_confirming(
            bool(queue.get("confirming")),
            int(queue.get("total", 0) or 0),
        )
        self._verifier_mode = False
        self._goal_phase = str(data.get("goal_phase", "") or "")
        goal_confirmation = data.get("goal_confirmation") or {}
        self._goal_confirming = bool(goal_confirmation.get("active"))
        self._goal_editing = False
        progress = data.get("progress")
        if isinstance(progress, dict):
            self._set_progress(progress)
        self._render_history()
        self._refresh_meta_line()
        self._refresh_tasks_ui(data.get("tasks") or {})
        self._set_busy(bool(data.get("busy")))
        if not bool(data.get("busy")):
            self._live_runs.clear()
            self._refresh_task_status()
        self._set_activity(str(data.get("activity", "") or ""))
        self.query_one("#prompt", PromptInput).disabled = phase not in {
            "idle",
            "failed",
        } and not self._goal_confirming
        if self._goal_confirming:
            self.query_one("#prompt", PromptInput).disabled = True
            self.query_one("#prompt", PromptInput).placeholder = (
                "等待目标确认…"
            )
        if phase == "idle" and self._settings_cache is None:
            self.run_worker(self._prefetch_settings(), exclusive=False)
        if phase != "initializing" and self.stream_text:
            self._begin_stream()
            self._stream_dirty = True
        pending = data.get("pending_confirm")
        if pending:
            self.run_worker(self._ask_confirm(pending), exclusive=True)
        pending_ask = data.get("pending_ask")
        if pending_ask:
            self._show_ask(pending_ask)

    def _set_permission_lock(self, active: bool) -> None:
        """权限未决时锁住输入；选择后恢复，但仍受 busy / goal / queue 约束。"""
        self._permission_pending = active
        try:
            prompt = self.query_one("#prompt", PromptInput)
        except Exception:
            return
        if active:
            prompt.disabled = True
            prompt.placeholder = "等待权限确认…"
            return
        if self._goal_confirming or self._queue_confirming:
            return
        prompt.disabled = False
        prompt.placeholder = PROMPT_PLACEHOLDER
        prompt.focus()

    async def _ask_confirm(self, payload: dict) -> None:
        """把权限请求变成聊天流里的内联 diff 卡，结果回传给 daemon。"""
        request_id = str(payload.get("request_id", ""))
        if (
            request_id
            and request_id == self._confirm_request_id
            and self._permission_card is not None
        ):
            return
        choice = None
        future = asyncio.get_running_loop().create_future()
        card = PermissionDiffCard(
            payload,
            self.current_theme,
            lambda value: (
                future.set_result(value) if not future.done() else None
            ),
        )
        self._permission_card = card
        self._confirm_screen = None
        self._confirm_request_id = request_id
        self._confirm_future = future
        self._confirm_resolved = False
        self._set_permission_lock(True)
        self._set_activity("Waiting for approval…")
        try:
            chat = self._chat_ref or self.query_one("#chat", ChatLog)
            self._chat_ref = chat
            chat.add_widget(card)
            card.focus()
            choice = await future
        except asyncio.CancelledError:
            raise
        except Exception:
            choice = DENY
            card.set_state("denied")
        finally:
            if self._confirm_request_id == request_id:
                self._confirm_request_id = ""
                self._confirm_future = None
            self._confirm_screen = None
            self._set_activity("")
            self._set_permission_lock(False)
            if choice == DENY:
                self._permission_card = None
        if self._confirm_resolved or self.link is None:
            # 另一个窗口已经回应了这个请求
            return
        try:
            await self._rpc(
                "confirm", request_id=request_id, choice=choice or DENY
            )
        except (LinkError, Exception) as exc:
            card.set_state("failed")
            card._payload["fail_text"] = f"权限回传失败：{exc}"
            self._permission_card = None
            self.notify(
                f"Permission reply failed: {exc}",
                severity="error",
                timeout=5,
            )

    def _show_ask(self, payload: dict) -> None:
        """把会话里的待答问题挂到对话流末尾。"""
        request_id = str(payload.get("request_id", ""))
        if not request_id:
            return
        if self._ask_request_id == request_id and self._ask_widget is not None:
            return
        if self._ask_widget is not None:
            self._ask_widget.mark_answer("已取消", skipped=True)
        widget = AskChoice(payload, self._answer_ask)
        self._ask_widget = widget
        self._ask_request_id = request_id
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        chat.add_widget(widget)
        self._set_activity("Waiting for your answer…")

    def _answer_ask(
        self,
        request_id: str,
        choices: list[str],
        other: str,
        skipped: bool,
    ) -> None:
        images: list[Path] = []
        for token, path in self._pasted_images.items():
            if token in other:
                images.append(path)
                other = other.replace(token, "")
        other = " ".join(other.split())
        self.run_worker(
            self._answer_ask_rpc(request_id, choices, other, skipped, images),
            exclusive=False,
        )

    async def _answer_ask_rpc(
        self,
        request_id: str,
        choices: list[str],
        other: str,
        skipped: bool,
        images: list[Path] | None = None,
    ) -> None:
        try:
            await self._rpc(
                "answer_ask",
                request_id=request_id,
                choices=choices,
                other=other,
                skipped=skipped,
                images=[str(item) for item in (images or [])],
            )
        except Exception as exc:
            self.notify(f"Answer failed: {exc}", severity="warning", timeout=4)
            if (
                self._ask_widget is not None
                and self._ask_request_id == request_id
            ):
                self._ask_widget.reset_submission()

    def _resolve_ask(self, data: dict) -> None:
        """问题已作答（本窗口或另一窗口），收起为一行。"""
        request_id = str(data.get("request_id", ""))
        if (
            self._ask_request_id
            and request_id
            and request_id != self._ask_request_id
        ):
            return
        summary = str(data.get("summary", "") or "已跳过")
        skipped = bool(data.get("skipped"))
        widget = self._ask_widget
        if widget is not None:
            widget.mark_answer(summary, skipped)
        else:
            self._append_block("ask", summary)
        self.history.append(("ask", summary))
        self._ask_widget = None
        self._ask_request_id = ""
        self._seal_stream()
        self._restore_prompt_focus()

    def _seal_stream(self) -> None:
        """作答后封口正文块与思考组，后续内容另起新块排在答案之后。"""
        segment = self.stream_text[self._stream_offset:]
        if self._stream_md is not None and segment:
            self._stream_md.update(segment)
        self._stream_md = None
        self._stream_offset = len(self.stream_text)
        if self._thinking_box is not None:
            self._finish_thinking(self.thinking.strip())
        self.thinking = ""
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0

    def _restore_prompt_focus(self) -> None:
        try:
            self.query_one("#prompt", PromptInput).focus()
        except Exception:
            pass

    def _show_compacting(self, active: bool) -> None:
        """上下文压缩进行中：聊天区末尾挂一行临时提示。"""
        if active:
            if self._compacting_line is not None:
                return
            try:
                chat = self._chat_ref or self.query_one("#chat", ChatLog)
            except Exception:
                return
            self._chat_ref = chat
            self._compacting_line = chat.add_static(
                Text("↺ Compacting context…", style=self.current_theme.secondary),
                classes="compacting",
            )
        else:
            line, self._compacting_line = self._compacting_line, None
            if line is not None:
                line.remove()

    def _finish_reply(self, data: dict) -> None:
        self._verifier_mode = False
        self._goal_phase = ""
        if self._ask_widget is not None:
            self._ask_widget.mark_answer("已取消", skipped=True)
            self._ask_widget = None
            self._ask_request_id = ""
        self._show_compacting(False)
        segment = self.stream_text[self._stream_offset:]
        streamed = self._turn_text_streamed
        self.stream_text = ""
        self._turn_text_streamed = False
        text = str(data.get("text", "") or "")
        if text:
            self.history.append((ASSISTANT, text))
            display = segment if segment else (text if not streamed else "")
            if display:
                if self._stream_md is None:
                    self._begin_stream()
                if self._stream_md is not None:
                    self._render_stream_segment(display)
        self._stream_md = None
        self._stream_offset = 0
        if self._thinking_box is not None:
            self._finish_thinking(self.thinking.strip())
        self.thinking = ""
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows.clear()
        self._live_runs.clear()
        self._refresh_task_status()
        self._set_activity("")
        duration = float(data.get("duration", 0.0))
        if self._concise:
            self._finish_concise_summary(duration)
        self._append_turn_stats(
            duration,
            int(data.get("input_tokens", 0)),
            int(data.get("output_tokens", 0)),
            int(data.get("cache_tokens", 0)),
            bool(data.get("compacted", False)),
        )

    async def on_unmount(self) -> None:
        if self.link is not None:
            await self.link.close()

    def on_resize(self, event) -> None:
        """窄屏时压缩聊天区左右留白。"""
        chat = self.query_one("#chat", ChatLog)
        pad = 1 if event.size.width < 80 else 2
        chat.styles.padding = (1, pad, 0, pad)


    def _jump_to_latest(self) -> None:
        self.query_one("#chat", ChatLog).scroll_end(animate=False)

    def on_click(self, event) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "jump":
            self._jump_to_latest()
        elif widget_id == "settings-link":
            self._open_settings()
