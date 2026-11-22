"""Agent 会话内核分包：任务队列：入队、逐条执行、确认/取消、卡片更新。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from alex.config import (
    CONFIG,
    LOCKED_SKILLS,
    ROOT,
    lock_skill_files,
    project_path,
    reload_config,
    save_config,
    workspace_path,
)
from alex.core.images import build_user_message, display_text
from alex.core import reasoning
from alex.core.snapshots import Checkpoint, SnapshotError, SnapshotStore, now_label

from alex.session.common import (
    ASSISTANT,
    BOOT_STAGES,
    DEFAULT_WELCOME,
    Emit,
    GOAL_RESTATEMENT_PROMPT,
    GoalRestatement,
    READONLY_TOOLS,
    SessionError,
    USER,
    _format_tokens,
)


class QueueMixin:
    """任务队列：入队、逐条执行、确认/取消、卡片更新。"""

    async def queue(self, text: str) -> dict[str, Any]:
        """拆成互不依赖的任务并串行执行；立即返回，进度用事件推送。"""
        await self._wait_ready(timeout=30)
        if self.agent is None:
            raise SessionError("not_ready", "session is not ready")
        if self._busy or self._queue_active or self._goal_confirming:
            raise SessionError("busy", "a reply is already running")
        if not text.strip():
            raise SessionError("internal", "empty queue")
        if self._revert is not None:
            self.commit_revert()

        before = {task.id for task in self.agent.state.tasks_context.tasks}
        self._queue_before_ids = before
        self._queue_active = True
        self._queue_stop = False
        self._queue_index = 0
        self._queue_total = 0
        self._queue_confirming = False
        self._queue_task_ids = []
        self._queue_subjects = []
        self._queue_card_index = None
        self._refresh_tasks()  # 规划期间先收起旧的置顶任务栏
        self._queue_task = asyncio.create_task(self._run_queue(text, before))
        return {"started": True}

    async def _run_queue(self, text: str, before: set[str]) -> None:
        results: list[tuple[str, str, str]] = []
        try:
            plan = (
                "Plan a queue of small, independent tasks for the request "
                "below.\n\n"
                "Rules:\n"
                "- If any task needs another task's output or completion "
                "first, do NOT call TaskCreate. Explain the dependency and "
                "ask the user to run the tasks one at a time.\n"
                "- Otherwise call TaskCreate once per task, in execution "
                "order.\n"
                "- Do not start any task now.\n\n"
                f"Request:\n{text}"
            )
            try:
                await self._start_turn(plan, f"/queue {text}")
            except asyncio.CancelledError:
                self._queue_stop = True
                return
            if self._queue_stop:
                return
            new_tasks = [
                task
                for task in self.agent.state.tasks_context.tasks
                if task.id not in before
            ]
            if not new_tasks:
                self._notice(
                    "Queue not started — the tasks depend on each other. "
                    "Run them one at a time.",
                    title="Queue",
                    severity="warning",
                    timeout=8,
                )
                return

            total = len(new_tasks)
            self._queue_total = total
            self._queue_subjects = [task.subject for task in new_tasks]
            self._queue_task_ids = [task.id for task in new_tasks]
            self._queue_confirming = True
            payload = self._queue_card_payload("confirming")
            self.history.append(
                ("queue_card", json.dumps(payload, ensure_ascii=False))
            )
            self._queue_card_index = len(self.history) - 1
            self.emit("queue_card", payload)
            self._progress(f"Queue 0/{total} · awaiting start", "warn")
            self._phase = "confirming"
            self._emit_status()
            self._refresh_tasks()

            decision = await self._wait_queue_decision()
            if decision != "start" or self._queue_stop:
                self._update_queue_card("cancelled")
                self._progress(f"Queue 0/{total} · not started", "muted")
                # 丢弃后立即落盘，避免 daemon 重启后任务又出现在列表里
                await self._drop_queue_tasks()
                return

            self._queue_confirming = False
            self._update_queue_card("started")
            self._phase = "running"
            self._emit_status()
            started = time.monotonic()

            for index, task in enumerate(new_tasks, 1):
                self._queue_index = index
                done_now = sum(1 for _, state, _ in results if state == "done")
                self._progress(
                    f"Queue {index}/{total} · {done_now}/{total} done"
                )
                if self._queue_stop:
                    results.append((task.subject, "skipped", ""))
                    continue
                if task.state == "completed":
                    results.append((task.subject, "done", ""))
                    continue

                header = f"Queue {index}/{total} · {task.subject}"
                self.history.append(("queue", header))
                self.emit(
                    "queue",
                    {"index": index, "total": total, "subject": task.subject},
                )
                self._refresh_tasks()
                prompt = (
                    "Work only on this queued task. Do not touch other "
                    "queued tasks.\n\n"
                    f"Task:\n{task.subject}\n{task.description or ''}\n\n"
                    "When it is fully done, mark it completed with "
                    f'TaskUpdate(task_id="{task.id}", status="completed"). '
                    "If it cannot be completed, leave the status unchanged "
                    "and reply with one short sentence explaining why."
                )
                try:
                    answer = await self._start_turn(
                        prompt, header, label=task.subject, bubble=False
                    )
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise  # 会话关闭导致整个队列被取消
                    self._queue_stop = True
                    answer = ""

                if task.state == "completed":
                    results.append((task.subject, "done", ""))
                elif self._queue_stop:
                    if task.state == "in_progress":
                        task.state = "pending"
                    results.append((task.subject, "skipped", ""))
                else:
                    results.append(
                        (task.subject, "failed", self._one_line(answer))
                    )
                    # 失败的任务退回 pending，避免下次空闲时误显示为运行中
                    if task.state == "in_progress":
                        task.state = "pending"
                    self._refresh_tasks()

            done = sum(1 for _, status, _ in results if status == "done")
            failed = sum(1 for _, status, _ in results if status == "failed")
            skipped = sum(1 for _, status, _ in results if status == "skipped")
            payload = {
                "results": [list(item) for item in results],
                "done": done,
                "failed": failed,
                "skipped": skipped,
                "seconds": max(0, int(time.monotonic() - started)),
            }
            self.history.append(
                ("queue_result", json.dumps(payload, ensure_ascii=False))
            )
            self.emit("queue_result", payload)
            self._update_queue_card("done")
            self._progress("")
            # 队列结束即清理本次任务：结果卡已记录成败，旧任务不再堆到下一次队列
            await self._drop_queue_tasks()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._notice(
                f"Queue failed: {exc}",
                title="Queue",
                severity="error",
                timeout=8,
            )
            self._progress("")
            await self._drop_queue_tasks()
        finally:
            # 会话关闭等外部取消路径也要清理，落盘交给 close()
            self._discard_queue_tasks()
            self._queue_active = False
            self._queue_stop = False
            self._queue_task = None
            self._queue_index = 0
            self._queue_total = 0
            self._queue_confirming = False
            self._queue_decision = None
            self._queue_task_ids = []
            self._queue_subjects = []
            self._queue_card_index = None
            self._queue_before_ids = set()
            self._phase = "running"
            self._set_busy(False)
            self._refresh_tasks()

    async def queue_confirm(self) -> dict[str, Any]:
        """用户确认开始：释放队列任务，继续执行。"""
        future = self._queue_decision
        if not self._queue_confirming or future is None or future.done():
            raise SessionError("no_queue", "no queue is waiting for confirmation")
        self._queue_confirming = False
        future.set_result("start")
        return {"started": True}

    async def queue_cancel(self) -> dict[str, Any]:
        """用户二次 Esc：丢弃本次拆分出来的任务。"""
        future = self._queue_decision
        if not self._queue_confirming or future is None or future.done():
            raise SessionError("no_queue", "no queue is waiting for confirmation")
        self._queue_confirming = False
        future.set_result("cancel")
        return {"cancelled": True}

    def _queue_card_payload(self, state: str) -> dict[str, Any]:
        return {"tasks": list(self._queue_subjects), "state": state}

    def _update_queue_card(self, state: str) -> None:
        """改写历史里的计划卡并推送增量，重连时也能看到最终状态。"""
        if self._queue_card_index is None:
            return
        if self._queue_card_index >= len(self.history):
            return
        payload = self._queue_card_payload(state)
        self.history[self._queue_card_index] = (
            "queue_card",
            json.dumps(payload, ensure_ascii=False),
        )
        self.emit("queue_card", payload)

    async def _wait_queue_decision(self) -> str:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._queue_decision = future
        try:
            return str(await future)
        except asyncio.CancelledError:
            self._queue_stop = True
            return "cancel"
        finally:
            if self._queue_decision is future:
                self._queue_decision = None

    def _discard_queue_tasks(self) -> bool:
        """移除本次队列新建的任务，并清理依赖引用。"""
        if self.agent is None:
            return False
        context = self.agent.state.tasks_context
        ids = set(self._queue_task_ids)
        if not ids and self._queue_before_ids:
            # 规划途中被取消时 _queue_task_ids 还没写入，按开始前的快照兜底
            ids = {
                task.id
                for task in context.tasks
                if task.id not in self._queue_before_ids
            }
        if not ids:
            return False
        remaining = [task for task in context.tasks if task.id not in ids]
        removed = len(remaining) != len(context.tasks)
        context.tasks = remaining
        for task in context.tasks:
            task.blocks = [item for item in task.blocks if item not in ids]
            task.blocked_by = [
                item for item in task.blocked_by if item not in ids
            ]
        return removed

    async def _drop_queue_tasks(self) -> None:
        """队列结束后清理本次任务并落盘，避免旧任务堆到下一次队列。"""
        if not self._discard_queue_tasks():
            return
        from alex.core.agent import save_state

        try:
            await asyncio.to_thread(save_state, self.agent.state)
        except Exception:
            pass

