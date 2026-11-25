"""Agent 会话内核分包：目标（Goal）轮次：确认、进度卡片、报告/判定、收尾。"""

from __future__ import annotations

import asyncio
from typing import Any


from alex.config import (
    CONFIG,
)

from alex.session.clarify_rules import needs_clarification
from alex.session.common import (
    GOAL_INTERVIEW_PROMPT,
    GoalInterview,
    SessionError,
    USER,
)

from alex.session.goal_pipeline import GoalPipelineMixin

class GoalMixin(GoalPipelineMixin):
    """目标（Goal）轮次：确认、进度卡片、报告/判定、收尾。"""

    async def goal(self, text: str) -> dict[str, Any]:
        """先复述并等待确认；确认后才启动执行者与验收者循环。"""
        await self._wait_ready(timeout=30)
        if self.agent is None or self._model is None:
            raise SessionError("not_ready", "session is not ready")
        if self._turn_open() or self._goal_state is not None:
            raise SessionError("busy", "a reply is already running")
        if not text.strip():
            raise SessionError("internal", "empty goal")
        if self._revert is not None:
            self.commit_revert()

        max_iters = int(CONFIG["model"].get("goal_max_iters", 3))
        self._goal_state = state = {
            "goal": text,
            "max_iters": max_iters,
            "round": 1,
            "phase": "understanding",
            "rows": [],
            "started": 0.0,
            "seconds": 0.0,
            "execute_started": 0.0,
            "verify_started": 0.0,
            "last_result": "",
            "failed_rounds": 0,
            "restatement": "",
            "brief": [],
            "questions": 0,
            "answered": 0,
            "understanding_live": True,
        }
        display = f"/goal {text}"
        history_start = len(self.history)
        self._exchange_seq += 1
        exchange_id = self._exchange_seq
        state["history_start"] = history_start
        state["exchange_id"] = exchange_id
        state["display"] = display
        self.history.append((USER, display))
        self.emit(
            "exchange_start",
            {"exchange_id": exchange_id, "text": display},
        )
        # 先挂占位卡：后面理解、出题、复述都在原卡片上原地更新
        self._goal_card_index = len(self.history)
        self.history.append(("goal_card", self._goal_card_json()))
        self.emit("goal_card", self._goal_card_view())
        self._goal_confirming = False
        self._phase = "understanding_goal"
        self._progress("Goal · Understanding request")
        self._emit_status()
        self._goal_task = asyncio.create_task(self._goal_understand(text))
        return {"understanding": True}

    async def _goal_understand(self, text: str) -> None:
        """后台理解：必要时逐题澄清，最后落到确认卡。"""
        state = self._goal_state
        if state is None:
            return
        try:
            questions, restatement = await self._goal_interview(text)
            brief: list[dict[str, str]] = []
            total = len(questions)
            for index, item in enumerate(questions, 1):
                if self._goal_state is None:
                    return
                state["phase"] = "clarifying"
                state["questions"] = total
                state["answered"] = index - 1
                state["understanding_live"] = False
                self._progress(f"Goal · Additional details {index}/{total}", "warn")
                self._update_goal_card()
                answer = await self._ask_user(
                    {
                        "question": f"({index}/{total}) {item['question']}",
                        "options": item["options"],
                        "multi": bool(item.get("multi")),
                        "allow_other": True,
                    }
                )
                brief.append(
                    {
                        "key": str(item.get("key") or "Details"),
                        "value": self._clarify_value(answer),
                    }
                )
                state["brief"] = brief
                state["answered"] = index
                self._update_goal_card()
                self._progress(
                    f"Goal · Additional details {min(index + 1, total)}/{total}"
                    if index < total
                    else "Goal · Awaiting confirmation",
                    "warn",
                )
            if self._goal_state is None:
                return
            state["phase"] = "awaiting_confirmation"
            state["restatement"] = restatement or self._one_line(text, 200)
            state["understanding_live"] = False
            self._goal_confirming = True
            self._phase = "confirming_goal"
            self._update_goal_card()
            self._progress("Goal · awaiting confirmation", "warn")
            self._emit_status()
        except asyncio.CancelledError:
            raise
        except Exception:
            state = self._goal_state
            if state is not None:
                state["phase"] = "awaiting_confirmation"
                state["restatement"] = state.get("restatement") or self._one_line(text, 200)
                state["understanding_live"] = False
                self._goal_confirming = True
                self._phase = "confirming_goal"
                self._update_goal_card()
                self._progress("Goal · awaiting confirmation", "warn")
                self._emit_status()
        finally:
            self._goal_task = None

    async def _goal_interview(self, text: str) -> tuple[list[dict], str]:
        """一次结构化调用：复述 + 0-10 道澄清题；信息够就直接只复述。"""
        fallback = self._one_line(text, 200)
        if not needs_clarification(text):
            return [], await self._restate_goal(text)
        from agentscope.message import UserMsg

        try:
            res = await self._model.generate_structured_output(
                messages=[
                    UserMsg(
                        name="user",
                        content=f"{GOAL_INTERVIEW_PROMPT}\n\n用户需求：\n{text}",
                    )
                ],
                structured_model=GoalInterview,
            )
            data = getattr(res, "content", None) or {}
            interview = GoalInterview.model_validate(data)
        except Exception:
            return [], fallback

        questions: list[dict] = []
        for item in interview.questions[:10]:
            prompt = str(item.question or "").strip()
            if not prompt:
                continue
            options = [
                {"label": opt.label.strip(), "description": opt.description.strip()}
                for opt in item.options[:4]
                if str(opt.label or "").strip()
            ]
            if len(options) < 2:
                continue
            questions.append(
                {
                    "key": str(item.key or "").strip(),
                    "question": prompt,
                    "options": options,
                    "multi": bool(item.multi),
                }
            )
        return questions, self._join_points(interview.restatement) or fallback

    @staticmethod
    def _join_points(self, raw: str) -> str:
        """把多行复述压成一行，保持原有分隔风格。"""
        points = [
            line.lstrip("-•* 　").strip()
            for line in (raw or "").splitlines()
            if line.strip()
        ]
        return self._one_line("；".join(points), 200)

