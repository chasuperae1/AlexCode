"""Session 事件分发主体：快照、确认、流式收尾；路由与历史已拆出。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text

from alex.config import reload_config
from alex.daemon.client import LinkError
from alex.screens.ask_choice import AskChoice
from alex.screens.confirm import DENY
from alex.sound import chime
from alex.ui.constants import PROMPT_PLACEHOLDER
from alex.ui.activity import SHELL_TOOLS
from alex.ui.panels import ChatLog, CompactionStatus
from alex.ui.widgets import PermissionDiffCard, PromptInput, ShellPermissionCard

from alex.ui.app_event_router import EventRouterMixin
from alex.ui.app_history import HistoryMixin


def permission_card_class(tool_name: str):
    """shell 类工具用独立的命令卡；文件类工具仍走 diff 卡。"""
    return (
        ShellPermissionCard if tool_name in SHELL_TOOLS else PermissionDiffCard
    )


class EventsMixin(EventRouterMixin, HistoryMixin):
    """事件分发入口：路由、快照、确认与历史回放通过组合提供。"""


    def _apply_snapshot(self, data: dict) -> None:
        # 快照整体替换 history：窗口下标失效，回到最新。
        # 续连快照例外：历史由 resync 补发，本地窗口（含翻页位置）保持不动。
        if not data.get("resume"):
            self._window_start = None
            self._window_end = None
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
        self.view.apply_snapshot(data)
        self._load_subagents(data.get("subagents"))
        pending_ask = data.get("pending_ask")
        if not pending_ask:
            # 另一窗口已作答或请求已取消：快照重建前先丢掉本地旧问题
            self._ask_payload = None
        self._meta_base = str(data.get("meta", "") or "")
        ws = data.get("workspace")
        if ws:
            self._workspace = str(ws)
            reload_config()
        queue = data.get("queue") or {}
        self._set_queue_confirming(
            bool(queue.get("confirming")),
            int(queue.get("total", 0) or 0),
        )
        self._verifier_mode = False
        self._live_tool_data.clear()
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
        self._refresh_task_status()
        self.query_one("#prompt", PromptInput).disabled = phase not in {
            "idle",
            "failed",
        } and not self._goal_confirming
        if self._goal_confirming:
            self.query_one("#prompt", PromptInput).disabled = True
            self.query_one("#prompt", PromptInput).placeholder = (
                "Waiting for goal confirmation…"
            )
        if phase == "idle" and self._settings_cache is None:
            self.run_worker(self._prefetch_settings(), exclusive=False)
        if phase != "initializing" and self.view.stream_text:
            self._begin_stream()
            self._stream_dirty = True
        pending = data.get("pending_confirm")
        if pending:
            self.run_worker(
                self._ask_confirm(pending), exclusive=True, group="permission"
            )
        pending_ask = data.get("pending_ask")
        if pending_ask:
            self._show_ask(pending_ask)
        elif self._ask_payload:
            # 另一窗口已作答或请求已取消：不要在本窗口重建时复活旧问题
            self._ask_payload = None

    def _set_permission_lock(self, active: bool) -> None:
        """权限未决时锁住输入；选择后恢复，但仍受 busy / goal / queue 约束。"""
        self._permission_pending = active
        try:
            prompt = self.query_one("#prompt", PromptInput)
        except Exception:
            return
        if active:
            prompt.disabled = True
            prompt.placeholder = "Waiting for permission…"
            return
        if self._goal_confirming or self._queue_confirming:
            return
        prompt.disabled = False
        prompt.placeholder = PROMPT_PLACEHOLDER
        prompt.focus()

    def _attach_permission_card(self) -> None:
        """按当前主题重建卡片（主题切换后文字颜色也要跟着变）。"""
        payload = getattr(self, "_permission_payload", None)
        future = getattr(self, "_confirm_future", None)
        if not payload or future is None or future.done():
            return
        card_class = permission_card_class(str(payload.get("tool") or ""))
        card = card_class(
            payload,
            self.current_theme,
            lambda value: (
                future.set_result(value) if not future.done() else None
            ),
        )
        self._permission_card = card
        try:
            chat = self._chat_ref or self.query_one("#chat", ChatLog)
        except Exception:
            return
        chat.add_widget(card)
        self._place_concise_summary(before=card)
        card.focus()

    async def _ask_confirm(self, payload: dict) -> None:
        """把权限请求变成聊天流里的内联卡：shell 用命令卡，文件用 diff 卡。"""
        request_id = str(payload.get("request_id", ""))
        if (
            request_id
            and request_id == self._confirm_request_id
            and self._permission_card is not None
        ):
            # 历史重建会重挂卡片；同一次请求不重复建卡
            return
        self._ensure_latest_window()
        self._permission_payload = dict(payload)
        # 需要点 accept 时提醒一次；同一请求重建卡片不重复响（上面已挡掉）
        chime("permission")
        choice = None
        future = asyncio.get_running_loop().create_future()
        card_class = permission_card_class(str(payload.get("tool") or ""))
        card = card_class(
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
        if self._concise and self._concise_card_widget() is None:
            # 重启后直接恢复未决权限时 history 里还没有本轮摘要，先补一张，
            # 保证“摘要在上、权限卡在下”，而不是只剩一张孤零零的权限卡。
            summary = self._ensure_concise_summary()
            if summary is not None:
                summary.step(f"calling {str(payload.get('tool') or 'tool')}")
        try:
            chat = self._chat_ref or self.query_one("#chat", ChatLog)
            self._chat_ref = chat
            chat.add_widget(card)
            card.focus()
            if self._concise:
                summary = self._concise_card_widget()
                if summary is not None and str(payload.get("tool") or "") in SHELL_TOOLS:
                    tool = str(payload.get("tool") or "")
                    summary.ensure_shell(tool)
                    summary.wait_shell(tool)
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
            tool = str(payload.get("tool") or "")
            if self._concise:
                summary = self._concise_card_widget()
                if summary is not None and tool in SHELL_TOOLS:
                    if choice == DENY:
                        summary.deny_shell(tool)
                    elif choice is not None:
                        summary.allow_shell(tool)
                if choice == DENY:
                    card.set_state("denied")
                card.remove()
                self._permission_card = None
                self._permission_payload = None
            elif choice == DENY:
                card.set_state("denied")
                self._permission_card = None
                self._permission_payload = None
        if self._confirm_resolved or self.link is None:
            # 另一个窗口已经回应了这个请求
            return
        try:
            await self._rpc(
                "confirm", request_id=request_id, choice=choice or DENY
            )
        except (LinkError, Exception) as exc:
            card.set_state("failed")
            card._payload["fail_text"] = f"Permission reply failed: {exc}"
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
        self._ensure_latest_window()
        if self._ask_widget is not None:
            self._ask_widget.mark_answer("Cancelled", skipped=True)
        self._ask_payload = dict(payload)
        # 需要你答题时也提醒一次（上面已挡掉同一次请求的重挂）
        chime("ask")
        self._ensure_concise_summary()
        widget = AskChoice(payload, self._answer_ask)
        self._ask_widget = widget
        self._ask_request_id = request_id
        chat = self._chat_ref or self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        chat.add_widget(widget)
        self._place_concise_summary(before=widget)
        self._set_activity("Waiting for your answer…")
        # 未答问题不能抢占主输入框：用户可能正想继续输入新问题。
        # 答题选项仍可点击；键盘焦点默认留在底部 Message 框。
        self._restore_prompt_focus()

    def _answer_ask(
        self,
        request_id: str,
        choices: list[str],
        other: str,
        skipped: bool,
    ) -> None:
        images: list[Path] = []
        for token, path in self._ask_pasted_images.items():
            if token in other:
                images.append(path)
                other = other.replace(token, "")
        # 答题卡只有一个自定义输入框：答完就把它的图片映射清掉
        self._ask_pasted_images.clear()
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
        summary = str(data.get("summary", "") or "Skipped")
        skipped = bool(data.get("skipped"))
        widget = self._ask_widget
        if widget is not None:
            widget.mark_answer(summary, skipped)
        else:
            self._append_block("ask", summary)
        self._ask_widget = None
        self._ask_request_id = ""
        self._ask_payload = None
        self._seal_stream()
        self._restore_prompt_focus()

    def _seal_stream(self) -> None:
        """作答后封口正文块与思考组，后续内容另起新块排在答案之后。"""
        # 正文统一走 _close_stream_block：这段文字会同时落进 history，
        # 否则重建后只剩答案、答案之前的正文凭空消失。
        self._close_stream_block()
        thinking = self.view.end_thinking()
        self._finish_thinking(thinking)
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0

    def _restore_prompt_focus(self) -> None:
        try:
            self.query_one("#prompt", PromptInput).focus()
        except Exception:  # 输入框未挂载时无需聚焦
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
            self._compacting_line = CompactionStatus()
            chat.add_widget(self._compacting_line)
        else:
            line, self._compacting_line = self._compacting_line, None
            if line is not None:
                line.remove()

    def _finish_reply(self, data: dict) -> None:
        self._verifier_mode = False
        self._goal_phase = ""
        if self._ask_widget is not None:
            self._ask_widget.mark_answer("Cancelled", skipped=True)
            self._ask_widget = None
            self._ask_request_id = ""
            self._ask_payload = None
        self._show_compacting(False)
        segment = self.view.stream_text[self._stream_offset:]
        streamed = self._turn_text_streamed
        self.view.stream_text = ""
        self._turn_text_streamed = False
        text = str(data.get("text", "") or "")
        if text:
            display = segment if segment else (text if not streamed else "")
            if display:
                if self._stream_md is None:
                    self._begin_stream()
                if self._stream_md is not None:
                    self._render_stream_segment(display)
        self._stream_md = None
        self._stream_offset = 0
        self._stream_frozen = []
        self._stream_frozen_upto = 0
        self._stream_hidden_marker = None
        if self._thinking_box is not None:
            self._finish_thinking(self.view.thinking.strip())
        self.view.thinking = ""
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
        else:
            # 回答下方补一行耗时/用量；历史里同一条记录会在重建时还原它
            self._append_turn_stats(
                duration,
                int(data.get("input_tokens", 0) or 0),
                int(data.get("output_tokens", 0) or 0),
                int(data.get("cache_tokens", 0) or 0),
                bool(data.get("compacted")),
            )
        # 本轮真正停下来才响：用户自己按停止的不提醒，队列里还有任务也不提醒
        if not data.get("interrupted") and not self._queue_pending:
            chime("reply_end")
        self._maybe_trim_window()

    async def on_unmount(self) -> None:
        if self.link is not None:
            await self.link.close()

    def on_resize(self, event) -> None:
        """窄屏时压缩聊天区左右留白。"""
        pad = 1 if event.size.width < 80 else 2
        for selector in ("#chat", "#subagents"):
            try:
                self.query_one(selector).styles.padding = (1, pad, 0, pad)
            except Exception:
                pass


    def _jump_to_latest(self) -> None:
        chat = self.query_one("#chat", ChatLog)
        if getattr(self, "_window_start", None) is not None:
            self._window_start = None
            self._window_end = None
            self._render_history()
            chat.follow_bottom()
            return
        chat.scroll_end(animate=False)

    def on_click(self, event) -> None:
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "jump":
            self._jump_to_latest()
        elif widget_id == "settings-link":
            self._open_settings()
        elif widget_id == "task-status":
            event.stop()
            self.toggle_subagents()
