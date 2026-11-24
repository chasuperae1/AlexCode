"""聊天流的流式/思考/工具行渲染。"""

from __future__ import annotations

import json
import time

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Collapsible, Static

from alex.ui.markdown import MarkdownStatic
from alex.ui.activity import SHELL_TOOLS
from alex.ui.constants import ASSISTANT
from alex.ui.panels import ChatLog, ConciseSummary
from alex.ui.sources import format_bytes
from alex.ui.widgets import _format_tokens

# 流式正文超过这个长度就只保留尾部，避免超长 Markdown 反复重排
_STREAM_TRUNCATE = 5000
_STREAM_TAIL = 3000


class ChatStreamMixin:
    """正文流、思考折叠、工具行与简洁模式收口。"""

    @staticmethod
    def _thinking_text(text: str, theme) -> Text:
        return Text(text, style=theme.secondary)

    # ---------------------------------------------------------- 简洁模式收口
    @property
    def _concise(self) -> bool:
        return getattr(self, "_display_mode", "developer") == "concise"

    def _begin_concise_summary(self) -> None:
        """每轮开头挂一张汇总卡，运行中只有这一行。"""
        card = ConciseSummary(self.current_theme)
        card.exchange_id = int(getattr(self.view, "exchange_seq", 0) or 0) or None
        state = getattr(self, "_sources", None)
        if state is not None:
            card.set_rules(len(state.rules), state.rules_size)
        self._chat().add_widget(card)
        self._concise_card = card

    def _ensure_concise_summary(self) -> ConciseSummary | None:
        """重建/权限/提问补卡时，保证简洁模式始终有本轮 step 卡。"""
        if not self._concise:
            return None
        card = self._concise_card_widget()
        if card is None:
            self._begin_concise_summary()
            card = self._concise_card_widget()
        return card

    def _concise_card_widget(self) -> ConciseSummary | None:
        card = getattr(self, "_concise_card", None)
        return card if isinstance(card, ConciseSummary) else None

    def _place_concise_summary(self, before=None) -> None:
        """把 step 卡放到正文之后、未决权限/选择题之前。"""
        if not self._concise:
            return
        card = self._concise_card_widget()
        if card is None:
            return
        try:
            body = self._chat().query_one("#chat-body")
        except Exception:
            return
        if card.parent is not body:
            return
        target = before
        if target is None or target.parent is not body:
            for pending in (
                getattr(self, "_permission_card", None),
                getattr(self, "_ask_widget", None),
            ):
                if pending is not None and pending.parent is body:
                    target = pending
                    break
        if target is not None and target is not card:
            body.move_child(card, before=target)
            return
        others = [child for child in body.children if child is not card]
        if others:
            body.move_child(card, after=others[-1])

    def _finish_concise_summary(self, duration: float) -> None:
        card = self._concise_card_widget()
        if card is not None:
            card.finish(duration)

    def _begin_thinking(self) -> None:
        """一段思考开始：单独一个折叠行，默认收起。"""
        self._finish_thinking("")  # 兜底上一段未结束的思考
        if self._concise:
            self._thinking_box = None
            self._thinking_static = None
            card = self._ensure_concise_summary()
            if card is not None:
                card.set_activity("thinking")
            return
        body = Static("", classes="debug-body")
        box = Collapsible(
            body, title="Thinking…", collapsed=True, classes="debug"
        )
        self._chat().add_widget(box)
        self._thinking_box = box
        self._thinking_static = body

    def _update_thinking(self) -> None:
        if self._concise:
            return
        if self._thinking_static is None or self._thinking_box is None:
            return
        theme = self.current_theme
        text = self.view.thinking
        if len(text) > _STREAM_TRUNCATE:
            text = "… (earlier content hidden)\n" + text[-_STREAM_TAIL:]
        self._thinking_static.update(self._thinking_text(text, theme))
        elapsed = time.monotonic() - self._thinking_started
        self._thinking_box.title = f"Thinking · {elapsed:.1f}s"

    def _finish_thinking(self, text: str) -> None:
        """结束当前思考段：有内容定稿为折叠行，无内容直接移除。"""
        if self._concise:
            if text:
                card = self._concise_card_widget()
                if card is not None:
                    card.add_thinking(len(text), count_step=True)
            self._thinking_box = None
            self._thinking_static = None
            return
        box, body = self._thinking_box, self._thinking_static
        self._thinking_box = None
        self._thinking_static = None
        theme = self.current_theme
        if box is None:
            if not text:
                return
            body = Static(self._thinking_text(text, theme), classes="debug-body")
            box = Collapsible(
                body,
                title=f"Thinking · {len(text):,} chars",
                collapsed=True,
                classes="debug",
            )
            self._chat().add_widget(box)
            return
        if not text:
            box.remove()
            return
        elapsed = (
            time.monotonic() - self._thinking_started
            if self._thinking_started
            else 0.0
        )
        if body is not None:
            body.update(self._thinking_text(text, theme))
        box.title = f"Thinking · {elapsed:.1f}s · {len(text):,} chars"
        box.collapsed = True

    @staticmethod
    def _tool_line(text: str, ok: bool, theme) -> Text:
        """完成后的工具行：✓/✗ + 内容，Subagent 名称用主色。"""
        line = Text()
        line.append(
            "✓ " if ok else "✗ ", style=theme.success if ok else theme.error
        )
        if text.startswith("Subagent"):
            head, sep, tail = text.partition(" · ")
            line.append(head, style=theme.primary)
            if sep:
                line.append(sep + tail, style=theme.foreground)
        else:
            line.append(text, style=theme.foreground)
        return line

    def _rules_summary_widget(self) -> Collapsible | None:
        """开发者模式顶部的 rules 折叠行，只显示数量与文件大小。"""
        state = getattr(self, "_sources", None)
        if state is None or not state.rules:
            return None
        theme = self.current_theme
        body = Text(
            " · ".join(item.name for item in state.rules),
            style=theme.secondary,
        )
        title = (
            f"Rules · {len(state.rules)} loaded · "
            f"{format_bytes(state.rules_size)}"
        )
        return Collapsible(
            Static(body, classes="debug-body"),
            title=title,
            collapsed=True,
            classes="debug rule-summary",
        )

    @staticmethod
    def _skill_from_result(data: dict) -> str:
        label = str(data.get("label") or "")
        if label.startswith("Skill "):
            skill = label[6:].split(" · ", 1)[0].strip()
            return "" if skill in {"", "done"} else skill
        text = str(data.get("text") or "")
        if text.startswith("Skill "):
            skill = text[6:].split(" done", 1)[0].split(" · ", 1)[0].strip()
            return "" if skill in {"", "done"} else skill
        return ""

    def _skill_line(
        self, name: str, size: int, elapsed: float, theme
    ) -> Text:
        line = Text()
        line.append("◆ ", style=theme.warning)
        line.append(
            f"Skill {name} · read {format_bytes(size)} · {elapsed:.1f}s",
            style=theme.warning,
        )
        return line

    def _start_tool_row(self, tool_id: str, data: dict) -> None:
        """工具开始：立刻追加一行，完成时原地更新为结果。"""
        if self._concise:
            card = self._concise_card_widget()
            if card is not None:
                name = str(data.get("name") or data.get("label") or "tool")
                card.step(f"calling {name}")
                if name in SHELL_TOOLS:
                    card.start_shell(name, tool_id)
            return
        name = str(data.get("name") or data.get("label") or "tool")
        if name in SHELL_TOOLS:
            # 开发者模式由 shell 卡承载命令与输出，不再追加重复的工具行
            return
        theme = self.current_theme
        kind = str(data.get("kind", "tool"))
        line = self._event_line(kind, str(data.get("label", "")), theme)
        line.append(" …", style=theme.secondary)
        row = Static(line, classes=self._tool_row_classes())
        self._chat().add_widget(row)
        if tool_id:
            self._tool_rows[tool_id] = row

    def _restore_live_tool_rows(self, before=None) -> None:
        """重建聊天区后把仍在运行的工具行补回去（仅 developer 模式）。"""
        if self._concise:
            return
        live = dict(getattr(self, "_live_tool_data", {}) or {})
        if not live:
            return
        theme = self.current_theme
        chat = self._chat()
        for tool_id, data in live.items():
            name = str(data.get("name") or data.get("label") or "tool")
            if name in SHELL_TOOLS:
                continue
            label = str(data.get("label") or name)
            line = self._event_line(
                str(data.get("kind", "tool")), label, theme
            )
            line.append(" …", style=theme.secondary)
            row = Static(line, classes=self._tool_row_classes())
            chat.mount(row, before=before)
            self._tool_rows[tool_id] = row

    def _tool_row_classes(self) -> str:
        return "tool-line tool-line-verifier" if self._verifier_mode else "tool-line"

    def _finish_tool_row(self, tool_id: str, data: dict) -> None:
        theme = self.current_theme
        ok = bool(data.get("ok", data.get("kind") == "result"))
        name = str(data.get("name") or "tool").strip() or "tool"
        skill = str(data.get("skill") or "").strip()
        if ok and not skill:
            skill = self._skill_from_result(data)
        elapsed = float(data.get("elapsed", 0.0) or 0.0)
        size = 0
        if skill:
            state = getattr(self, "_sources", None)
            if state is not None:
                item = state.mark_skill(skill, elapsed)
                size = item.size if item is not None else 0
            self._refresh_meta_line()
        if self._concise:
            card = self._concise_card_widget()
            if card is not None:
                if name in SHELL_TOOLS:
                    card.finish_shell(
                        name,
                        ok,
                        elapsed,
                        lines=int(data.get("line_count", 0) or 0),
                        exit_code=data.get("exit_code"),
                        call_id=tool_id,
                    )
                    if skill:
                        card.add_skill(skill, size, elapsed)
                elif skill:
                    card.add_skill(skill, size, elapsed)
                else:
                    card.add_tool(name, ok, str(data.get("text", "")))
            if tool_id:
                self._tool_rows.pop(tool_id, None)
            return
        if name in SHELL_TOOLS:
            if tool_id:
                self._tool_rows.pop(tool_id, None)
            return
        if skill:
            line = self._skill_line(skill, size, elapsed, theme)
        else:
            line = self._tool_line(str(data.get("text", "")), ok, theme)
        row = self._tool_rows.pop(tool_id, None) if tool_id else None
        if row is None:
            self._chat().add_widget(
                Static(line, classes=self._tool_row_classes())
            )
        else:
            row.update(line)

    def _close_stream_block(self) -> None:
        """思考或工具开始前封口当前正文块，之后的文字另起一块。

        否则同一轮里工具之后的回答会继续写入工具行上方的旧 Markdown，
        造成正文与工具行顺序颠倒。封口前先补刷尚未渲染的尾部文字，
        避免节流导致最后一小段被丢掉。
        """
        if self._stream_md is not None:
            raw = self.view.stream_text[self._stream_offset:]
            segment = raw
            if len(segment) > _STREAM_TRUNCATE:
                segment = (
                    "… (earlier content hidden)\n" + segment[-_STREAM_TAIL:]
                )
            # 工具/思考之前的中间正文也要进 history，否则主题切换或设置变更
            # 触发重建时，这段只存在于当前界面的文字会消失。验收者说的正文
            # 记成 verifier_text，重建时才不会挂到 Alex 名下。
            persisted = raw.strip()
            if persisted:
                self.view.record(
                    "verifier_text" if self._verifier_mode else ASSISTANT,
                    persisted,
                )
            self._render_stream_segment(segment)
            self._stream_md = None
            # 已落进 history 的中间正文不能继续留在流式缓冲里，否则重建时
            # 会同时挂出 history 版和当前流式版两份。
            self.view.stream_text = ""
            self._stream_offset = 0
            self._place_concise_summary()

    def _begin_stream(self) -> None:
        """回答容器延迟到首个文本 delta 再挂载，直接接在消息流末尾。"""
        if self._stream_md is not None:
            return
        chat = self._chat()
        theme = self.current_theme
        if not self._verifier_mode:
            role = Static(
                Text("Alex", style=f"bold {theme.primary}"), classes="role"
            )
            chat.add_widget(role)
        md = MarkdownStatic(
            Text(self.view.stream_text[self._stream_offset:] + " ▍"), classes="md"
        )
        chat.add_widget(md)
        self._stream_md = md
        # 正文一旦出现，step 卡就跟着移到正文下方；仍在权限/选择题之前。
        self._place_concise_summary()

    def _flush_stream(self) -> None:
        """节流刷新：流式阶段用纯文本渲染，结束时再一次性转成 Markdown。

        长回复（>5000 字符）只显示尾部，避免对巨量字符串做布局计算。
        """
        if not self._stream_dirty:
            return
        self._stream_dirty = False
        if self._stream_md is not None:
            display = self.view.stream_text[self._stream_offset:]
            if len(display) > _STREAM_TRUNCATE:
                display = "… (earlier content hidden)\n" + display[-_STREAM_TAIL:]
            self._stream_md.update(Text(display + " ▍"))
        if self.view.thinking:
            self._thinking_ticks += 1
            if self._thinking_ticks % 3 == 0:
                self._update_thinking()

    @staticmethod

    def _event_line(kind: str, content: str, theme) -> Text:
        """事件流的一行：工具 / MCP / 技能 / 规则 / 结果。"""
        styles = {
            "tool": ("▸ ", theme.secondary),
            "mcp": ("▸ ", theme.accent),
            "skill": ("◆ ", theme.warning),
            "rule": ("· ", theme.secondary),
            "result": ("  ✓ ", theme.success),
            "error": ("  ✗ ", theme.error),
        }
        prefix, color = styles.get(kind, ("· ", theme.secondary))
        return Text(f"{prefix}{content}", style=color)

    def _verifier_label(self, theme=None):
        """验收者角色行：实时挂载与历史重建共用同一份。"""
        theme = theme or self.current_theme
        return Static(
            Text("Verifier", style=f"bold {theme.secondary}"),
            classes="role-verifier",
        )

    def _compaction_widget(self, summary: str, detail: str = "", theme=None):
        """压缩提示卡：实时挂载与历史重建共用同一份排版。"""
        theme = theme or self.current_theme
        label = "≡ Context compacted"
        if detail:
            label += f" · {detail}"
        body = summary.strip()
        children = [
            Static(
                Text(label, style=f"bold {theme.secondary}"),
                classes="compaction-title",
            )
        ]
        if body:
            children.append(MarkdownStatic(body, classes="md compaction-body"))
        # 标题与摘要作为一个整体一次性挂载，避免分块出现像在流式追加。
        return Vertical(*children, classes="compaction-block")

    def _append_compaction(self, summary: str, detail: str = "") -> None:
        """在聊天区标记本轮发生过上下文压缩，并展示摘要。"""
        # 摘要是一次性结果：先收起加载行，再整块挂载，避免 spinner 与结果
        # 同时出现在屏幕上或看起来像在逐段追加。
        self._show_compacting(False)
        try:
            chat = self.query_one("#chat", ChatLog)
        except Exception:
            return
        chat.add_widget(self._compaction_widget(summary, detail))

    def _make_turn_stats(
        self,
        duration: float,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int = 0,
        compacted: bool = False,
    ) -> Static | None:
        """构造本轮耗时与 token 用量的展示控件（无用量时返回 None）。"""
        if not (input_tokens or output_tokens):
            return None
        parts = [f"{duration:.1f}s"]
        if input_tokens:
            label = f"↑{_format_tokens(input_tokens)}"
            if cache_tokens:
                label += f" ({_format_tokens(cache_tokens)} cached)"
            parts.append(label)
        if output_tokens:
            parts.append(f"↓{_format_tokens(output_tokens)}")
        if compacted:
            parts.append("context compacted")
        return Static(
            Text(" · ".join(parts), style=self.current_theme.secondary),
            classes="turn-stats",
        )

    def _append_turn_stats(
        self,
        duration: float,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int = 0,
        compacted: bool = False,
    ) -> None:
        """在回答下方显示本轮耗时与 token 用量。"""
        widget = self._make_turn_stats(
            duration, input_tokens, output_tokens, cache_tokens, compacted
        )
        if widget is None:
            return
        try:
            chat = self.query_one("#chat", ChatLog)
        except Exception:
            return
        chat.add_widget(widget)

    def _turn_stats_widgets(self, content: str) -> list:
        """从 stats 记录还原本轮统计行；简洁模式由汇总卡承载，不再单独挂。"""
        if self._concise:
            return []
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        widget = self._make_turn_stats(
            float(data.get("duration") or 0.0),
            int(data.get("input_tokens") or 0),
            int(data.get("output_tokens") or 0),
            int(data.get("cache_tokens") or 0),
            bool(data.get("compacted")),
        )
        return [widget] if widget is not None else []
