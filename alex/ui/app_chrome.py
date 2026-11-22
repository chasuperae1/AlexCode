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


from alex.ui.app_chrome_theme import ChromeThemeMixin
from alex.ui.app_chrome_palette import ChromePaletteMixin
class ChromeMixin(ChromeThemeMixin, ChromePaletteMixin):
    """界面外围：主题、任务与进度、目标卡、模型选择、命令面板。"""


    def action_toggle_debug(self) -> None:
        """Ctrl+O 展开/收起最近一段思考。"""
        boxes = list(self.query(Collapsible))
        if boxes:
            boxes[-1].collapsed = not boxes[-1].collapsed


    def _refresh_tasks_ui(self, data: dict) -> None:
        bar = self.query_one("#tasks", Static)
        if not data.get("visible"):
            bar.set_class(False, "-visible")
            return
        bar.update(str(data.get("summary", "")))
        bar.set_class(True, "-visible")

    def _set_progress(self, data: dict) -> None:
        """输入框左上角的一行进度，目标与队列共用。"""
        try:
            tag = self.query_one("#progress-tag", Static)
        except Exception:
            return
        text = str(data.get("text") or "")
        tone = str(data.get("tone") or "primary")
        classes = ("-warn", "-ok", "-bad", "-muted")
        if not text:
            tag.update("")
            tag.set_class(False, "-visible", *classes)
            return
        tag.update(Text(text))
        for name in classes:
            tag.set_class(name == f"-{tone}", name)
        tag.set_class(True, "-visible")

    def _apply_goal_card(self, data: dict) -> None:
        terminal = {"passed", "stopped", "impossible", "failed", "cancelled"}
        phase = str(data.get("phase") or "")
        if phase not in terminal:
            self._goal_phase = phase
        self._goal_confirming = phase == "awaiting_confirmation"
        card = self._goal_card_widget
        if card is None:
            card = GoalCard(data, self.current_theme)
            self._append_widget(card)
            self._goal_card_widget = card
        else:
            card.update_payload(data, self.current_theme)
        if str(data.get("phase") or "") in terminal:
            self._goal_card_widget = None
        if phase in {"understanding", "clarifying"} or self._goal_confirming:
            placeholder = {
                "understanding": "正在理解需求…",
                "clarifying": "请在下方选择补充条件…",
            }.get(phase)
            if placeholder is None:
                placeholder = (
                    "修改目标后按 Enter 重新确认…"
                    if self._goal_editing
                    else "等待目标确认…"
                )
            try:
                prompt = self.query_one("#prompt", PromptInput)
                prompt.disabled = not self._goal_editing
                prompt.placeholder = placeholder
            except Exception:
                pass

    def _enter_verifier(self) -> None:
        """验收开始：另起 Verifier 角色行，后续内容缩进显示。"""
        self._goal_phase = "verifying"
        if self._verifier_mode:
            return
        self._close_stream_block()
        self._verifier_mode = True
        theme = self.current_theme
        self._append_widget(
            Static(
                Text("Verifier", style=f"bold {theme.secondary}"),
                classes="role-verifier",
            )
        )

    def _exit_verifier(self) -> None:
        """验收结束、进入下一轮执行：恢复 Alex 角色与正文块。"""
        self._goal_phase = "executing"
        if not self._verifier_mode:
            return
        self._close_stream_block()
        self._verifier_mode = False

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not busy:
            self._stop_armed = False
            self._stopping = False
        self._refresh_meta_line()

    def _set_queue_confirming(self, active: bool, total: int = 0) -> None:
        """切换确认阶段：锁定输入框，并恢复提示文案。"""
        self._queue_confirming = active
        if active:
            self._queue_total = total
        else:
            self._queue_armed = False
            if self._queue_card_widget is not None:
                self._queue_card_widget.set_armed(False)
        try:
            prompt = self.query_one("#prompt", PromptInput)
        except Exception:
            return
        prompt.disabled = active
        prompt.placeholder = (
            "Waiting for confirmation…" if active else PROMPT_PLACEHOLDER
        )

    def _workspace_line(self):
        """Idle: workspace path. Busy: only the spinner + Esc hint."""
        if self._queue_confirming:
            theme = self.current_theme
            count = self._queue_total
            suffix = "task" if count == 1 else "tasks"
            if self._queue_armed:
                return Text(
                    "tasks will be cleared · esc again to discard · enter to start",
                    style=f"bold {theme.warning}",
                )
            return Text(
                f"{count} {suffix} ready · enter to start · esc to cancel",
                style=theme.secondary,
            )
        if not self._busy:
            return f"Workspace: {self._workspace} · {theme_label(self.theme)}"
        frame = self._spinner_frames[self._spinner_index % len(self._spinner_frames)]
        if self._stopping:
            label = "stopping…"
        elif self._stop_armed:
            label = "press esc again to confirm"
        else:
            label = "press esc to stop"
        if self._goal_phase:
            label = f"{self._goal_phase} · {label}"
        theme = self.current_theme
        line = Text()
        line.append(frame, style=f"bold {theme.accent}")
        line.append(
            f" {label}",
            style=f"bold {theme.warning}"
            if self._stop_armed or self._stopping
            else theme.secondary,
        )
        return line

    def _meta_text(self) -> Text:
        """顶栏 meta：只保留模型 / 上下文信息，不展示 rules / skills。"""
        return Text(self._meta_base)

    def _refresh_meta_line(self) -> None:
        try:
            self.query_one("#meta", Static).update(self._meta_text())
            self.query_one("#workspace-line", Static).update(self._workspace_line())
        except Exception:
            pass

    def _task_status_text(self) -> Text | None:
        """Input-bar status: live subagent / shell counts and longest runtime."""
        summary = self._live_runs.summary()
        if summary.total == 0 or summary.elapsed < MIN_VISIBLE_SECONDS:
            return None
        theme = self.current_theme
        tone = theme.warning if summary.long_running else theme.secondary
        count = (
            f"bold {theme.warning}"
            if summary.long_running
            else f"bold {theme.foreground}"
        )
        line = Text()
        line.append("● ", style=tone)
        if summary.subagents:
            line.append("Subagent ", style=theme.secondary)
            line.append(str(summary.subagents), style=count)
        if summary.subagents and summary.shells:
            line.append(" · ", style=theme.secondary)
        if summary.shells:
            line.append("Shell ", style=theme.secondary)
            line.append(str(summary.shells), style=count)
        line.append(" · ", style=theme.secondary)
        line.append(format_duration(summary.elapsed), style=tone)
        return line

    def _refresh_task_status(self) -> None:
        try:
            widget = self.query_one("#task-status", Static)
        except Exception:
            return
        line = self._task_status_text()
        if line is None:
            widget.update("")
            widget.set_class(False, "-visible")
        else:
            widget.update(line)
            widget.set_class(True, "-visible")

    def _tick_task_status(self) -> None:
        if self._live_runs.summary().total:
            self._refresh_task_status()

    def _refresh_sources(self) -> None:
        """设置页改动 rule / skill 后重新统计，保留本轮已读技能顺序。"""
        old = getattr(self, "_sources", None)
        state = load_sources()
        if old is not None:
            state.read_skills = {
                name: elapsed
                for name, elapsed in old.read_skills.items()
                if name in state.skills
            }
        self._sources = state
        self._refresh_meta_line()
        if getattr(self, "_display_mode", "developer") != "developer":
            return
        try:
            self.query_one(".rule-summary", Collapsible).remove()
        except Exception:
            pass
        widget = self._rules_summary_widget()
        if widget is None:
            return
        try:
            chat = self._chat()
            children = list(chat.children)
            if children:
                chat.mount(widget, before=children[0])
            else:
                chat.mount(widget)
        except Exception:
            pass

    def _tick_spinner(self) -> None:
        if self._loading_visible:
            self._boot_frame += 1
            self._refresh_loading()
            return
        if not self._busy:
            return
        self._spinner_index += 1
        self._refresh_meta_line()

    async def _open_model_picker(self) -> None:
        if self.link is None:
            return
        if self._busy:
            self.notify(
                "Busy — wait for the current reply to finish",
                severity="warning",
                timeout=2,
            )
            return
        try:
            catalog = await self._rpc("model_catalog")
        except Exception as exc:
            self.notify(
                f"Model list failed: {exc}",
                title="Model list failed",
                severity="error",
                timeout=6,
            )
            return
        if not catalog:
            self.notify("No model profiles configured", timeout=4)
            return
        self._model_catalog = {str(item["name"]): item for item in catalog}
        rows = [
            (str(item["name"]), bool(item.get("current"))) for item in catalog
        ]
        self.push_screen(
            PickerScreen(
                "Choose model",
                rows,
                "↑/↓ move · Enter select · Esc cancel",
            ),
            self._on_model_picked,
        )

    def _on_model_picked(self, name: str | None) -> None:
        if not name:
            return
        item = self._model_catalog.get(name, {})
        levels = [str(level) for level in item.get("levels") or []]
        if not levels:
            self.run_worker(self._switch_model(name))
            return
        current = item.get("level")
        rows = [(level, level == current) for level in levels]
        self.push_screen(
            PickerScreen(
                "Reasoning effort",
                rows,
                "↑/↓ move · Enter apply · Esc back",
            ),
            lambda level: self._on_effort_picked(name, level),
        )

    def _on_effort_picked(self, name: str, level: str | None) -> None:
        if level is None:
            self.run_worker(self._open_model_picker())
            return
        self.run_worker(self._switch_model(name, level))

    async def _switch_model(
        self, name: str, reasoning_level: str | None = None
    ) -> None:
        if self.link is None:
            return
        try:
            result = await self._rpc(
                "switch_model", name=name, reasoning_level=reasoning_level
            )
        except Exception as exc:
            self.notify(
                f"Switch failed: {exc}", title="Switch failed", severity="error", timeout=6
            )
            return
        effort = result.get("reasoning")
        suffix = f" · {effort}" if effort else ""
        if result.get("changed"):
            self.notify(f"Switched to {name}{suffix}", timeout=4)
        else:
            self.notify(f"Already using {name}{suffix}", timeout=2)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """命令面板打开时才接管方向键、回车和 Tab。"""
        if action == "escape":
            # 弹窗自己处理 Esc（取消/关闭），否则会被全局“停止回复”吞掉
            return not isinstance(self.screen, ModalScreen)
        if action.startswith("palette_"):
            return self._palette_visible
        if action.startswith("queue_"):
            return self._queue_confirming
        if action == "goal_confirm":
            return self._goal_confirming and not self._goal_editing
        if action == "goal_edit":
            return self._goal_confirming and not self._goal_editing
        return True

