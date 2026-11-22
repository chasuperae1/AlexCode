"""记忆中间件分包：LESSONS.md 的读写、合并、裁剪与渲染。"""

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


class LessonsMixin:
    """LESSONS.md 的读写、合并、裁剪与渲染。"""

    def _read_lessons(self) -> str:
        try:
            return self._lessons_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _norm_title(title: str) -> str:
        # 去掉日期前缀和标点，用于跨轮去重
        title = re.sub(r"^\d{4}-\d{2}-\d{2}\s*·\s*", "", title.strip())
        return re.sub(r"[\W_]+", "", title).lower()

    @staticmethod
    def _lesson_entries(text: str) -> list[str]:
        if not text:
            return []
        parts = re.split(r"^##\s+", text, flags=re.M)
        return [f"## {part.strip()}" for part in parts[1:] if part.strip()]

    def _append_lessons(self, lessons: list[dict]) -> bool:
        """追加经验；返回是否已超出数量或字符上限。"""
        entries = self._lesson_entries(self._read_lessons())
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
        for item in lessons:
            entries.append(
                f"## {stamp} · {item['title']}\n"
                f"- type: {item['type']} · confidence: {item['confidence']:.2f}\n\n"
                f"{item['content']}"
            )
        body = self._render_lessons(entries)
        self._write_lessons(body)
        return (
            len(entries) > self.MAX_LESSONS
            or len(body) > self.MAX_LESSONS_CHARS
        )

    async def _merge_lessons(self, agent: Agent) -> None:
        """LESSONS.md 满时让模型合并去重，失败则按置信度保底裁剪。"""
        existing = self._read_lessons()
        if not existing:
            return
        raw: list = []
        try:
            response = await agent.model.generate_structured_output(
                [
                    SystemMsg(
                        name="system",
                        content=self.MERGE_PROMPT.format(
                            max_lessons=self.MAX_LESSONS,
                            max_content=self.MAX_LESSON_CONTENT_CHARS,
                            max_chars=self.MAX_LESSONS_CHARS,
                        ),
                    ),
                    UserMsg(name="user", content=existing[: self.MAX_INPUT_CHARS]),
                ],
                structured_model=_LessonList,
            )
            content = getattr(response, "content", {}) or {}
            raw = content.get("lessons") or []
        except Exception:
            logger.warning("Lesson merge failed", exc_info=True)

        stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
        entries: list[str] = []
        for item in raw:
            if isinstance(item, Lesson):
                data = item.model_dump()
            elif isinstance(item, dict):
                data = item
            else:
                continue
            title = str(data.get("title", "")).strip()
            body = str(data.get("content", "")).strip()
            if not title or not body:
                continue
            try:
                confidence = float(data.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            entries.append(
                f"## {stamp} · {title[:120]}\n"
                f"- type: {str(data.get('type', 'project'))} · "
                f"confidence: {confidence:.2f}\n\n"
                f"{body[: self.MAX_LESSON_CONTENT_CHARS]}"
            )

        if entries:
            self._write_lessons(self._render_lessons(entries))
            if (
                len(entries) > self.MAX_LESSONS
                or len(self._read_lessons()) > self.MAX_LESSONS_CHARS
            ):
                self._trim_lessons()
        else:
            self._trim_lessons()

    def _trim_lessons(self) -> None:
        """按置信度 + 日期保留高价值条目，直到满足数量与字符上限。"""
        scored: list[tuple[float, str, str]] = []
        for entry in self._lesson_entries(self._read_lessons()):
            head = entry.splitlines()[0]
            match = re.search(r"confidence:\s*([0-9.]+)", entry)
            try:
                confidence = float(match.group(1)) if match else 0.0
            except ValueError:
                confidence = 0.0
            date = re.search(r"(\d{4}-\d{2}-\d{2})", head)
            scored.append((confidence, date.group(1) if date else "", entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        kept: list[str] = []
        for _confidence, _date, entry in scored:
            if len(kept) >= self.MAX_LESSONS:
                break
            candidate = self._render_lessons(kept + [entry])
            if len(candidate) > self.MAX_LESSONS_CHARS and kept:
                continue
            kept.append(entry)
        self._write_lessons(self._render_lessons(kept))

    def _write_lessons(self, body: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._lessons_file.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(self._lessons_file)

    @staticmethod
    def _render_lessons(entries: list[str]) -> str:
        return "# Lessons\n\n" + "\n\n".join(entries) + "\n"
