"""聊天流的 diff 卡片与历史回放。"""

from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text

from alex.config import workspace_path
from alex.core.snapshots import Checkpoint
from alex.screens.diff import DiffScreen, diff_lines, parse_diff
from alex.screens.message_actions import REVERT
from alex.ui.activity import SHELL_TOOLS
from alex.ui.panels import ChatLog
from alex.ui.widgets import (
    ChangesSummary,
    InlineDiffCard,
    ShellPermissionCard,
    ToolDiffCard,
)


class ChatDiffMixin:
    """单轮 diff、tool diff 和历史 checkpoint 回放。"""

    def _append_changes(self, checkpoint: Checkpoint) -> None:
        try:
            chat = self.query_one("#chat", ChatLog)
        except Exception:
            return
        if self._concise:
            # 简洁模式：改动并进汇总卡的 Changes 行，不再单独挂一行
            card = self._concise_card_widget()
            if card is not None:
                card.set_changes(
                    len(checkpoint.files),
                    checkpoint.additions,
                    checkpoint.deletions,
                )
                return
            chat.add_widget(ChangesSummary(checkpoint))
            return
        if self._turn_diff_cards:
            # 本轮已经逐条展示过改动，这里只留一行可点击的汇总，避免重复整块 diff
            chat.add_widget(ChangesSummary(checkpoint))
            return
        card = InlineDiffCard(checkpoint, self.current_theme)
        chat.add_widget(card)
        self.run_worker(
            self._load_inline_diff(checkpoint.exchange_id, card),
            exclusive=False,
        )

    def _append_tool_diff(self, data: dict) -> None:
        """把单次 Edit / Write 的 diff 立刻挂到聊天流末尾。"""
        permission = getattr(self, "_permission_card", None)
        raw_name = str(data.get("name") or "")
        name = raw_name.lower()
        if permission is not None and permission.tool_name.lower() == name:
            # 权限卡已经展示过这次改动的预览；完成后原地转成普通 diff 卡，
            # 不再追加第二张，否则同一次写入会出现两层相同 diff。
            # shell 卡不产生 diff，收成一行结果即可，也不计入本轮 diff 卡。
            permission.finish(data)
            self._permission_card = None
            if getattr(permission, "finishes_as_diff", True):
                self._turn_diff_cards += 1
            return
        diff_text = str(data.get("diff") or "")
        if not diff_text:
            if not self._concise and raw_name in SHELL_TOOLS:
                self._append_shell_result_card(data)
            return
        try:
            files = parse_diff(diff_text)
            if not files:
                return
            lines = diff_lines(diff_text, dark=self.current_theme.dark)
            if len(files) == 1 and lines:
                # 路径与 +/− 数已经在卡片标题上，正文不再重复这一行
                lines = lines[1:]
        except Exception:
            return
        additions = sum(item.additions for item in files)
        deletions = sum(item.deletions for item in files)
        path = files[0].path or "(unknown)"
        if len(files) > 1:
            extra = len(files) - 1
            path = f"{path}  +{extra} more file{'s' if extra != 1 else ''}"
        if self._concise:
            card = self._concise_card_widget()
            if card is not None:
                card.add_diff(path, additions, deletions, len(files))
            self._turn_diff_cards += len(files)
            return
        try:
            chat = self._chat()
        except Exception:
            return
        chat.add_widget(
            ToolDiffCard(
                path,
                (additions, deletions),
                lines,
                time.strftime("%H:%M:%S"),
                self.current_theme,
            )
        )
        self._turn_diff_cards += 1

    def _append_shell_result_card(self, data: dict) -> None:
        """开发者模式下，没有权限卡时也给 shell 留一张带输出的完成卡。"""
        try:
            chat = self._chat()
        except Exception:
            return
        ok = bool(data.get("ok", True))
        stdout = str(data.get("stdout") or "")
        payload = {
            "tool": str(data.get("name") or "Shell"),
            "command": str(data.get("command") or ""),
            "cwd": self._workspace or str(workspace_path()),
            "allow_always": False,
            "readonly": True,
            "result_text": str(data.get("text") or ""),
            "exit_code": int(data.get("exit_code", 0 if ok else 1) or 0),
            "stdout": stdout,
            "output_path": str(data.get("output_path") or ""),
            "line_count": int(data.get("line_count", 0) or 0),
        }
        card = ShellPermissionCard(
            payload,
            self.current_theme,
            lambda *_args: None,
        )
        card.set_state("done" if ok else "failed")
        card.set_output(
            stdout,
            exit_code=payload["exit_code"],
            failed=not ok,
            path=payload["output_path"],
            line_count=payload["line_count"],
        )
        chat.add_widget(card)

    async def _load_inline_diff(
        self,
        exchange_id: int,
        card: InlineDiffCard,
    ) -> None:
        try:
            data = await self._rpc("get_diff", exchange_id=exchange_id)
        except Exception as exc:
            card.set_error(str(exc))
            return
        card.set_diff(str(data.get("text", "")))

    def _history_tool_card(self, checkpoint: Checkpoint) -> ToolDiffCard | None:
        """历史回放的单文件 diff 卡：等 get_diff 回来后回填正文。"""
        if len(checkpoint.files) != 1:
            return None
        raw = checkpoint.files[0]
        try:
            path = Path(raw)
            if not path.is_absolute():
                path = Path(self._workspace or workspace_path()) / path
            shown = str(path)
        except Exception:
            shown = raw
        clock = (
            checkpoint.created_at[-8:]
            if len(checkpoint.created_at) >= 8
            else checkpoint.created_at
        ) or time.strftime("%H:%M")
        placeholder = [Text("Loading diff…", style=self.current_theme.secondary)]
        return ToolDiffCard(
            shown,
            (checkpoint.additions, checkpoint.deletions),
            placeholder,
            clock,
            self.current_theme,
        )

    async def _load_tool_card(
        self,
        exchange_id: int,
        card: ToolDiffCard,
    ) -> None:
        try:
            data = await self._rpc("get_diff", exchange_id=exchange_id)
        except Exception as exc:
            card.set_lines(
                [
                    Text(
                        f"Could not load diff: {exc}",
                        style=self.current_theme.error,
                    )
                ]
            )
            return
        text = str(data.get("text", ""))
        files = parse_diff(text)
        lines = diff_lines(text, dark=self.current_theme.dark) if text else []
        if len(files) == 1 and lines:
            # 路径与 +/− 数已经在卡片标题上，正文不再重复这一行
            lines = lines[1:]
        card.set_lines(lines)

    def _checkpoint_for(self, exchange_id: int) -> Checkpoint | None:
        for checkpoint in self.view.checkpoints:
            if checkpoint.exchange_id == exchange_id:
                return checkpoint
        return None

    @staticmethod

    def _same_prompt(showing: str, recorded: str) -> bool:
        """图片消息展示时附带 [image] 行，匹配时允许这层后缀。"""
        if not recorded:
            return showing.startswith("[image]")
        if showing == recorded:
            return True
        return showing.startswith(recorded + "\n") or recorded.startswith(
            showing + "\n"
        )

    def open_change_diff(self, widget) -> None:
        """点击变更摘要：展示这一轮的 diff，并提供回滚。"""
        exchange_id = getattr(widget, "exchange_id", None)
        if exchange_id is None or self.link is None:
            return
        self.run_worker(self._show_changes_diff(int(exchange_id)), exclusive=True)

    async def _show_changes_diff(self, exchange_id: int) -> None:
        try:
            data = await self._rpc("get_diff", exchange_id=exchange_id)
        except Exception as exc:
            self.notify(str(exc), severity="error", timeout=5)
            return
        action = await self.push_screen_wait(
            DiffScreen(
                str(data.get("prompt", "")),
                str(data.get("text", "")),
                summary=(
                    f"{data.get('files', 0)} file(s) changed · "
                    f"+{data.get('additions', 0)} -{data.get('deletions', 0)}"
                ),
            )
        )
        if action == REVERT:
            await self._undo(int(data.get("index", 0)))
