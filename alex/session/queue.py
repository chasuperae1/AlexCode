"""Agent 会话内核分包：任务队列：入队、逐条执行、确认/取消、卡片更新。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any



from alex.session.common import (
    SessionError,
)


PLAN_PROMPT = (
    "Plan a queue of small tasks for the request below.\n\n"
    "The queue runs tasks strictly one after another, so each task may rely "
    "on the output of the tasks before it, and each task must end with "
    "something checkable (a command result, a diff, a passing test, a "
    "written file).\n\n"
    "Granularity — be aggressive, this is the whole point of a queue:\n"
    "- Count the units of work before you write anything. A unit is the "
    "smallest thing that can be verified on its own: one test case, one test "
    "file, one fixture, one function or module, one endpoint, one migration, "
    "one doc section, one scenario to reproduce, one config key.\n"
    "- Aim for one task per unit. Merge units only when they are the same "
    "operation on the same file or area and would be verified together — then "
    'say which units it covers in the subject (e.g. "parser cases 41-45"). '
    "Never merge different files, subsystems or failure domains into one "
    "task.\n"
    "- Numbers to hit: 100 test cases -> 20-100 tasks (around 20 when "
    "adjacent cases share one fixture and are batched by five, up to 100 when "
    "each case stands alone); refactoring a 12-file module -> 12 or more "
    "tasks, one per file, plus a verification task; a spec with N bullet "
    "requirements -> at least N tasks.\n"
    "- If your plan has 7-8 tasks for work that clearly contains dozens of "
    "checkable units, it is too coarse — split it further before you answer. "
    "A long explicit queue beats a short vague one: 40 small tasks beat 6 "
    "broad ones. Keep the total under about 100 by batching adjacent "
    "identical units.\n"
    "- Stop only when the remaining items would be trivial busywork that "
    "needs no separate verification.\n"
    "- Each task must still be finishable within one reply.\n"
    "- Call TaskCreate once per task, in the order they should run. Do not "
    "skip planning because a step builds on an earlier one: sequential "
    "execution covers it.\n"
    "- Do not start any task now.\n\n"
    "Request:\n{text}"
)


def plan_prompt(text: str) -> str:
    """队列规划的完整指令：任务点数由「可独立验收的单元数」决定。"""
    return PLAN_PROMPT.format(text=text)


class QueueMixin:
    """任务队列：入队、逐条执行、确认/取消、卡片更新。"""

    # 规划一轮要连续调几十上百次 TaskCreate：每次工具调用算一次迭代，
    # 默认 max_iters=50 会把 60 个任务的计划从中间截断，这里临时放宽。
    PLAN_MAX_ITERS = 120

    async def queue(self, text: str) -> dict[str, Any]:
        """拆成有序任务并串行执行；任务之间可以前后依赖。立即返回，进度用事件推送。"""
        await self._wait_ready(timeout=30)
        if self.agent is None:
            raise SessionError("not_ready", "session is not ready")
        if self._turn_open():
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
        """队列主流程：规划确认 → 逐条执行 → 汇总收尾。"""
        try:
            prepared = await self._prepare_queue(text, before)
            if prepared is None:
                return
            tasks, total, started = prepared
            results = await self._execute_queue(tasks, total)
            await self._finish_queue(results, started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 规划 / 执行任一环节失败：提示并清理，不拖垮会话
            await self._fail_queue(exc)
        finally:
            self._reset_queue_state()

    async def _prepare_queue(
        self, text: str, before: set[str]
    ) -> tuple[list[Any], int, float] | None:
        """让模型把请求拆成有序任务，并等用户确认；不需要执行时返回 None。"""
        react = getattr(self.agent, "react_config", None)
        previous_iters = getattr(react, "max_iters", None)
        if isinstance(previous_iters, int) and previous_iters < self.PLAN_MAX_ITERS:
            try:
                react.max_iters = self.PLAN_MAX_ITERS
            except Exception:  # 框架对象不允许改时按原预算跑
                previous_iters = None
        else:
            previous_iters = None
        try:
            await self._start_turn(plan_prompt(text), f"/queue {text}")
        except asyncio.CancelledError:
            self._queue_stop = True
            return None
        finally:
            if previous_iters is not None:
                react.max_iters = previous_iters
        if self._queue_stop:
            return None
        new_tasks = [
            task
            for task in self.agent.state.tasks_context.tasks
            if task.id not in before
        ]
        if not new_tasks:
            self._notice(
                "Queue not started — the plan came back empty. "
                "Try /queue again, or send the request as a normal prompt.",
                title="Queue",
                severity="warning",
                timeout=8,
            )
            return None

        total = len(new_tasks)
        # 队列本身就是串行：模型顺手写上的 blocked_by 一律拆掉，
        # 否则任务栏会显示成“被依赖阻塞”，而队列其实照样按顺序跑。
        ids = {task.id for task in new_tasks}
        for task in new_tasks:
            task.blocked_by = [item for item in task.blocked_by if item not in ids]
            task.blocks = [item for item in task.blocks if item not in ids]
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
            return None

        self._queue_confirming = False
        self._update_queue_card("started")
        self._phase = "running"
        self._emit_status()
        return new_tasks, total, time.monotonic()

    async def _execute_queue(
        self, tasks: list[Any], total: int
    ) -> list[tuple[str, str, str]]:
        """逐条执行队列任务；返回 (subject, 状态, 失败原因) 列表。"""
        results: list[tuple[str, str, str]] = []
        for index, task in enumerate(tasks, 1):
            self._queue_index = index
            done_now = sum(1 for _, state, _ in results if state == "done")
            self._progress(f"Queue {index}/{total} · {done_now}/{total} done")
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
        return results

    async def _finish_queue(
        self, results: list[tuple[str, str, str]], started: float
    ) -> None:
        """汇总执行结果：推结果卡、收进度，并清理本次任务。"""
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

    async def _fail_queue(self, exc: Exception) -> None:
        """规划或执行异常：提示用户，并清掉这次拆出来的任务。"""
        self._notice(
            f"Queue failed: {exc}",
            title="Queue",
            severity="error",
            timeout=8,
        )
        self._progress("")
        await self._drop_queue_tasks()

    def _reset_queue_state(self) -> None:
        """无论正常结束还是被取消，都复位队列状态（落盘交给 close()）。"""
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
        except Exception:  # 尽力保存队列状态，失败不影响丢弃流程
            pass

