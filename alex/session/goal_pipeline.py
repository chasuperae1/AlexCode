"""Agent 会话内核分包：目标（Goal）轮次：确认、进度卡片、报告/判定、收尾。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any



from alex.session.common import (
    GOAL_RESTATEMENT_PROMPT,
    GoalRestatement,
    SessionError,
)



class GoalPipelineMixin:
    """目标澄清、执行、报告/校验与重试。"""

    def _clarify_value(answer: dict[str, Any]) -> str:
        """把一道选择题的回答压成 brief 里的一个值。"""
        if answer.get("skipped"):
            return "Not selected"
        other = str(answer.get("other") or "").strip()
        choices = [str(item) for item in (answer.get("choices") or []) if str(item)]
        if other and choices:
            return ", ".join(choices) + f" ({other})"
        return other or (", ".join(choices) if choices else "Not selected")

    async def goal_confirm(self) -> dict[str, Any]:
        """确认目标并开始执行。"""
        state = self._goal_state
        if not self._goal_confirming or state is None:
            raise SessionError("no_goal", "no goal is waiting for confirmation")

        state["phase"] = "executing"
        state["rows"] = [
            {
                "round": 1,
                "phase": "execute",
                "status": "run",
                "text": "",
                "seconds": 0.0,
            }
        ]
        state["started"] = time.monotonic()
        state["execute_started"] = state["started"]
        self._goal_confirming = False
        self._update_goal_card()
        self._progress(f"Goal Round 1/{state['max_iters']} · executing")
        self._launch_goal_pipeline(state, history_start=state["history_start"])
        return {"started": True}

    def _launch_goal_pipeline(
        self, state: dict[str, Any], *, history_start: int | None = None
    ) -> None:
        """启动（或重试）执行者 + 验收者的流水线。"""
        from agentscope.message import UserMsg
        from agentscope.pipeline import GoalPipeline

        from alex.core.agent import build_verifier

        rounds_left = max(1, int(state["max_iters"]) - int(state["round"]) + 1)
        pipeline = GoalPipeline(
            executor=self.agent,
            verifier=build_verifier(
                self._model, self._backend, on_result=self._on_goal_verdict
            ),
            verifier_reset_context=True,
            max_iters=rounds_left,
        )
        self._busy = True
        self._phase = "running"
        self._emit_status()
        start = len(self.history) if history_start is None else history_start
        self._reply_task = asyncio.create_task(
            self._reply(
                state["goal"],
                start,
                state["exchange_id"],
                [],
                label=state["display"],
                events=self._goal_events(
                    pipeline.reply_stream(
                        UserMsg(name="user", content=state["goal"])
                    )
                ),
            )
        )

    async def goal_update(self, text: str) -> dict[str, Any]:
        """修改尚未确认的目标需求。"""
        state = self._goal_state
        if not self._goal_confirming or state is None:
            raise SessionError("no_goal", "no goal is waiting for confirmation")
        if not text.strip():
            raise SessionError("internal", "empty goal")
        state["goal"] = text.strip()
        state["brief"] = []
        state["restatement"] = await self._restate_goal(text)
        self._update_goal_card()
        return {"updated": True}

    async def goal_cancel(self) -> dict[str, Any]:
        """取消尚未确认的目标（理解中 / 澄清中 / 等待确认都适用）。"""
        if self._goal_state is None:
            raise SessionError("no_goal", "no goal to cancel")
        state = self._goal_state
        retry = self._goal_retry_task
        self._goal_retry_task = None
        if retry is not None and not retry.done():
            retry.cancel()
        task = self._goal_task
        self._goal_task = None
        if task is not None and not task.done():
            # 正在等选择题时取消：标记 stopping 让待答文件被丢弃而不是保留
            self._stopping = True
            try:
                task.cancel()
                await task
            except asyncio.CancelledError:  # 取消 goal 任务，CancelledError 属预期
                pass
            except Exception:  # goal 任务退出异常不影响取消
                pass
            finally:
                self._stopping = False
        state["phase"] = "cancelled"
        state["understanding_live"] = False
        self._goal_confirming = False
        self._update_goal_card()
        self._goal_state = None
        self._goal_card_index = None
        self._progress("")
        self._phase = "idle"
        self._emit_status()
        return {"cancelled": True}

    def _progress(self, text: str, tone: str = "primary") -> None:
        """输入框左上角的一行进度：目标或队列共用。"""
        payload = {"text": text, "tone": tone if text else "primary"}
        if payload == self._progress_state:
            return
        self._progress_state = payload
        self.emit("progress", payload)

    def _goal_card_view(self) -> dict[str, Any]:
        state = self._goal_state or {}
        rows = [
            {
                "round": row["round"],
                "phase": row["phase"],
                "status": row["status"],
                "text": row["text"],
                "seconds": int(row["seconds"]),
            }
            for row in state.get("rows", [])
        ]
        return {
            "goal": state.get("goal", ""),
            "restatement": state.get("restatement", ""),
            "max_iters": state.get("max_iters", 1),
            "round": state.get("round", 1),
            "phase": state.get("phase", "executing"),
            "seconds": int(state.get("seconds", 0)),
            "failed_rounds": state.get("failed_rounds", 0),
            "rows": rows,
            "brief": list(state.get("brief") or []),
            "questions": int(state.get("questions", 0)),
            "answered": int(state.get("answered", 0)),
            "live": bool(state.get("understanding_live")),
        }

    def _goal_card_json(self) -> str:
        view = dict(self._goal_card_view())
        view.pop("live", None)  # 历史回放时不该继续转圈
        return json.dumps(view, ensure_ascii=False)

    def _update_goal_card(self) -> None:
        if self._goal_state is None or self._goal_card_index is None:
            return
        if self._goal_card_index < len(self.history):
            self.history[self._goal_card_index] = (
                "goal_card",
                self._goal_card_json(),
            )
        self.emit("goal_card", self._goal_card_view())

    @staticmethod
    def _goal_row(state: dict, phase: str) -> dict | None:
        for row in reversed(state["rows"]):
            if row["phase"] == phase and row["status"] == "run":
                return row
        return None

    def _on_goal_report(self, report: str) -> None:
        """执行者交出报告：记下本轮结果，进入验收。"""
        state = self._goal_state
        if state is None:
            return
        row = self._goal_row(state, "execute")
        if row is not None:
            row["status"] = "ok"
            row["text"] = self._one_line(report, 70) or "report ready"
            row["seconds"] = time.monotonic() - state["execute_started"]
        state["phase"] = "verifying"
        state["verify_started"] = time.monotonic()
        state["rows"].append(
            {
                "round": state["round"],
                "phase": "verify",
                "status": "run",
                "text": "checking the workspace…",
                "seconds": 0.0,
            }
        )
        self.history.append(("verifier", ""))
        self._update_goal_card()
        self._progress(
            f"Goal Round {state['round']}/{state['max_iters']} · verifying"
        )
        self.emit("goal_verify_start", {"round": state["round"]})

    def _on_goal_verdict(self, result: dict) -> None:
        """验收者的最终判定（框架内部消费，这里同步到卡片与进度）。"""
        state = self._goal_state
        if state is None:
            return
        verdict = str(result.get("result") or "fail")
        message = str(result.get("message") or "")
        row = self._goal_row(state, "verify")
        if row is not None:
            row["status"] = "ok" if verdict == "pass" else "bad"
            detail = self._one_line(message, 80)
            row["text"] = (
                f"{verdict} — {detail}" if detail and verdict != "pass"
                else verdict
            )
            row["seconds"] = time.monotonic() - state["verify_started"]
        state["last_result"] = verdict
        self.emit(
            "goal_verdict",
            {
                "round": state["round"],
                "result": verdict,
                "message": message,
            },
        )
        if verdict == "fail" and state["round"] < state["max_iters"]:
            state["failed_rounds"] += 1
            state["round"] += 1
            state["phase"] = "executing"
            state["execute_started"] = time.monotonic()
            state["rows"].append(
                {
                    "round": state["round"],
                    "phase": "execute",
                    "status": "run",
                    "text": "applying the verifier's feedback…",
                    "seconds": 0.0,
                }
            )
            self._update_goal_card()
            self._progress(
                f"Goal Round {state['round']}/{state['max_iters']} · executing",
                "warn",
            )
            self.emit("goal_round", {"round": state["round"]})
            return
        self._update_goal_card()
        if verdict == "pass":
            self._progress(
                f"Goal passed · Round {state['round']}/{state['max_iters']}", "ok"
            )
        elif verdict == "impossible":
            self._progress(
                f"Goal stopped · Round {state['round']}/{state['max_iters']}", "bad"
            )

    async def _goal_events(self, source):
        """包装 GoalPipeline 事件流：补上轮次、报告与结束状态。"""
        from agentscope.event import TextBlockDeltaEvent
        from agentscope.message import AssistantMsg, Msg

        executor_text: list[str] = []
        executor_round = 0
        try:
            async for event in source:
                state = self._goal_state
                if (
                    isinstance(event, TextBlockDeltaEvent)
                    and state is not None
                    and state.get("phase") == "executing"
                ):
                    round_no = int(state.get("round", 1))
                    if round_no != executor_round:
                        executor_text.clear()
                        executor_round = round_no
                    executor_text.append(event.delta)
                    # 执行者的回答先暂存，避免看起来像最终答复；
                    # 等验收工具与结果都显示完后再输出。
                    continue
                if isinstance(event, Msg) and event.structured_output is not None:
                    report = event.structured_output.get("report")
                    if report is not None:
                        self._on_goal_report(str(report))
                        # 结构化占位消息不展示，最终回答沿用执行者的正文
                        continue
                yield event
        except asyncio.CancelledError:
            self._finish_goal("stopped")
            raise
        except Exception as exc:
            state = self._goal_state
            if (
                state is not None
                and not self._stopping
                and int(state["round"]) < int(state["max_iters"])
            ):
                self._advance_after_error(state, exc)
                self._schedule_goal_retry()
            else:
                self._finish_goal("failed")
            raise
        else:
            state = self._goal_state or {}
            result = state.get("last_result")
            if result == "pass":
                status = "passed"
            elif result == "impossible":
                status = "impossible"
            elif result == "fail":
                status = "failed"
            else:
                status = "stopped"
            self._finish_goal(status)
            if executor_text:
                yield AssistantMsg(name="Alex", content="".join(executor_text))

    def _advance_after_error(self, state: dict[str, Any], exc: Exception) -> None:
        """一次请求/验收出错：记本轮的失败原因，推进到下一轮。"""
        row = self._goal_row(
            state, "verify" if state["phase"] == "verifying" else "execute"
        )
        if row is not None:
            row["status"] = "bad"
            row["text"] = f"error — {self._one_line(str(exc), 60)}"
            began = state.get("verify_started") or state.get("execute_started")
            if began:
                row["seconds"] = time.monotonic() - began
        state["failed_rounds"] += 1
        state["round"] += 1
        state["phase"] = "executing"
        state["execute_started"] = time.monotonic()
        state["rows"].append(
            {
                "round": state["round"],
                "phase": "execute",
                "status": "run",
                "text": "retrying after the failed request…",
                "seconds": 0.0,
            }
        )
        self._update_goal_card()
        self._progress(
            f"Goal Round {state['round']}/{state['max_iters']} · retrying", "warn"
        )
        self.emit("goal_round", {"round": state["round"]})

    def _schedule_goal_retry(self) -> None:
        current = self._goal_retry_task
        if current is not None and not current.done():
            return
        self._goal_retry_task = asyncio.create_task(self._goal_retry_later())

    async def _goal_retry_later(self) -> None:
        """等当前 _reply 收尾（写历史、落盘、释放 busy）后再起下一轮。"""
        for _ in range(100):
            await asyncio.sleep(0.2)
            if self._reply_task is None:
                break
        state = self._goal_state
        if state is None or self._stopping:
            return
        if int(state["round"]) > int(state["max_iters"]):
            return
        self._launch_goal_pipeline(state)

    def _finish_goal(self, status: str) -> None:
        state = self._goal_state
        retry = self._goal_retry_task
        self._goal_retry_task = None
        if retry is not None and not retry.done() and retry is not asyncio.current_task():
            retry.cancel()
        if state is None:
            return
        state["phase"] = status
        state["seconds"] = time.monotonic() - state["started"]
        for row in reversed(state["rows"]):
            if row["status"] == "run":
                row["status"] = "bad" if status != "passed" else "ok"
                if not row["text"]:
                    row["text"] = "stopped" if status != "passed" else "done"
                break
        if status == "passed":
            self._progress(
                f"Goal passed · Round {state['round']}/{state['max_iters']}", "ok"
            )
        elif status == "failed":
            self._progress(
                f"Goal failed · Round {state['round']}/{state['max_iters']}", "bad"
            )
        else:
            self._progress(
                f"Goal stopped · Round {state['round']}/{state['max_iters']}", "bad"
            )
        self._update_goal_card()
        self.emit(
            "goal_end",
            {
                "status": status,
                "round": state["round"],
                "max_iters": state["max_iters"],
                "seconds": int(state["seconds"]),
            },
        )
        self._goal_state = None
        self._goal_card_index = None

    async def _restate_goal(self, text: str) -> str:
        """一次性结构化调用，把目标复述成确认卡片上的一句话。"""
        from agentscope.message import UserMsg

        try:
            res = await self._model.generate_structured_output(
                messages=[
                    UserMsg(
                        name="user",
                        content=f"{GOAL_RESTATEMENT_PROMPT}\n\n用户需求：\n{text}",
                    )
                ],
                structured_model=GoalRestatement,
            )
            raw = str((getattr(res, "content", None) or {}).get("restatement") or "")
        except Exception:
            return self._one_line(text, 200)
        return self._join_points(raw) or self._one_line(text, 200)
