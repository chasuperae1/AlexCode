"""子代理可见性：注册表、内联行、右下角入口、浮层与子会话整页。"""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual.containers import Vertical

from alex.ui.activity import (
    SubagentNotice,
    SubagentRow,
    subagent_chip_text,
)
from alex.ui.panels import ChatLog

# 全部子代理完成后，右下角标识继续保持一段可查看时间，然后自动收起
SUBAGENT_DONE_VISIBLE_SECONDS = 20.0


class SubagentMixin:
    """维护子代理运行态，并把它们投影到主界面与子会话页。"""

    def _init_subagents(self) -> None:
        # id -> 记录（字段与 daemon 快照里的 subagents[] 一致）
        self._subagents: dict[str, dict[str, Any]] = {}
        self._subagent_rows: dict[str, SubagentRow] = {}
        self._subagent_notice: SubagentNotice | None = None
        self._subagent_screen = None
        self._subagent_board = None
        self._subagent_view_open = False
        # 最后一次子代理完成的时间：全部结束后用它控制标识的保留时长
        self._subagent_done_at: float | None = None

    # ---------------------------------------------------------- 事件入口
    def _on_subagent_start(self, data: dict) -> None:
        subagent_id = str(data.get("id") or "")
        if not subagent_id:
            return
        if not self._subagents_active():
            # 新一轮派发：上一批已经收尾的卡片不带进来
            self._clear_finished_subagents()
        record = self._subagents.get(subagent_id)
        if record is None:
            record = {"id": subagent_id, "transcript": []}
            self._subagents[subagent_id] = record
        record.update(
            {
                "label": str(data.get("label") or "Subagent"),
                "description": str(data.get("description") or ""),
                "model": str(data.get("model") or ""),
                "prompt": str(data.get("prompt") or ""),
                "started_at": float(data.get("started_at") or 0.0),
                "status": "running",
                "elapsed": 0.0,
                "steps": 0,
                "waiting": False,
                "waiting_tool": "",
                "result": "",
                "error": "",
                "text": "",
                "thinking": "",
            }
        )
        record.setdefault("transcript", [])
        self._subagent_done_at = None
        self._mount_subagent_row(record)
        self._refresh_subagent_chip()
        self._refresh_subagent_board()
        self._refresh_subagent_screen(subagent_id)

    def _on_subagent_delta(self, data: dict) -> None:
        subagent_id = str(data.get("id") or "")
        record = self._subagents.get(subagent_id)
        if record is None:
            return
        channel = str(data.get("channel") or "text")
        delta = str(data.get("delta") or "")
        if channel == "thinking":
            record["thinking"] = str(record.get("thinking") or "") + delta
            self._append_transcript(record, {"kind": "think", "delta": delta})
        else:
            record["text"] = str(record.get("text") or "") + delta
            self._append_transcript(record, {"kind": "text", "delta": delta})
        self._refresh_subagent_screen(subagent_id)

    def _on_subagent_tool(self, data: dict) -> None:
        subagent_id = str(data.get("id") or "")
        record = self._subagents.get(subagent_id)
        if record is None:
            return
        if str(data.get("phase") or "") == "start":
            record["steps"] = int(record.get("steps") or 0) + 1
        self._append_transcript(
            record,
            {
                "kind": "tool",
                "call_id": str(data.get("call_id") or ""),
                "name": str(data.get("name") or "Tool"),
                "phase": str(data.get("phase") or ""),
                "state": str(data.get("state") or ""),
                "elapsed": float(data.get("elapsed") or 0.0),
            },
        )
        self._refresh_subagent_board()
        self._refresh_subagent_screen(subagent_id)

    def _on_subagent_end(self, data: dict) -> None:
        subagent_id = str(data.get("id") or "")
        record = self._subagents.get(subagent_id)
        if record is None:
            return
        state = str(data.get("state") or "completed")
        record.update(
            {
                "status": state,
                "elapsed": float(
                    data.get("seconds") or record.get("elapsed") or 0.0
                ),
                "steps": int(data.get("steps") or record.get("steps") or 0),
                "waiting": False,
                "waiting_tool": "",
                "result": str(data.get("result") or ""),
                "error": str(data.get("error") or ""),
            }
        )
        self._append_transcript(
            record,
            {
                "kind": "end",
                "status": state,
                "seconds": float(data.get("seconds") or 0.0),
                "steps": int(data.get("steps") or 0),
                "error": str(data.get("error") or ""),
            },
        )
        self._update_subagent_row(record)
        self._drop_subagent_notice(subagent_id)
        if any(
            item.get("waiting") or item.get("status") == "running"
            for item in self._subagents.values()
        ):
            self._subagent_done_at = None
        else:
            self._subagent_done_at = time.monotonic()
        self._refresh_subagent_chip()
        self._refresh_subagent_board()
        self._refresh_subagent_screen(subagent_id)

    # ---------------------------------------------------------- 权限归属
    def _subagent_permission_requested(self, payload: dict) -> None:
        subagent_id = str(payload.get("subagent_id") or "")
        if not subagent_id:
            return
        record = self._subagents.get(subagent_id)
        if record is None:
            return
        record["waiting"] = True
        record["waiting_tool"] = str(payload.get("tool") or "approval")
        record["permission"] = dict(payload)
        self._subagent_done_at = None
        self._update_subagent_row(record)
        self._show_subagent_notice(record, payload)
        self._refresh_subagent_chip()
        self._refresh_subagent_board()
        self._refresh_subagent_screen(subagent_id)

    def _subagent_permission_resolved(self, request_id: str) -> None:
        for subagent_id, record in self._subagents.items():
            permission = record.get("permission") or {}
            if str(permission.get("request_id") or "") != request_id:
                continue
            record["waiting"] = False
            record["waiting_tool"] = ""
            record["permission"] = None
            self._update_subagent_row(record)
            self._drop_subagent_notice(subagent_id)
            self._refresh_subagent_chip()
            self._refresh_subagent_board()
            self._refresh_subagent_screen(subagent_id)
            return

    # ---------------------------------------------------------- 主界面投影
    def _subagent_records(self) -> list[dict[str, Any]]:
        return list(self._subagents.values())

    def _subagents_active(self) -> bool:
        return any(
            item.get("waiting") or item.get("status") == "running"
            for item in self._subagents.values()
        )

    def _clear_finished_subagents(self) -> None:
        """新一轮派发时清掉上一批已收尾的卡（行、提示与注册表一起）。"""
        finished = [
            subagent_id
            for subagent_id, record in self._subagents.items()
            if not (record.get("waiting") or record.get("status") == "running")
        ]
        for subagent_id in finished:
            self._subagents.pop(subagent_id, None)
            row = self._subagent_rows.pop(subagent_id, None)
            if row is not None:
                try:
                    row.remove()
                except Exception:  # 已随聊天区重建一起移除
                    pass
            notice = self._subagent_notice
            if notice is not None and notice.subagent_id == subagent_id:
                self._drop_subagent_notice(subagent_id)
        self._subagent_done_at = None

    def _mount_subagent_row(self, record: dict[str, Any]) -> None:
        subagent_id = str(record.get("id") or "")
        if not subagent_id:
            return
        row = self._subagent_rows.get(subagent_id)
        if row is not None and getattr(row, "is_mounted", False):
            row.update_record(record, self.current_theme)
            return
        try:
            chat = self._chat_ref or self.query_one("#chat", ChatLog)
        except Exception:
            return
        self._chat_ref = chat
        row = SubagentRow(record, self.current_theme)
        chat.add_widget(row)
        self._subagent_rows[subagent_id] = row

    def _update_subagent_row(self, record: dict[str, Any]) -> None:
        row = self._subagent_rows.get(str(record.get("id") or ""))
        if row is not None and getattr(row, "is_mounted", False):
            row.update_record(record, self.current_theme)

    def _show_subagent_notice(
        self, record: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        self._drop_subagent_notice()
        try:
            chat = self._chat_ref or self.query_one("#chat", ChatLog)
        except Exception:
            return
        self._chat_ref = chat
        notice = SubagentNotice(record, payload, self.current_theme)
        chat.add_widget(notice)
        self._subagent_notice = notice

    def _drop_subagent_notice(self, subagent_id: str = "") -> None:
        notice = self._subagent_notice
        if notice is None:
            return
        if subagent_id and notice.subagent_id != subagent_id:
            return
        try:
            notice.remove()
        except Exception:
            pass
        self._subagent_notice = None

    def _refresh_subagent_chip(self) -> None:
        refresh = getattr(self, "_refresh_task_status", None)
        if refresh is not None:
            refresh()

    def _subagent_chip(self) -> Text | None:
        records = self._subagent_records()
        if not records:
            return None
        active = any(
            item.get("waiting") or item.get("status") == "running"
            for item in records
        )
        if not active:
            # 全部完成后只保留一小段可查看时间，随后自动收起
            done_at = self._subagent_done_at
            if done_at is None:
                return None
            if time.monotonic() - done_at > SUBAGENT_DONE_VISIBLE_SECONDS:
                return None
        return subagent_chip_text(records, self.current_theme)

    def subagent_rows(self) -> list[dict[str, Any]]:
        """浮层里的顺序：等待授权 → 运行中 → 最近完成。"""
        records = self._subagent_records()
        records.sort(
            key=lambda item: (
                0
                if item.get("waiting")
                else 1
                if item.get("status") == "running"
                else 2,
                -float(item.get("started_at") or 0.0),
            )
        )
        return records

    # ---------------------------------------------------------- 交互区视图
    def toggle_subagents(self) -> None:
        """右下角标识：在同一块交互区里打开 / 收起子代理列表。"""
        if self._subagent_view_open:
            self.close_subagents()
            return
        if not self._subagents:
            return
        self.open_subagents()

    def open_subagents(self) -> None:
        """把列表挂进 ``#subagents``，并把聊天区让出来。"""
        if not self._subagents:
            return
        from alex.ui.subagent_board import SubagentBoard
        from alex.ui.widgets import PromptInput

        host = self.query_one("#subagents", Vertical)
        self._drop_subagent_board()
        board = SubagentBoard(self.subagent_rows(), self.current_theme)
        host.mount(board)
        self._subagent_board = board
        self._subagent_view_open = True
        try:
            self.query_one("#chat").display = False
            host.display = True
            # 列表里 ↑↓ 由应用层接管；输入框暂时禁用，Esc 回来即可继续打
            self.query_one("#jump").display = False
            prompt = self.query_one("#prompt", PromptInput)
            prompt.disabled = True
            prompt.placeholder = "Subagents open · esc back to the chat"
        except Exception:
            pass
        self.call_after_refresh(board.focus)

    def close_subagents(self) -> None:
        from alex.ui.constants import PROMPT_PLACEHOLDER
        from alex.ui.widgets import PromptInput

        self._subagent_view_open = False
        self._drop_subagent_board()
        try:
            host = self.query_one("#subagents", Vertical)
            host.display = False
            self.query_one("#chat").display = True
            self.query_one("#jump").display = True
            prompt = self.query_one("#prompt", PromptInput)
            prompt.disabled = False
            prompt.placeholder = PROMPT_PLACEHOLDER
            prompt.focus()
        except Exception:
            pass

    def _drop_subagent_board(self) -> None:
        board = self._subagent_board
        self._subagent_board = None
        if board is None:
            return
        try:
            board.remove()
        except Exception:  # 尚未挂载 / 已随宿主一起移除
            pass

    def _refresh_subagent_board(self) -> None:
        """列表打开期间，状态变化原地重绘（保留选中）。"""
        board = self._subagent_board
        if board is None or not self._subagent_view_open:
            return
        board.set_records(self.subagent_rows())

    def _retheme_subagent_board(self) -> None:
        """切主题时重建卡片，颜色跟着换。"""
        if self._subagent_view_open:
            self.open_subagents()

    def _subagent_key_move(self, step: int) -> bool:
        board = self._subagent_board
        if board is None or not self._subagent_view_open:
            return False
        board.move(step)
        return True

    def _subagent_key_open(self) -> bool:
        board = self._subagent_board
        if board is None or not self._subagent_view_open:
            return False
        if board.selected_id:
            self.open_subagent(board.selected_id)
        return True

    def _subagent_key_stop(self) -> bool:
        """ctrl+x：只停选中的那一个，其余照跑。"""
        board = self._subagent_board
        if board is None or not self._subagent_view_open:
            return False
        subagent_id = board.selected_id
        record = self._subagents.get(subagent_id) or {}
        if record.get("waiting"):
            self.notify(
                "Resolve the permission first, then stop it",
                severity="warning",
                timeout=3,
            )
            return True
        if record.get("status") != "running":
            self.notify("This subagent already finished", timeout=2)
            return True
        self.run_worker(self._stop_subagent(subagent_id), exclusive=False)
        return True

    async def _stop_subagent(self, subagent_id: str) -> None:
        if self.link is None:
            return
        try:
            result = await self._rpc("stop_subagent", subagent_id=subagent_id)
        except Exception as exc:
            self.notify(f"Stop failed: {exc}", severity="error", timeout=4)
            return
        if not (result or {}).get("stopped"):
            self.notify("It had already finished", timeout=2)

    def action_subagent_stop(self) -> None:
        self._subagent_key_stop()

    def open_subagent(self, subagent_id: str) -> None:
        record = self._subagents.get(str(subagent_id))
        if record is None:
            return
        from alex.screens.subagent import SubagentScreen

        screen = SubagentScreen(
            dict(record),
            self.current_theme,
            on_permission=self._subagent_child_choice,
        )
        self._subagent_screen = screen
        self.push_screen(screen)

    def _subagent_child_choice(self, choice: str) -> None:
        future = getattr(self, "_confirm_future", None)
        if future is not None and not future.done():
            future.set_result(choice)

    def _refresh_subagent_screen(self, subagent_id: str) -> None:
        screen = self._subagent_screen
        if screen is None:
            return
        if str(getattr(screen, "subagent_id", "")) != str(subagent_id):
            return
        record = self._subagents.get(str(subagent_id))
        if record is not None:
            screen.set_record(dict(record))

    def _clear_subagent_screen(self) -> None:
        self._subagent_screen = None

    def _reset_subagent_view(self) -> None:
        """整体重建聊天区：行控件由历史记录重新挂载。"""
        self._subagent_rows = {}
        self._subagent_notice = None

    def _restore_subagent_notice(self) -> None:
        """重建后如果仍有子代理在等权限，补回那一行提示。"""
        for record in self._subagents.values():
            permission = record.get("permission")
            if record.get("waiting") and isinstance(permission, dict):
                self._show_subagent_notice(record, permission)
                return

    def _load_subagents(self, items: Any) -> None:
        """断线重连快照：恢复注册表（含正文与工具记录）。"""
        if items is None:
            return
        restored: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            subagent_id = str(item.get("id") or "")
            if not subagent_id:
                continue
            record = dict(item)
            record.setdefault("transcript", [])
            restored[subagent_id] = record
        self._subagents = restored
        self._subagent_done_at = (
            time.monotonic()
            if restored
            and not any(
                item.get("waiting") or item.get("status") == "running"
                for item in restored.values()
            )
            else None
        )
        self._refresh_subagent_board()

    @staticmethod
    def _append_transcript(record: dict[str, Any], item: dict[str, Any]) -> None:
        transcript = record.setdefault("transcript", [])
        if (
            item.get("kind") in {"text", "think"}
            and transcript
            and transcript[-1].get("kind") == item.get("kind")
        ):
            transcript[-1]["delta"] = (
                str(transcript[-1].get("delta") or "")
                + str(item.get("delta") or "")
            )
        else:
            transcript.append(dict(item))
        if len(transcript) > 400:
            del transcript[:100]
