"""AskUser 的内联选择题控件。

按设计稿 v2：无卡片边框，只保留 ``?`` 提问行、缩进选项和一行快捷键提示。
键盘操作由控件自身处理（焦点在控件上时）：
- ↑↓ / j k：移动高亮
- 1-9：单选直接提交，多选切换勾选
- Space / a：多选勾选 / 全选
- Enter：确认或提交
- Tab：进入自定义输入
- Esc：自定义输入时返回选项，否则跳过（由 App 的 escape 动作转发）
"""

from __future__ import annotations

from typing import Any, Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from alex.screens.ask_option import AskInput, AskOption


class AskChoice(Vertical):
    """内联选择题。回答后原地收起为一行结果。"""

    can_focus = True

    DEFAULT_CSS = """
    AskChoice {
        width: 1fr;
        height: auto;
        margin: 1 0 1 0;
    }
    AskChoice .ask-question {
        width: 1fr;
        height: auto;
    }
    AskChoice .ask-options {
        width: 1fr;
        height: auto;
        max-height: 10;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
        margin: 0 0 0 2;
    }
    AskChoice .ask-hint {
        width: 1fr;
        height: 1;
        margin: 0 0 0 2;
        color: $text-muted;
    }
    AskChoice .ask-hint-left {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    AskChoice .ask-hint-right {
        width: auto;
        height: 1;
        padding: 0 1 0 0;
        color: $text-muted;
    }
    AskChoice .ask-input {
        width: 1fr;
        height: 1;
        margin: 1 0 0 2;
        padding: 0 1;
        /* Input:focus 的 tall 边框会让单行输入框没有内容行，文字被裁掉 */
        border: none !important;
        background: $surface;
        color: $foreground;
        display: none;
    }
    AskChoice .ask-custom-hint {
        width: 1fr;
        height: auto;
        margin: 0 0 0 2;
        color: $text-muted;
        display: none;
    }
    /* 自定义输入时保留选项，只收起普通提示行 */
    AskChoice.-custom .ask-hint {
        display: none;
    }
    AskChoice.-custom .ask-input,
    AskChoice.-custom .ask-custom-hint {
        display: block;
    }
    AskChoice .ask-answer {
        width: 1fr;
        height: auto;
        display: none;
    }
    AskChoice .ask-tick {
        width: 2;
        height: 1;
        color: $success;
    }
    AskChoice .ask-answer-text {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }
    AskChoice.-answered .ask-question,
    AskChoice.-answered .ask-options,
    AskChoice.-answered .ask-hint,
    AskChoice.-answered .ask-input,
    AskChoice.-answered .ask-custom-hint {
        display: none;
    }
    AskChoice.-answered .ask-answer {
        display: block;
    }
    AskChoice.-answered {
        margin-left: 2;
    }
    AskChoice.-skipped .ask-tick {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        payload: dict[str, Any],
        submit: Callable[[str, list[str], str, bool], None],
    ) -> None:
        super().__init__()
        self.request_id = str(payload.get("request_id", ""))
        self.question = str(payload.get("question", ""))
        self.multi = bool(payload.get("multi", False))
        self.options = [
            {
                "label": str(item.get("label", "")),
                "description": str(item.get("description", "")),
            }
            for item in payload.get("options", []) or []
            if isinstance(item, dict) and str(item.get("label", "")).strip()
        ]
        self._submit_cb = submit
        self._index = 0
        self._selected: set[int] = set()
        self._mode = "options"
        self._submitted = False
        self._done = False
        self._rows: list[AskOption] = []
        self._other_row: AskOption | None = None

    # ------------------------------------------------------------------ 渲染
    def compose(self) -> ComposeResult:
        question = Text()
        accent = self.app.current_theme.accent
        question.append("? ", style=f"bold {accent}")
        question.append(self.question)
        yield Static(question, classes="ask-question", markup=False)
        with Vertical(classes="ask-options"):
            for index, item in enumerate(self.options):
                row = AskOption(
                    index,
                    item["label"],
                    item["description"],
                    multi=self.multi,
                )
                self._rows.append(row)
                yield row
            self._other_row = AskOption(
                len(self.options),
                "Other (custom input)",
                "Tab to type",
                multi=self.multi,
                other=True,
            )
            yield self._other_row
        with Horizontal(classes="ask-hint"):
            yield Static(self._hint_text(), classes="ask-hint-left", markup=False)
            yield Static("Esc Skip", classes="ask-hint-right", markup=False)
        yield AskInput(placeholder="Enter a custom answer…", classes="ask-input")
        yield Static(
            "Enter Send · Esc Back to options · Tab Message", classes="ask-custom-hint", markup=False
        )
        yield Horizontal(
            Static("✓", classes="ask-tick"),
            Static("", classes="ask-answer-text"),
            classes="ask-answer",
        )

    def on_mount(self) -> None:
        self._refresh()
        # 用户可能正在主输入框打字，或刚从 Settings 返回；不要一律抢焦点。
        # 只有当前整个界面没有焦点时才由答题卡接管键盘。
        if self.app.focused is None:
            self.focus()

    def _hint_text(self) -> str:
        count = len(self.options)
        if self.multi:
            hint = (
                f"↑↓ Move · 1–{count} Select · Space Toggle · A All · Enter Submit"
                " · Tab Message"
            )
            if self._selected:
                hint += f" · {len(self._selected)} selected"
            return hint
        return (
            f"↑↓ Move · 1–{count} Select · Enter Confirm · Tab Message"
        )

    def _refresh(self) -> None:
        for index, row in enumerate(self._rows):
            row.set_active(index == self._index)
            row.set_checked(index in self._selected)
        if self._other_row is not None:
            is_other = self._index == len(self.options)
            self._other_row.set_active(is_other)
            self._other_row.set_checked(
                self._mode == "custom"
            )
        if self.multi:
            self.query_one(".ask-hint-left", Static).update(self._hint_text())

    # ------------------------------------------------------------------ 键盘
    def on_key(self, event: events.Key) -> None:
        if self._done or self._submitted:
            return
        key = event.key
        if key in {"alt+v", "f6"} and self._mode == "custom":
            # Ctrl+V 会被终端当成自己的粘贴，图片进不来；Alt+V / F6 交给应用处理
            handler = getattr(self.app, "paste_clipboard_image", None)
            if handler is not None:
                event.stop()
                event.prevent_default()
                self.run_worker(
                    handler(self.query_one(".ask-input", Input)), exclusive=True
                )
            return
        if self._mode == "custom":
            if key == "tab":
                # 自定义回答里 Tab 也不能被困住：退出自定义并回主输入框。
                event.stop()
                event.prevent_default()
                self._exit_custom()
                handler = getattr(self.app, "_restore_prompt_focus", None)
                if handler is not None:
                    handler()
            return
        handled = True
        if key in {"up", "k"}:
            self._move(-1)
        elif key in {"down", "j"}:
            self._move(1)
        elif key.isdigit() and len(key) == 1:
            number = int(key)
            if 1 <= number <= len(self.options):
                self._index = number - 1
                self._activate_row()
            else:
                handled = False
        elif key == "space":
            if self.multi:
                self._toggle(self._index)
            else:
                handled = False
        elif key == "a" and self.multi:
            if len(self._selected) == len(self.options):
                self._selected.clear()
            else:
                self._selected = set(range(len(self.options)))
            self._refresh()
        elif key == "enter":
            self._confirm()
        elif key == "tab":
            if self._index == len(self.options):
                self._enter_custom()
            else:
                # Tab 是用户切回主输入框的常用动作，不能无条件吸进自定义回答。
                # 只有高亮在 Other 行时才进入自定义输入；独立控件没有应用层
                # 输入框可回退时，仍保留原来的 Tab 进入自定义行为。
                handler = getattr(self.app, "_restore_prompt_focus", None)
                if handler is not None:
                    handler()
                else:
                    self._enter_custom()
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()

    def _move(self, delta: int) -> None:
        rows = len(self.options) + 1
        self._index = (self._index + delta) % rows
        self._refresh()
        self._scroll_into_view()

    def _activate_row(self) -> None:
        if self._index == len(self.options):
            self._enter_custom()
            return
        if self.multi:
            self._toggle(self._index)
        else:
            self._refresh()
            self._confirm()

    def _toggle(self, index: int) -> None:
        if index >= len(self.options):
            return
        if index in self._selected:
            self._selected.discard(index)
        else:
            self._selected.add(index)
        self._refresh()

    def _confirm(self) -> None:
        if self._index == len(self.options):
            if self._mode == "custom":
                self._submit_custom()
            else:
                self._enter_custom()
            return
        # 点选普通选项时必须退出自定义模式，否则会误提交空的自定义答案。
        if self._mode == "custom":
            self._mode = "options"
            self.remove_class("-custom")
            self._refresh()
        if self.multi:
            if not self._selected:
                self._selected.add(self._index)
            labels = [
                self.options[index]["label"] for index in sorted(self._selected)
            ]
            self._submit(labels, "", False)
            return
        self._submit([self.options[self._index]["label"]], "", False)

    def _submit(self, choices: list[str], other: str, skipped: bool) -> None:
        if self._submitted or self._done:
            return
        self._submitted = True
        self._submit_cb(self.request_id, choices, other, skipped)

    def _scroll_into_view(self) -> None:
        row = (
            self._other_row
            if self._index == len(self.options)
            else self._rows[self._index]
        )
        if row is not None:
            row.scroll_visible(animate=False)

    # ------------------------------------------------------------------ 鼠标
    def on_click(self, event: events.Click) -> None:
        if self._done or self._submitted:
            return
        node = event.widget
        while node is not None and not isinstance(node, AskOption):
            node = node.parent
        if not isinstance(node, AskOption):
            return
        event.stop()
        self._index = node.index
        if node.index == len(self.options):
            self._enter_custom()
            return
        if self.multi:
            self._toggle(node.index)
        else:
            self._refresh()
            self._confirm()

    # ------------------------------------------------------------------ 自定义
    def _enter_custom(self) -> None:
        if self._done or self._submitted:
            return
        self._mode = "custom"
        self._index = len(self.options)
        self.add_class("-custom")
        self._refresh()
        field = self.query_one(".ask-input", Input)
        field.focus()

    def _exit_custom(self) -> None:
        self._mode = "options"
        self.remove_class("-custom")
        self._refresh()
        self.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit_custom()

    def _submit_custom(self) -> None:
        if self._done or self._submitted:
            return
        text = self.query_one(".ask-input", Input).value.strip()
        if not text:
            return
        self._submit([], text, False)

    # ------------------------------------------------------------------ 外部
    def handle_escape(self) -> None:
        """App 的 Esc 动作转发到这里。"""
        if self._done:
            return
        if self._mode == "custom":
            self._exit_custom()
            return
        self._submit([], "", True)

    def reset_submission(self) -> None:
        """提交失败时允许用户重新作答。"""
        if not self._done:
            self._submitted = False

    def mark_answer(self, summary: str, skipped: bool = False) -> None:
        """回答完成：原地收起为一行结果，并交出焦点。"""
        if self._done:
            return
        self._done = True
        self._submitted = True
        self.can_focus = False
        self.add_class("-answered")
        self.set_class(skipped, "-skipped")
        self.query_one(".ask-tick", Static).update("⤼" if skipped else "✓")
        self.query_one(".ask-answer-text", Static).update(summary)
