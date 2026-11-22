"""Session 事件路由：把 daemon 事件分发到界面状态与弹窗。"""

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
    PromptInput,
    ToolDiffCard,
    UserMessage,
    _changes_label,
    _changes_text,
    _format_tokens,
)




class EventRouterMixin:
    """事件分发入口。"""

    def _handle_event(self, kind: str, data: dict) -> None:
        if kind == "snapshot":
            self._apply_snapshot(data)
        elif kind == "boot":
            self._boot_set(
                str(data.get("stage", "")),
                str(data.get("status", "idle")),
                str(data.get("detail", "")),
            )
        elif kind == "exchange_start":
            self._exchange_seq = int(data.get("exchange_id", self._exchange_seq))
            self._turn_text_streamed = False
            self._turn_diff_cards = 0
            self._concise_card = None
            self._verifier_mode = False
            self._goal_phase = ""
            self._set_progress({"text": ""})
            self.history.append((USER, data.get("text", "")))
            self._append_block(USER, data.get("text", ""), self._exchange_seq)
            if self._concise:
                self._begin_concise_summary()
        elif kind == "thinking_start":
            self.thinking = ""
            self._thinking_ticks = 0
            self._thinking_started = time.monotonic()
            self._close_stream_block()
            self._begin_thinking()
            self._set_activity("Thinking…")
        elif kind == "thinking_delta":
            self.thinking += data.get("delta", "")
            self._stream_dirty = True
        elif kind == "thinking_end":
            text = str(data.get("text", "") or self.thinking).strip()
            self._finish_thinking(text)
            if text:
                self.history.append(("think", text))
            self.thinking = ""
            self._stream_dirty = True
        elif kind == "text_delta":
            self.stream_text += data.get("delta", "")
            self._turn_text_streamed = True
            self._begin_stream()
            self._stream_dirty = True
        elif kind == "tool_start":
            tool_id = str(data.get("id", ""))
            self._close_stream_block()
            self._start_tool_row(tool_id, data)
            self._live_runs.start(tool_id, str(data.get("name") or ""))
            self._refresh_task_status()
            self._set_activity(f"Calling {data.get('name', '')}")
        elif kind == "tool_label":
            # 参数到位后补全任务名（如 Subagent 的描述），此时工具可能还在运行
            row = self._tool_rows.get(str(data.get("id", "")))
            if row is not None:
                theme = self.current_theme
                line = self._event_line(
                    str(data.get("kind", "tool")),
                    str(data.get("label", "")),
                    theme,
                )
                line.append(" …", style=theme.secondary)
                row.update(line)
        elif kind == "tool_result":
            tool_id = str(data.get("id", ""))
            text = str(data.get("text", ""))
            self._finish_tool_row(tool_id, data)
            self._live_runs.finish(tool_id)
            self._refresh_task_status()
            # Edit / Write 一完成就把这次的 diff 挂到该工具行下面（默认收起）
            self._append_tool_diff(data)
            if text:
                self.history.append(
                    (str(data.get("kind", "result")), text)
                )
            self._set_activity(text)
        elif kind == "reply_end":
            self._finish_reply(data)
            self.run_worker(self._prefetch_settings(), exclusive=False)
        elif kind == "confirm_request":
            self.run_worker(self._ask_confirm(data), exclusive=True)
        elif kind == "confirm_end":
            # 另一个窗口先回应时，更新本窗口的内联卡，不再弹第二个确认层
            request_id = str(data.get("request_id", ""))
            choice = str(data.get("choice", ""))
            if (
                request_id == self._confirm_request_id
                and self._permission_card is not None
            ):
                self._confirm_resolved = True
                self._permission_card.resolve_external(choice)
                future = self._confirm_future
                if future is not None and not future.done():
                    result = (
                        DENY
                        if choice == DENY
                        else ALWAYS
                        if choice == ALWAYS
                        else ALLOW
                    )
                    future.set_result(result)
            self._set_activity("")
        elif kind == "ask_request":
            self._show_ask(data)
        elif kind == "ask_end":
            self._resolve_ask(data)
        elif kind == "compaction":
            self._append_compaction(
                str(data.get("summary", "")), str(data.get("detail", ""))
            )
        elif kind == "compacting":
            self._show_compacting(bool(data.get("active")))
        elif kind == "history_reset":
            self.history = [tuple(item) for item in data.get("history", [])]
            self.stream_text = ""
            self.thinking = ""
            self._stream_offset = 0
            self._live_runs.clear()
            self._refresh_task_status()
            self._render_history()
        elif kind == "checkpoints":
            self._checkpoints = [
                Checkpoint(**item) for item in data.get("checkpoints", [])
            ]
        elif kind == "changes":
            try:
                checkpoint = Checkpoint(**data.get("checkpoint", {}))
            except TypeError:
                return
            self._append_changes(checkpoint)
        elif kind == "meta":
            self._meta_base = str(data.get("label", ""))
            ws = data.get("workspace")
            if ws:
                self._workspace = str(ws)
                reload_config()
            self._refresh_meta_line()
        elif kind == "tasks":
            self._refresh_tasks_ui(data)
        elif kind == "progress":
            self._set_progress(data)
        elif kind == "goal_card":
            self._apply_goal_card(data)
        elif kind == "goal_verify_start":
            self._enter_verifier()
        elif kind == "goal_round":
            self._exit_verifier()
        elif kind == "goal_end":
            self._goal_phase = ""
            self._verifier_mode = False
        elif kind == "queue":
            index = int(data.get("index", 0))
            total = int(data.get("total", 0))
            label = f"Queue {index}/{total}" if total else ""
            self._append_block(
                "queue", f"{label} · {data.get('subject', '')}"
            )
            self._refresh_meta_line()
        elif kind == "queue_card":
            state = str(data.get("state") or "")
            tasks = [str(item) for item in data.get("tasks") or []]
            card = self._queue_card_widget
            if card is None and state == "confirming":
                card = QueueCard(
                    tasks, state="confirming", theme=self.current_theme
                )
                self._append_widget(card)
                self._queue_card_widget = card
            elif card is not None:
                card.set_state(state)
            if state == "confirming":
                self._set_queue_confirming(True, len(tasks))
            else:
                self._set_queue_confirming(False)
                if state in {"done", "cancelled"}:
                    self._queue_card_widget = None
            self._refresh_meta_line()
        elif kind == "queue_result":
            card = self._build_queue_result(
                json.dumps(data, ensure_ascii=False)
            )
            if card is not None:
                self._append_widget(card)
            self._refresh_meta_line()
        elif kind == "queue_done":
            self._append_block("queue_summary", str(data.get("text", "")))
            self._refresh_meta_line()
        elif kind == "status":
            self._set_busy(bool(data.get("busy")))
            self._stopping = bool(data.get("stopping", False))
            self._set_activity(str(data.get("activity", "")))
            phase = str(data.get("phase", ""))
            if phase in {"idle", "failed"}:
                if self._concise and self._concise_card_widget() is not None:
                    card = self._concise_card_widget()
                    if phase == "failed" and card.state == "running":
                        card.fail(str(data.get("activity") or ""))
                if self._queue_confirming:
                    self._set_queue_confirming(False)
                prompt = self.query_one("#prompt", PromptInput)
                prompt.disabled = False
                prompt.placeholder = PROMPT_PLACEHOLDER
                self._goal_editing = False
                # 选择题把焦点交给了卡片，回复结束后交还输入框
                if self._ask_widget is None:
                    prompt.focus()
            elif phase == "confirming_goal":
                self._goal_confirming = True
                prompt = self.query_one("#prompt", PromptInput)
                prompt.disabled = not self._goal_editing
                prompt.placeholder = (
                    "修改目标后按 Enter 重新确认…"
                    if self._goal_editing
                    else "等待目标确认…"
                )
            elif phase == "running":
                self.query_one("#prompt", PromptInput).placeholder = PROMPT_PLACEHOLDER
            # 遮罩只服务于初始化；任务运行中重连时直接进入界面
            if phase != "initializing":
                self._hide_loading()
        elif kind == "notice":
            self.notify(
                str(data.get("message", "")),
                title=str(data.get("title", "")) or None,
                severity=str(data.get("severity", "information")),
                timeout=float(data.get("timeout", 4)),
            )
