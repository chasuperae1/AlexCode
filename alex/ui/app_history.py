"""聊天历史重建：把 history/checkpoint 还原成消息、工具行和 diff 卡。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from textual.widgets import Collapsible, Static

from alex.core.snapshots import Checkpoint
from alex.ui.constants import ASSISTANT, DEBUG_KINDS, USER
from alex.ui.activity import SHELL_TOOLS
from alex.ui.panels import ChatLog, ConciseSummary
from alex.ui.cards import ShellPermissionCard
from alex.ui.widgets import InlineDiffCard, ToolDiffCard


def _parse_skill_record(text: str) -> tuple[str, float] | None:
    """把 ``Skill <name> done · 1.2s`` 拆成技能名与耗时。"""
    if not text.startswith("Skill "):
        return None
    skill = text[6:].split(" done", 1)[0].split(" · ", 1)[0].strip()
    if not skill:
        return None
    seconds = 0.0
    tail = text.rsplit(" · ", 1)[-1].strip()
    if tail.endswith("s"):
        try:
            seconds = float(tail[:-1])
        except ValueError:
            seconds = 0.0
    return skill, seconds


def _align_checkpoints(
    history: list[tuple[str, str]], checkpoints: list[Checkpoint]
) -> tuple[dict[int, Checkpoint], list[Checkpoint]]:
    """重启后 history 会丢掉 tool 行，按末尾用户消息对齐 checkpoint。"""
    user_indices = [
        position for position, item in enumerate(history) if item[0] == USER
    ]
    ordered = sorted(
        checkpoints, key=lambda item: (item.history_start, item.exchange_id)
    )
    tail = ordered[-len(user_indices):] if user_indices else []
    mapping = dict(zip(user_indices[-len(tail):], tail))
    return mapping, list(checkpoints)


@dataclass
class _HistoryBuild:
    """一次历史重建的中间状态，避免在循环里传十个闭包变量。"""

    theme: Any
    concise: bool
    remaining: list[Checkpoint]
    widgets: list = field(default_factory=list)
    inline_diffs: list[tuple[int, InlineDiffCard]] = field(default_factory=list)
    tool_cards: list[tuple[int, ToolDiffCard]] = field(default_factory=list)
    tool_card_done: set[int] = field(default_factory=set)
    pending: list[tuple[str, str]] = field(default_factory=list)
    deferred: list = field(default_factory=list)
    current_exchange: int | None = None
    turn_widget_start: int = 0
    skip_shell_request: str = ""
    shell_cards: dict[str, ShellPermissionCard] = field(default_factory=dict)


class HistoryMixin:
    """历史回放与代码卡恢复。"""

    def _render_history(self) -> None:
        """重建整个聊天区：用户消息 → 折叠的调试组 → 回答。"""
        chat = self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        body = chat.query_one("#chat-body")
        # 正在展示的权限卡直接留在原处，避免 clear → 重挂之间出现空窗或叠卡；
        # 已落进 history 的 shell 卡则由下面的 _history_shell_card_entry 重建。
        active = getattr(self, "_permission_card", None)
        keep = active if active is not None and active.parent is body else None
        skip_request = ""
        if keep is not None:
            skip_request = str((keep._payload or {}).get("request_id") or "")
        # 重建会清空再重挂，滚动位置会被重置到顶部；先记住重建前的状态。
        # 正在跟随贴底的中间态也算“在底部”，否则连续两次重建（history_reset
        # 紧跟 checkpoints）会把视口定格在半途。
        at_bottom = chat.is_vertical_scroll_end or chat._follow_timer is not None
        anchor = int(chat.scroll_y)
        live_summary = self._concise_card_widget() if self._concise else None
        live_summary_state = (
            live_summary.snapshot_state()
            if isinstance(live_summary, ConciseSummary)
            else None
        )
        ask_payload = dict(getattr(self, "_ask_payload", None) or {})
        thinking_started = float(getattr(self, "_thinking_started", 0.0) or 0.0)
        self._reset_history_view()
        chat._suspend_scroll = True
        chat.clear(keep=keep)

        checkpoint_by_user, remaining = _align_checkpoints(
            self.view.history, self.view.checkpoints
        )
        build = _HistoryBuild(
            theme=self.current_theme,
            concise=self._concise,
            remaining=remaining,
            skip_shell_request=skip_request,
        )
        self._build_history(build, checkpoint_by_user)
        if live_summary_state is not None:
            # 当前用户回合里，一个内部 reply 可能产生多个 checkpoint，历史回放
            # 会各生成一张 step 卡；而界面上本来只有一张实时卡。重建时把本回合
            # 所有历史摘要卡统一替换成内存里的实时卡，保证一张回答只有一张卡。
            card_positions = [
                position
                for position, widget in enumerate(build.widgets)
                if position >= build.turn_widget_start
                and isinstance(widget, ConciseSummary)
            ]
            insert_at = (
                card_positions[0] if card_positions else len(build.widgets)
            )
            for position in reversed(card_positions):
                build.widgets.pop(position)
            restored = ConciseSummary(self.current_theme)
            restored.restore_state(live_summary_state)
            build.widgets.insert(insert_at, restored)
            self._concise_card = restored
        self._mount_history(
            body,
            build,
            before=keep,
            thinking_started=thinking_started,
        )
        if build.concise and keep is not None:
            summary = self._concise_card_widget()
            tool = str((keep._payload or {}).get("tool") or "")
            if summary is not None and tool in SHELL_TOOLS:
                summary.start_shell(tool)
                summary.wait_shell(tool)
        if ask_payload:
            # 未作答的选择题不在 history 里，必须按请求重挂，否则换主题后消失。
            self._show_ask(ask_payload)
            # 重建发生在 Settings 返回 / 切主题之后：焦点应回到主输入框，
            # 而不是被新挂载的答题卡抢走。
            self._restore_prompt_focus()
        chat._suspend_scroll = False
        # 重启后还没有卡、但会话里仍有未决请求：补一张可交互的卡
        if getattr(self, "_permission_pending", False) and active is None:
            self._attach_permission_card()
        self._restore_chat_scroll(chat, at_bottom=at_bottom, anchor=anchor)

    def _restore_chat_scroll(
        self, chat: ChatLog, at_bottom: bool, anchor: int
    ) -> None:
        """重建历史后回到原来的阅读位置。

        原来停在底部（包括启动时聊天区还是空的情况）就重新贴底，否则回到
        原偏移。历史是一次性 ``mount_all`` 挂上的，Textual 不会因为内容变高
        而自动滚动，不主动贴底时视口会停在最旧的第一条消息，用户每次打开
        CLI 都要自己滚到最新。
        """
        if at_bottom:
            # 历史一次性挂载，高度分几帧才稳定：跟着贴底，稳定后自动停
            chat.follow_bottom()
            return

        chat._stop_follow()

        def _restore_anchor() -> None:
            chat.scroll_y = anchor

        self.call_after_refresh(_restore_anchor)

    def _reset_history_view(self) -> None:
        self._ask_widget = None
        self._ask_request_id = ""
        self._compacting_line = None
        self._queue_card_widget = None
        self._goal_card_widget = None
        self._stream_md = None
        self._stream_offset = 0
        self._turn_text_streamed = False
        self._turn_diff_cards = 0
        self._concise_card = None
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows = {}

    def _build_history(
        self,
        build: _HistoryBuild,
        checkpoint_by_user: dict[int, Checkpoint],
    ) -> None:
        if not build.concise:
            rules_widget = self._rules_summary_widget()
            if rules_widget is not None:
                build.widgets.append(rules_widget)
        index = 0
        while index < len(self.view.history):
            kind, content = self.view.history[index]
            if kind in ("rule", "skill"):
                # 来源已由顶部 Rules 行 / 摘要卡统一呈现，旧历史里的公告行不再重复
                index += 1
                continue
            if kind == "shell_card":
                self._history_shell_card_entry(build, content)
            elif kind in DEBUG_KINDS:
                self._history_debug_entry(build, kind, content)
            elif kind == USER:
                self._history_user_entry(build, index, content, checkpoint_by_user)
            else:
                self._history_reply_entry(build, kind, content)
            index += 1
        if build.concise:
            checkpoint = (
                self._checkpoint_for(build.current_exchange)
                if build.current_exchange is not None
                else None
            )
            self._flush_pending(build, checkpoint)

    def _history_shell_card_entry(
        self, build: _HistoryBuild, content: str
    ) -> None:
        """shell 记录：简洁模式并入步骤摘要；开发者模式还原完整卡片。"""
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        request_id = str(data.get("request_id") or "")
        if request_id and request_id == build.skip_shell_request:
            return
        if build.concise:
            build.pending.append(("shell_card", content))
            return
        existing = build.shell_cards.get(request_id) if request_id else None
        if existing is not None:
            # 同一次确认可能既有会话侧记录、又有本地即时记录：只保留后到的状态
            existing._payload.update(data)
            state = str(data.get("state") or "done")
            if state not in {"allowed", "denied", "done", "failed"}:
                state = "done"
            existing.set_state(state)
            existing.set_output(
                str(data.get("stdout") or ""),
                exit_code=int(data.get("exit_code", 0) or 0),
                failed=state == "failed",
            )
            return
        payload = dict(data)
        payload["readonly"] = True
        card = ShellPermissionCard(payload, build.theme, lambda _choice: None)
        card.can_focus = False
        state = str(data.get("state") or "done")
        if state not in {"allowed", "denied", "done", "failed"}:
            state = "done"
        card.set_state(state)
        card.set_output(
            str(data.get("stdout") or ""),
            exit_code=int(data.get("exit_code", 0) or 0),
            failed=state == "failed",
        )
        build.widgets.append(card)
        if request_id:
            build.shell_cards[request_id] = card

    def _history_debug_entry(
        self, build: _HistoryBuild, kind: str, content: str
    ) -> None:
        if build.concise:
            build.pending.append((kind, content))
            return
        # 思考单独折叠；工具 / 子代理每行一条，保持原始顺序
        if kind == "think":
            build.widgets.append(
                Collapsible(
                    Static(
                        self._thinking_text(content, build.theme),
                        classes="debug-body",
                    ),
                    title=f"Thinking · {len(content):,} chars",
                    collapsed=True,
                    classes="debug",
                )
            )
            return
        if kind in ("result", "error"):
            parsed = _parse_skill_record(content) if kind == "result" else None
            if parsed is not None:
                skill, seconds = parsed
                state = getattr(self, "_sources", None)
                size = state.skill_size(skill) if state is not None else 0
                build.widgets.append(
                    Static(
                        self._skill_line(skill, size, seconds, build.theme),
                        classes="tool-line",
                    )
                )
            else:
                build.widgets.append(
                    Static(
                        self._tool_line(content, kind == "result", build.theme),
                        classes="tool-line",
                    )
                )
            self._history_tool_card_entry(build, kind, content)
            return
        build.widgets.append(
            Static(
                self._event_line(kind, content, build.theme),
                classes="tool-line",
            )
        )

    def _history_tool_card_entry(
        self, build: _HistoryBuild, kind: str, content: str
    ) -> None:
        if (
            kind != "result"
            or build.current_exchange is None
            or build.current_exchange in build.tool_card_done
            or not content.startswith(("Write done", "Edit done"))
        ):
            return
        checkpoint = self._checkpoint_for(build.current_exchange)
        card = (
            self._history_tool_card(checkpoint)
            if checkpoint is not None
            else None
        )
        if card is None:
            return
        build.widgets.append(card)
        build.tool_cards.append((build.current_exchange, card))
        build.tool_card_done.add(build.current_exchange)

    def _history_user_entry(
        self,
        build: _HistoryBuild,
        index: int,
        content: str,
        checkpoint_by_user: dict[int, Checkpoint],
    ) -> None:
        if build.concise and (build.pending or build.deferred):
            self._flush_pending(build)
        exchange_id = None
        mapped = checkpoint_by_user.get(index)
        if mapped is not None and mapped in build.remaining:
            exchange_id = mapped.exchange_id
            build.remaining.remove(mapped)
        else:
            for position, checkpoint in enumerate(build.remaining):
                if checkpoint.history_start == index or self._same_prompt(
                    content, checkpoint.prompt
                ):
                    exchange_id = checkpoint.exchange_id
                    build.remaining.pop(position)
                    break
        build.widgets.extend(self._make_block_widgets(USER, content, exchange_id))
        build.current_exchange = exchange_id
        # 记住当前用户回合在 widgets 里的起点；重建时整回合只能有一张摘要卡。
        build.turn_widget_start = len(build.widgets)

    def _history_reply_entry(
        self, build: _HistoryBuild, kind: str, content: str
    ) -> None:
        if build.concise and kind != ASSISTANT:
            # ask / queue / goal 等插在回合中间的块，先不结算摘要；统一延后，
            # 保证一次回答只生成一张 step 卡，而不是每个 ask 前都截断一张。
            build.deferred.extend(self._make_block_widgets(kind, content))
            return
        checkpoint = None
        if kind == ASSISTANT and build.current_exchange is not None:
            checkpoint = self._checkpoint_for(build.current_exchange)
        if (
            not build.concise
            and kind == ASSISTANT
            and checkpoint is not None
            and len(checkpoint.files) == 1
            and checkpoint.exchange_id not in build.tool_card_done
        ):
            card = self._history_tool_card(checkpoint)
            if card is not None:
                build.widgets.append(card)
                build.tool_cards.append((checkpoint.exchange_id, card))
                build.tool_card_done.add(checkpoint.exchange_id)
        build.widgets.extend(self._make_block_widgets(kind, content))
        if kind == ASSISTANT and checkpoint is not None:
            if checkpoint.files and not build.concise and len(checkpoint.files) > 1:
                diff_card = InlineDiffCard(checkpoint, build.theme)
                build.widgets.append(diff_card)
                build.inline_diffs.append((checkpoint.exchange_id, diff_card))

    def _build_concise_card(
        self,
        records: list[tuple[str, str]],
        checkpoint: Checkpoint | None = None,
        exchange_id: int | None = None,
    ) -> ConciseSummary:
        """把一轮历史过程还原成静态汇总卡（不展开、不放 shell 正文）。"""
        card = ConciseSummary(self.current_theme)
        card.exchange_id = exchange_id
        self._concise_card = card
        state = getattr(self, "_sources", None)
        if state is not None:
            card.set_rules(len(state.rules), state.rules_size)
        has_shell = any(kind == "shell_card" for kind, _ in records)
        latest_shell: dict[str, dict] = {}
        for record_kind, record_content in records:
            if record_kind != "shell_card":
                continue
            try:
                data = json.loads(record_content)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            request_id = str(data.get("request_id") or "")
            if request_id:
                latest_shell[request_id] = data
        seen_shell: set[str] = set()
        for record_kind, record_content in records:
            if record_kind == "shell_card":
                try:
                    data = json.loads(record_content)
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                request_id = str(data.get("request_id") or "")
                if request_id:
                    if request_id in seen_shell:
                        continue
                    seen_shell.add(request_id)
                    data = latest_shell.get(request_id, data)
                card.step()
                card.add_shell_record(data)
                continue
            if record_kind == "think":
                card.add_thinking(len(record_content), count_step=True)
                continue
            ok = record_kind == "result"
            parsed = _parse_skill_record(record_content) if ok else None
            if parsed is not None:
                skill, seconds = parsed
                card.step()
                if state is not None:
                    state.mark_skill(skill, seconds)
                    size = state.skill_size(skill)
                else:
                    size = 0
                card.add_skill(skill, size, seconds)
                continue
            name = (
                record_content.split(" done")[0][:24]
                if ok
                else record_kind.title()
            )
            if has_shell and name in SHELL_TOOLS:
                # shell_card 已经承载了状态，旧的结果行不再重复计数
                continue
            card.step()
            card.add_tool(name, ok, record_content)
        duration = checkpoint.duration if checkpoint is not None else 0.0
        card.finish(duration)
        if checkpoint is not None and checkpoint.files:
            if len(checkpoint.files) == 1:
                # 单文件能还原具体路径，避免重启后只剩 “1 file +18 -2”
                card.add_diff(
                    checkpoint.files[0],
                    checkpoint.additions,
                    checkpoint.deletions,
                )
            else:
                card.set_changes(
                    len(checkpoint.files),
                    checkpoint.additions,
                    checkpoint.deletions,
                )
        return card

    def _flush_pending(
        self, build: _HistoryBuild, checkpoint: Checkpoint | None = None
    ) -> None:
        if build.pending:
            exchange_id = (
                checkpoint.exchange_id
                if checkpoint is not None
                else build.current_exchange
            )
            build.widgets.append(
                self._build_concise_card(
                    build.pending,
                    checkpoint,
                    exchange_id,
                )
            )
            build.pending = []
        if build.deferred:
            build.widgets.extend(build.deferred)
            build.deferred = []

    def _mount_history(
        self,
        body,
        build: _HistoryBuild,
        before=None,
        *,
        thinking_started: float = 0.0,
    ) -> None:
        # 一次性挂载所有控件，避免逐个 mount 触发 N 次布局重排
        if build.widgets:
            body.mount_all(build.widgets, before=before)
        for exchange_id, diff_card in build.inline_diffs:
            self.run_worker(
                self._load_inline_diff(exchange_id, diff_card),
                exclusive=False,
            )
        for exchange_id, tool_card in build.tool_cards:
            self.run_worker(
                self._load_tool_card(exchange_id, tool_card),
                exclusive=False,
            )
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows = {}
        # 思考先于工具行开始：重建时也要先挂思考、再挂运行中的工具行，
        # 否则主题切换会把两行的先后顺序倒过来。
        if self.view.thinking:
            self._begin_thinking()
            if thinking_started:
                self._thinking_started = thinking_started
            self._update_thinking()
        self._restore_live_tool_rows(before=before)
        if self.view.stream_text:
            self._begin_stream()
