"""AlexApp 的输入与命令：快捷键动作、队列、粘贴、发送、权限、设置、斜杠命令。"""

from __future__ import annotations

from alex.screens.dir_picker import DirectoryPicker
from alex.ui.panels import QueueButton
from alex.ui.widgets import InlineDiffCard, PromptInput, ToolDiffCard


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
