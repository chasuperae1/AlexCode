"""Session 事件路由：把 daemon 事件分发到界面状态与弹窗。"""

from __future__ import annotations

import json
import time

from alex.config import reload_config
from alex.core.snapshots import Checkpoint
from alex.screens.confirm import ALLOW, ALWAYS, DENY
from alex.ui.constants import PROMPT_PLACEHOLDER, USER
from alex.ui.panels import QueueCard
from alex.ui.widgets import PromptInput


class EventRouterMixin:
    """事件分发入口：按 ``kind`` 查注册表，调用对应的短处理方法。"""

    # 事件名 -> 处理方法名。新增事件只加一行，不再往 if/elif 里塞分支。
    _EVENT_HANDLERS: dict[str, str] = {
        # 快照与启动
        "snapshot": "_on_snapshot",
        "boot": "_on_boot",
        "meta": "_on_meta",
        "status": "_on_status",
        "tasks": "_on_tasks",
        "progress": "_on_progress",
        "notice": "_on_notice",
        # 一轮对话的流式过程
        "exchange_start": "_on_exchange_start",
        "thinking_start": "_on_thinking_start",
        "thinking_delta": "_on_thinking_delta",
        "thinking_end": "_on_thinking_end",
        "text_delta": "_on_text_delta",
        "tool_start": "_on_tool_start",
        "tool_label": "_on_tool_label",
        "tool_result": "_on_tool_result",
        "reply_end": "_on_reply_end",
        # 确认与选择题
        "confirm_request": "_on_confirm_request",
        "confirm_end": "_on_confirm_end",
        "ask_request": "_on_ask_request",
        "ask_end": "_on_ask_end",
        # 上下文与历史
        "compaction": "_on_compaction",
        "compacting": "_on_compacting",
        "history_reset": "_on_history_reset",
        "checkpoints": "_on_checkpoints",
        "changes": "_on_changes",
        "shell_card": "_on_shell_card",
        # Goal / Queue
        "goal_card": "_on_goal_card",
        "goal_verify_start": "_on_goal_verify_start",
        "goal_round": "_on_goal_round",
        "goal_end": "_on_goal_end",
        "queue": "_on_queue",
        "queue_card": "_on_queue_card",
        "queue_result": "_on_queue_result",
        "queue_done": "_on_queue_done",
    }

    def _handle_event(self, kind: str, data: dict) -> None:
        """统一入口：先落 daemon 指定的历史记录，再分发到界面处理。

        记录必须和挂载同时发生，顺序也要一致（先写历史，再挂控件）；界面
        这边不再自行 ``view.history.append``，避免“事件带一份 + 本地再写一份”。
        ``record`` 可以是单条 ``[kind, payload]``，也可以是一组（reply_end 同时
        写正文和 stats）。
        """
        record = data.get("record")
        if record:
            if isinstance(record[0], (list, tuple)):
                for item in record:
                    self.view.record(item[0], item[1])
            else:
                self.view.record(record[0], record[1])
        handler = self._EVENT_HANDLERS.get(kind)
        if handler is not None:
            getattr(self, handler)(data)

    # ---------------------------------------------------------------- 快照 / 外壳
    def _on_snapshot(self, data: dict) -> None:
        self._apply_snapshot(data)

    def _on_boot(self, data: dict) -> None:
        self._boot_set(
            str(data.get("stage", "")),
            str(data.get("status", "idle")),
            str(data.get("detail", "")),
        )

    def _on_meta(self, data: dict) -> None:
        self._meta_base = str(data.get("label", ""))
        ws = data.get("workspace")
        if ws:
            self._workspace = str(ws)
            reload_config()
        self._refresh_meta_line()

    def _on_status(self, data: dict) -> None:
        self._set_busy(bool(data.get("busy")))
        self._stopping = bool(data.get("stopping", False))
        self._set_activity(str(data.get("activity", "")))
        phase = str(data.get("phase", ""))
        if phase in {"idle", "failed"}:
            self._settle_idle_phase(phase, data)
        elif phase == "confirming_goal":
            self._enter_goal_confirming()
        elif phase == "running":
            self.query_one("#prompt", PromptInput).placeholder = PROMPT_PLACEHOLDER
        # 遮罩只服务于初始化；任务运行中重连时直接进入界面
        if phase != "initializing":
            self._hide_loading()

    def _settle_idle_phase(self, phase: str, data: dict) -> None:
        """空闲 / 失败：收尾汇总卡、队列确认并把焦点交还输入框。"""
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

    def _enter_goal_confirming(self) -> None:
        self._goal_confirming = True
        prompt = self.query_one("#prompt", PromptInput)
        prompt.disabled = not self._goal_editing
        prompt.placeholder = (
            "Edit the goal and press Enter to confirm again…"
            if self._goal_editing
            else "Waiting for goal confirmation…"
        )

    def _on_tasks(self, data: dict) -> None:
        self._refresh_tasks_ui(data)

    def _on_progress(self, data: dict) -> None:
        self._set_progress(data)

    def _on_notice(self, data: dict) -> None:
        self.notify(
            str(data.get("message", "")),
            title=str(data.get("title", "")) or None,
            severity=str(data.get("severity", "information")),
            timeout=float(data.get("timeout", 4)),
        )

    # ---------------------------------------------------------------- 一轮对话的流式过程
    def _on_exchange_start(self, data: dict) -> None:
        exchange_id = int(data.get("exchange_id", self.view.exchange_seq))
        # 正在翻旧历史时又发了新消息：先回到最新窗口，否则这条消息和随后的
        # 流式回答都挂到旧窗口下面，用户根本看不见。
        self._ensure_latest_window()
        self._turn_text_streamed = False
        self._turn_diff_cards = 0
        self._concise_card = None
        self._verifier_mode = False
        self._goal_phase = ""
        self._set_progress({"text": ""})
        self.view.begin_exchange(exchange_id, data.get("text", ""))
        self._append_block(USER, data.get("text", ""), exchange_id)
        if self._concise:
            self._begin_concise_summary()

    def _on_thinking_start(self, data: dict) -> None:
        self.view.begin_thinking()
        self._thinking_ticks = 0
        self._thinking_started = time.monotonic()
        self._close_stream_block()
        self._begin_thinking()
        self._set_activity("Thinking…")

    def _on_thinking_delta(self, data: dict) -> None:
        self.view.add_thinking(data.get("delta", ""))
        self._stream_dirty = True

    def _on_thinking_end(self, data: dict) -> None:
        text = self.view.end_thinking(str(data.get("text", "") or ""))
        self._finish_thinking(text)
        self._stream_dirty = True

    def _on_text_delta(self, data: dict) -> None:
        self.view.add_text(data.get("delta", ""))
        self._turn_text_streamed = True
        self._begin_stream()
        self._stream_dirty = True

    def _on_tool_start(self, data: dict) -> None:
        tool_id = str(data.get("id", ""))
        self._live_tool_data[tool_id] = dict(data)
        self._close_stream_block()
        self._start_tool_row(tool_id, data)
        self._live_runs.start(tool_id, str(data.get("name") or ""))
        self._refresh_task_status()
        self._set_activity(f"Calling {data.get('name', '')}")

    def _on_tool_label(self, data: dict) -> None:
        """参数到位后补全任务名（如 Subagent 的描述），此时工具可能还在运行。"""
        tool_id = str(data.get("id", ""))
        live = self._live_tool_data.get(tool_id)
        if live is not None:
            live.update(data)
        row = self._tool_rows.get(tool_id)
        if row is not None:
            theme = self.current_theme
            line = self._event_line(
                str(data.get("kind", "tool")),
                str(data.get("label", "")),
                theme,
            )
            line.append(" …", style=theme.secondary)
            row.update(line)

    def _on_tool_result(self, data: dict) -> None:
        tool_id = str(data.get("id", ""))
        text = str(data.get("text", ""))
        self._live_tool_data.pop(tool_id, None)
        self._finish_tool_row(tool_id, data)
        self._live_runs.finish(tool_id)
        self._refresh_task_status()
        # Edit / Write 一完成就把这次的 diff 挂到该工具行下面（默认收起）
        self._append_tool_diff(data)
        # 历史由 daemon 的 record 统一写入（含 shell 卡），这里只负责挂载
        self._set_activity(text)

    def _on_shell_card(self, data: dict) -> None:
        """shell 卡的状态同步：历史已由 record 落好，屏幕上那张卡就地改状态。"""
        card = getattr(self, "_permission_card", None)
        if card is None:
            return
        request_id = str(data.get("request_id") or "")
        payload = dict(getattr(card, "_payload", {}) or {})
        if not request_id or request_id != str(payload.get("request_id") or ""):
            return
        state = str(data.get("state") or "")
        if state not in {"allowed", "denied", "done", "failed"}:
            return
        card.set_state(state)

    def _on_reply_end(self, data: dict) -> None:
        self._finish_reply(data)
        self.run_worker(self._prefetch_settings(), exclusive=False)

    # ---------------------------------------------------------------- 确认 / 选择题
    def _on_confirm_request(self, data: dict) -> None:
        # 权限等待独立成组：设置页/发送/刷新等默认组 worker 的 exclusive
        # 取消不能把正在等用户选择的卡片连带取消。
        self.run_worker(
            self._ask_confirm(data), exclusive=True, group="permission"
        )

    def _on_confirm_end(self, data: dict) -> None:
        # 另一个窗口先回应时，更新本窗口的内联卡，不再弹第二个确认层
        request_id = str(data.get("request_id", ""))
        choice = str(data.get("choice", ""))
        if request_id == self._confirm_request_id and self._permission_card is not None:
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

    def _on_ask_request(self, data: dict) -> None:
        self._show_ask(data)

    def _on_ask_end(self, data: dict) -> None:
        self._resolve_ask(data)

    # ---------------------------------------------------------------- 上下文 / 历史
    def _on_compaction(self, data: dict) -> None:
        self._append_compaction(
            str(data.get("summary", "")), str(data.get("detail", ""))
        )

    def _on_compacting(self, data: dict) -> None:
        self._show_compacting(bool(data.get("active")))

    def _on_history_reset(self, data: dict) -> None:
        self.view.reset_session(data.get("history", []))
        # 历史被整体替换（撤销/清空/重连）：窗口回到最新，旧下标已失效。
        self._window_start = None
        self._window_end = None
        if "checkpoints" in data:
            self.view.set_checkpoints(data.get("checkpoints") or [])
            self._history_needs_checkpoints = False
        else:
            # 旧服务端可能先发 history_reset、后发 checkpoints；等后者到达
            # 再重建一次，否则代码 diff 卡会因为缺少 checkpoint 而丢失。
            self._history_needs_checkpoints = True
        self._stream_offset = 0
        self._live_runs.clear()
        self._live_tool_data.clear()
        self._refresh_task_status()
        self._render_history()

    def _on_checkpoints(self, data: dict) -> None:
        self.view.set_checkpoints(data.get("checkpoints", []))
        if self._history_needs_checkpoints:
            self._history_needs_checkpoints = False
            if self.view.history:
                self._render_history()

    def _on_changes(self, data: dict) -> None:
        try:
            checkpoint = Checkpoint(**data.get("checkpoint", {}))
        except TypeError:
            return
        self._append_changes(checkpoint)

    # ---------------------------------------------------------------- Goal / Queue
    def _on_goal_card(self, data: dict) -> None:
        self._apply_goal_card(data)

    def _on_goal_verify_start(self, data: dict) -> None:
        self._enter_verifier()

    def _on_goal_round(self, data: dict) -> None:
        self._exit_verifier()

    def _on_goal_end(self, data: dict) -> None:
        self._goal_phase = ""
        self._verifier_mode = False

    def _on_queue(self, data: dict) -> None:
        index = int(data.get("index", 0))
        total = int(data.get("total", 0))
        label = f"Queue {index}/{total}" if total else ""
        self._append_block("queue", f"{label} · {data.get('subject', '')}")
        self._refresh_meta_line()

    def _on_queue_card(self, data: dict) -> None:
        state = str(data.get("state") or "")
        tasks = [str(item) for item in data.get("tasks") or []]
        card = self._queue_card_widget
        if card is None and state == "confirming":
            card = QueueCard(tasks, state="confirming", theme=self.current_theme)
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

    def _on_queue_result(self, data: dict) -> None:
        card = self._build_queue_result(json.dumps(data, ensure_ascii=False))
        if card is not None:
            self._append_widget(card)
        self._refresh_meta_line()

    def _on_queue_done(self, data: dict) -> None:
        self._append_block("queue_summary", str(data.get("text", "")))
        self._refresh_meta_line()
