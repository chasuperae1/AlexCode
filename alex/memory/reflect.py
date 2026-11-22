"""记忆中间件分包：反思提炼长期经验：调度与生成。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agentscope.agent import Agent
from agentscope.message import Msg, SystemMsg, UserMsg
from agentscope.middleware import MiddlewareBase

from alex.memory.models import Lesson, _CONSOLIDATION_LOCK, _CuratedMemory, _LessonList, logger


class ReflectMixin:
    """反思提炼长期经验：调度与生成。"""

    def _schedule_reflection(self, agent: Agent) -> None:
        """压缩后异步提炼经验，失败不影响当前回复。"""
        try:
            if self._reflection_task and not self._reflection_task.done():
                return
            self._reflection_task = asyncio.create_task(self._reflect(agent))
            self._reflection_task.add_done_callback(self._on_reflection_done)
        except Exception:
            logger.warning("Memory reflection scheduling failed", exc_info=True)

    def _on_reflection_done(self, task: asyncio.Task) -> None:
        if self._reflection_task is task:
            self._reflection_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("Memory reflection failed: %s", error)

    async def _reflect(self, agent: Agent) -> None:
        """从压缩摘要和最近对话中提炼长期经验，写入 LESSONS.md。"""
        async with self._reflection_lock:
            summary = (agent.state.summary or "").strip()
            if not summary:
                return

            existing = self._read_lessons()
            payload = (
                "Existing lessons:\n"
                f"{existing or '(empty)'}\n\n"
                "Compression summary:\n"
                f"{summary[: self.MAX_INPUT_CHARS]}\n\n"
                "Recent messages:\n"
                f"{self._recent_exchanges(agent)}"
            )
            response = await agent.model.generate_structured_output(
                [
                    SystemMsg(name="system", content=self.REFLECTION_PROMPT),
                    UserMsg(name="user", content=payload),
                ],
                structured_model=_LessonList,
            )
            content = getattr(response, "content", {}) or {}
            raw = content.get("lessons") or []

            known = {
                self._norm_title(match.group(1))
                for match in re.finditer(r"^##\s+(.+)$", existing, re.M)
            }
            lessons: list[dict] = []
            for item in raw:
                if isinstance(item, Lesson):
                    data = item.model_dump()
                elif isinstance(item, dict):
                    data = item
                else:
                    continue
                title = str(data.get("title", "")).strip()
                body = str(data.get("content", "")).strip()
                try:
                    confidence = float(data.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                if (
                    not title
                    or not body
                    or confidence < self.REFLECTION_MIN_CONFIDENCE
                ):
                    continue
                key = self._norm_title(title)
                if key in known:
                    continue
                known.add(key)
                lessons.append(
                    {
                        "title": title[:120],
                        "content": body[: self.MAX_LESSON_CONTENT_CHARS],
                        "type": str(data.get("type", "project")),
                        "confidence": confidence,
                    }
                )

            if lessons:
                overflow = self._append_lessons(lessons)
                if overflow:
                    await self._merge_lessons(agent)

    def _recent_exchanges(self, agent: Agent) -> str:
        lines: list[str] = []
        for message in list(agent.state.context)[-4:]:
            if not isinstance(message, Msg):
                continue
            if message.role not in {"user", "assistant"}:
                continue
            text = (message.get_text_content() or "").strip()
            if not text:
                continue
            label = "User" if message.role == "user" else "Alex"
            lines.append(f"**{label}:** {text[:500]}")
        return "\n\n".join(lines) or "(none)"
