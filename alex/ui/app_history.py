"""聊天历史重建：把 history/checkpoint 还原成消息、工具行和 diff 卡。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from textual.widgets import Collapsible, Static

from alex.core.snapshots import Checkpoint
from alex.daemon.client import LinkError
from alex.ui.constants import ASSISTANT, DEBUG_KINDS, USER
from alex.ui.activity import SHELL_TOOLS
from alex.ui.chat_window import (
    PAGE_UNITS,
    TRIM_UNITS,
    earlier_start,
    forward_end,
    tail_start,
    window_units,
)
from alex.ui.panels import ChatLog, ConciseSummary
from alex.ui.panels_chat import WindowEdge
from alex.ui.cards import ShellPermissionCard
from alex.ui.widgets import ChangesSummary, InlineDiffCard, ToolDiffCard


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
    # 已有 changes 记录的回合：变更卡由记录还原，不再用 checkpoint 补第二张
    changes_exchanges: set[int] = field(default_factory=set)
    # 上一条历史类型：连续 verifier_text 只挂一次角色行
    last_kind: str = ""


def _changes_exchanges(history: list[tuple[str, str]]) -> set[int]:
    """扫出已写入 changes 记录的回合 id。"""
    exchanges: set[int] = set()
    for kind, content in history:
        if kind != "changes":
            continue
        try:
            data = json.loads(content)
            checkpoint = data.get("checkpoint") or {}
            exchanges.add(int(checkpoint.get("exchange_id") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    return exchanges


class HistoryMixin:
    """历史回放与代码卡恢复。"""

    # 当前挂载的窗口区间 [start, end)；None 表示“跟随最新”。
    # 只影响控件，不删 history。
    _window_start: int | None = None
    _window_end: int | None = None
    # 正在向会话补更早的一页：避免连续按 Ctrl+↑ 发出多条重复请求
    _earlier_fetching = False

    def _render_history(self) -> None:
        """重建整个聊天区：用户消息 → 折叠的调试组 → 回答。"""
        chat = self.query_one("#chat", ChatLog)
        self._chat_ref = chat
        body = chat.query_one("#chat-body")
        # 未作答的选择题、审批卡不在 history 里，必须挂在可见处。
        # 正在翻旧窗口时直接切回最新，避免 _show_ask/_attach_permission_card
        # 再触发一次递归重建、让外层用旧锚点恢复滚动。
        if self._window_start is not None and (
            getattr(self, "_ask_payload", None)
            or getattr(self, "_permission_pending", False)
        ):
            self._window_start = None
            self._window_end = None
        start, end = self._history_bounds()
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
        # 正在翻旧窗口时，当前回合的实时卡/流式块不属于这个区间。
        # 尾部窗口的判定看“是否跟随最新”，不能只看 end：前插页的 end 也可能
        # 正好等于总数，但视口其实停在旧内容中间。
        at_tail = self._window_start is None
        live_summary = (
            self._concise_card_widget() if self._concise and at_tail else None
        )
        live_summary_state = (
            live_summary.snapshot_state()
            if isinstance(live_summary, ConciseSummary)
            else None
        )
        ask_payload = dict(getattr(self, "_ask_payload", None) or {})
        thinking_started = float(getattr(self, "_thinking_started", 0.0) or 0.0)
        self._reset_history_view()
        chat._suspend_scroll = True
        # 聊天区宽度此时已经确定：让历史里的 Markdown 块在构造阶段就按它排版，
        # 新一页挂上来的第一帧就有正文，不会先出现一排空块再逐块填字。
        self._md_build_width = int(
            body.content_region.width or body.size.width or 0
        )
        # 同一份宽度写进渲染上下文（旧控件马上要被替换，不必广播重排）
        context = getattr(self, "render_context", None)
        if context is not None and self._md_build_width > 0:
            context.set_width(self._md_build_width, broadcast=False)

        checkpoint_by_user, remaining = _align_checkpoints(
            self.view.history, self.view.checkpoints
        )
        build = _HistoryBuild(
            theme=self.current_theme,
            concise=self._concise,
            remaining=remaining,
            skip_shell_request=skip_request,
            changes_exchanges=_changes_exchanges(self.view.history),
        )
        self._build_history(build, checkpoint_by_user, start=start, end=end)
        self._insert_window_edges(build, start, end, len(self.view.history))
        # 新一页先在旧内容还挂在屏上的时候构造完，再一次性换掉。
        # 反过来（先 clear 再 build）会让聊天区在新控件就绪前空上一帧甚至几帧，
        # 用户看到的就是整块白闪。
        chat.clear(keep=keep)
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
            live=at_tail,
        )
        self._md_build_width = 0
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

    def action_history_earlier(self) -> None:
        self.load_earlier_history()

    def action_history_newer(self) -> None:
        self.load_newer_history()

    def _history_bounds(self) -> tuple[int, int]:
        """当前应该挂载的 [start, end)；None 表示跟随最新。"""
        history = self.view.history
        total = len(history)
        if total == 0:
            self._window_start = None
            self._window_end = None
            return 0, 0
        if self._window_start is None:
            return tail_start(history), total
        start = int(self._window_start)
        end = (
            int(self._window_end)
            if self._window_end is not None
            else forward_end(history, start)
        )
        # 会话被撤销/清空，或窗口起点已经落进尾屏：回到跟随最新。
        # 注意 end 可能正好等于 total（前插页还没裁掉尾部），不能因此判定为尾屏。
        if start >= total or start >= tail_start(history):
            self._window_start = None
            self._window_end = None
            return tail_start(history), total
        if end <= start:
            end = forward_end(history, start)
        if end <= start:
            self._window_start = None
            self._window_end = None
            return tail_start(history), total
        self._window_end = end
        return start, min(end, total)

    def _insert_window_edges(
        self,
        build: _HistoryBuild,
        start: int,
        end: int,
        total: int,
    ) -> None:
        """窗口两端各放一行“加载更早/更晚”，比只靠滚动触发可靠。"""
        # 本地窗口起点之前 + 会话里还没加载的那部分，才是真正的“更早”
        above = start + self.view.history_offset
        if above > 0:
            build.widgets.insert(
                0,
                WindowEdge(f"↑ Load earlier · {above} in history", "earlier"),
            )
            # 顶部插了一行，摘要卡的位置判断也要跟着偏移。
            build.turn_widget_start += 1
        if end < total:
            build.widgets.append(
                WindowEdge(f"↓ Load newer · {total - end} later", "newer")
            )

    def load_earlier_history(self) -> None:
        """把上一页插到现有窗口上方，并让眼前正在读的内容保持不动。

        之前是整屏换一页：内容全换、视口回到页底，读者会丢掉正在看的位置。
        现在改成“前插”：新一页挂在现有内容之前，再按插入高度补偿滚动偏移，
        屏幕上的那段话位置不变；视口以下的旧内容随后裁掉，换成新的
        “Load newer” 入口，活跃控件数量依旧有上限。
        """
        history = self.view.history
        if not history:
            return
        chat = self.query_one("#chat", ChatLog)
        body = chat.query_one("#chat-body")
        current = (
            self._window_start
            if self._window_start is not None
            else tail_start(history)
        )
        if current <= 0:
            # 本地已翻到已加载的起点：向会话补一页更早的历史
            self._fetch_earlier_page()
            return
        new_start = earlier_start(history, current, PAGE_UNITS)
        if new_start >= current:
            return
        old_end = self._window_end if self._window_end is not None else len(history)
        height_before = body.virtual_size.height

        # 新页先在旧内容旁边构造好（Markdown 预排版），再整体前插。
        self._md_build_width = int(body.content_region.width or body.size.width or 0)
        checkpoint_by_user, remaining = _align_checkpoints(
            history, self.view.checkpoints
        )
        build = _HistoryBuild(
            theme=self.current_theme,
            concise=self._concise,
            remaining=remaining,
            changes_exchanges=_changes_exchanges(history),
        )
        self._build_history(build, checkpoint_by_user, start=new_start, end=current)
        self._md_build_width = 0

        # 旧的“↑ Load earlier”摘掉，换成指向新起点的一行，避免叠两行。
        for edge in list(chat.query(WindowEdge)):
            if edge.direction == "earlier":
                edge.remove()
        head = WindowEdge(f"↑ Load earlier · {new_start} in history", "earlier")
        first = body.children[0] if body.children else None
        chat._suspend_scroll = True
        body.mount_all([head, *build.widgets], before=first)
        chat._suspend_scroll = False
        self._window_start = new_start
        self._window_end = old_end

        # 用“插进去的那一页有多高”来补偿滚动：新控件的高度要等一帧布局完才
        # 算出来，所以读不到变化就下一帧再来，最多重试几帧。
        scroll_before = chat.scroll_y

        def settle(retries: int = 3) -> None:
            added = body.virtual_size.height - height_before
            if added <= 0 and retries > 0:
                chat.call_after_refresh(lambda: settle(retries - 1))
                return
            if added > 0:
                chat.scroll_y = scroll_before + added
            # 滚动稳定后再按最终位置裁掉屏幕外的内容。
            chat.call_after_refresh(
                lambda: self._trim_prepended(chat, body, new_start)
            )

        chat.call_after_refresh(settle)

    def _fetch_earlier_page(self) -> None:
        """本地窗口已到已加载起点：向会话补一页更早的历史。"""
        if not self.view.has_earlier or self._earlier_fetching:
            return
        self.run_worker(self._load_earlier_page(), group="history", exclusive=True)

    async def _load_earlier_page(self) -> None:
        """拉一页更早的历史接在本地前面，然后把窗口落到这一页的尾部。

        history 只在尾部追加时下标才稳定；前插会让所有本地下标后移，
        因此这里不做增量挂载，直接按新下标整体重建一次。
        """
        self._earlier_fetching = True
        try:
            page = await self._rpc("history_page", before=self.view.history_offset)
            added = self.view.prepend_history(
                page.get("items") or [], int(page.get("start", 0) or 0)
            )
            if not added:
                return
            history = self.view.history
            self._window_start = earlier_start(history, added, PAGE_UNITS)
            self._window_end = added
            self._render_history()
            chat = self.query_one("#chat", ChatLog)
            self.call_after_refresh(lambda: chat.scroll_home(animate=False))
        except LinkError as exc:
            self.notify(
                f"Cannot load earlier history: {exc}",
                title="History",
                severity="warning",
                timeout=6,
            )
        finally:
            self._earlier_fetching = False

    def _trim_prepended(self, chat: ChatLog, body, start: int) -> None:
        """裁掉视口以下、已经读过的旧尾部内容，换成新的“Load newer”入口。

        裁的是屏幕外的控件，读者眼前的内容不动。
        """
        history = self.view.history
        view_bottom = chat.region.y + chat.region.height

        # 1) 视口以下的尾部：换成新的“Load newer”入口。
        cut = forward_end(history, start, PAGE_UNITS + 10)
        dropped = False
        for widget in list(body.children):
            index = getattr(widget, "_history_index", None)
            if index is None or index < cut:
                continue
            # 还看得见的内容不能裁，否则眼前的字会消失；留给下一次翻页。
            if widget.region.y < view_bottom:
                continue
            widget.remove()
            dropped = True
        if dropped:
            self._window_end = cut
            for edge in chat.query(WindowEdge):
                if edge.direction == "newer":
                    edge.remove()
            remaining = len(history) - cut
            if remaining > 0:
                body.mount(WindowEdge(f"↓ Load newer · {remaining} later", "newer"))

        # 2) 视口以上的内容不裁：那是用户刚载入、还没读到的页；
        # 回最新或重新渲染时会整体收回窗口。


    def load_newer_history(self) -> None:
        """往后翻一页；追上尾部时回到跟随最新。"""
        if self._window_start is None:
            return
        history = self.view.history
        start = (
            self._window_end
            if self._window_end is not None
            else forward_end(history, self._window_start)
        )
        if start >= len(history) or start >= tail_start(history):
            self._window_start = None
            self._window_end = None
            self._render_history()
            self.query_one("#chat", ChatLog).follow_bottom()
            return
        self._window_start = start
        self._window_end = forward_end(history, start, PAGE_UNITS)
        self._render_history()
        chat = self.query_one("#chat", ChatLog)
        # _render_history 可能把旧 scroll_y 的恢复排到下一帧；这里再排一次，
        # 保证“往后翻”最终停在新区间顶部。
        self.call_after_refresh(lambda: chat.scroll_home(animate=False))

    def _ensure_latest_window(self) -> None:
        """权限卡/选择题必须看得见：正在翻历史时先回到最新。"""
        if self._window_start is None:
            return
        self._window_start = None
        self._window_end = None
        self._render_history()

    def _maybe_trim_window(self) -> None:
        """一轮结束后，活跃控件超过预算就重建回最近一屏。"""
        if self._window_start is not None:
            return
        if not self.view.history:
            return
        if window_units(self.view.history) <= TRIM_UNITS:
            return
        self._render_history()

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
        self._stream_frozen = []
        self._stream_frozen_upto = 0
        self._stream_hidden_marker = None
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
        start: int = 0,
        end: int | None = None,
    ) -> None:
        items = self.view.history
        end = len(items) if end is None else min(end, len(items))
        start = max(0, min(start, end))
        if not build.concise and start == 0:
            rules_widget = self._rules_summary_widget()
            if rules_widget is not None:
                build.widgets.append(rules_widget)
        index = start
        while index < end:
            kind, content = items[index]
            mark = len(build.widgets)
            if kind in ("rule", "skill"):
                # 来源已由顶部 Rules 行 / 摘要卡统一呈现，旧历史里的公告行不再重复
                build.last_kind = kind
                index += 1
                continue
            if kind == "shell_card":
                self._history_shell_card_entry(build, content)
            elif kind == "compaction":
                self._history_compaction_entry(build, content)
            elif kind == "changes":
                self._history_changes_entry(build, content)
            elif kind == "verifier_text":
                self._history_verifier_text_entry(build, content)
            elif kind in DEBUG_KINDS:
                self._history_debug_entry(build, kind, content)
            elif kind == USER:
                self._history_user_entry(build, index, content, checkpoint_by_user)
            else:
                self._history_reply_entry(build, kind, content)
            # 记下来源记录下标：向上翻页时按它精确裁掉视口外的尾部控件。
            for widget in build.widgets[mark:]:
                widget._history_index = index
            build.last_kind = kind
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
                path=str(data.get("output_path") or ""),
                line_count=int(data.get("line_count", 0) or 0),
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
            path=str(data.get("output_path") or ""),
            line_count=int(data.get("line_count", 0) or 0),
        )
        build.widgets.append(card)
        if request_id:
            build.shell_cards[request_id] = card

    def _history_compaction_entry(
        self, build: _HistoryBuild, content: str
    ) -> None:
        """压缩提示卡：和实时挂载的排版保持同一份。"""
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        build.widgets.append(
            self._compaction_widget(
                str(data.get("summary") or ""),
                str(data.get("detail") or ""),
                build.theme,
            )
        )

    @staticmethod
    def _changes_checkpoint(raw: dict) -> Checkpoint:
        """changes 记录只存索引；老会话里是完整 checkpoint，这里两种都兼容。"""
        return Checkpoint(
            before=str(raw.get("before") or ""),
            after=str(raw.get("after") or ""),
            context_start=int(raw.get("context_start") or 0),
            history_start=int(raw.get("history_start") or 0),
            prompt=str(raw.get("prompt") or ""),
            created_at=str(raw.get("created_at") or ""),
            files=[str(item) for item in raw.get("files") or []],
            exchange_id=int(raw.get("exchange_id") or 0),
            additions=int(raw.get("additions") or 0),
            deletions=int(raw.get("deletions") or 0),
        )

    def _history_changes_entry(
        self, build: _HistoryBuild, content: str
    ) -> None:
        """整轮变更卡：本回合展示过逐条 diff 时还原成摘要行，否则还原整轮 diff。"""
        if build.concise:
            # 简洁模式的 Changes 行由 checkpoint 汇总卡承载
            return
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return
        raw = data.get("checkpoint") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return
        try:
            checkpoint = self._changes_checkpoint(raw)
        except (TypeError, ValueError):
            return
        if checkpoint.exchange_id in build.tool_card_done:
            build.widgets.append(ChangesSummary(checkpoint))
            return
        card = InlineDiffCard(checkpoint, build.theme)
        build.widgets.append(card)
        build.inline_diffs.append((checkpoint.exchange_id, card))

    def _history_verifier_text_entry(
        self, build: _HistoryBuild, content: str
    ) -> None:
        """验收者正文：归在 Verifier 角色下；缺少角色行时补一个。"""
        if build.last_kind != "verifier":
            build.widgets.append(self._verifier_label(build.theme))
        build.widgets.extend(self._make_block_widgets("verifier_text", content))

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
            and checkpoint.exchange_id not in build.changes_exchanges
        ):
            card = self._history_tool_card(checkpoint)
            if card is not None:
                build.widgets.append(card)
                build.tool_cards.append((checkpoint.exchange_id, card))
                build.tool_card_done.add(checkpoint.exchange_id)
        build.widgets.extend(self._make_block_widgets(kind, content))
        if kind == ASSISTANT and checkpoint is not None:
            if (
                checkpoint.files
                and not build.concise
                and len(checkpoint.files) > 1
                and checkpoint.exchange_id not in build.changes_exchanges
            ):
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
        live: bool = True,
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
        if not live:
            # 旧窗口里没有当前回合：思考、运行中的工具行、流式正文都不挂。
            return
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
