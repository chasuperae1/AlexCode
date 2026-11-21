"""Daily memory stream + background consolidation + compression reflection.

Layer 1: ``<memory_dir>/memory/YYYY-MM-DD.md`` — append-only flush at the end
of every call, before context compaction, and when a call fails.
Layer 2: ``<memory_dir>/MEMORY.md`` — periodically merged, deduplicated and
length-limited by a background consolidator.
Layer 3: ``<memory_dir>/LESSONS.md`` — durable lessons extracted from the
compression summary after every context compaction.

Stable preferences belong in ``rules/``, not in memory.
"""

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

logger = logging.getLogger(__name__)
_CONSOLIDATION_LOCK = asyncio.Lock()


class _CuratedMemory(BaseModel):
    memory: str = Field(
        description="The complete merged MEMORY.md content in Markdown.",
    )


class Lesson(BaseModel):
    """一条跨会话仍然成立的长期经验。"""

    title: str = Field(description="One-line title, at most 40 characters.")
    content: str = Field(description="What to do and why, at most 300 chars.")
    type: Literal["preference", "feedback", "project"] = "project"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class _LessonList(BaseModel):
    lessons: list[Lesson] = Field(default_factory=list)


class DailyMemoryMiddleware(MiddlewareBase):
    """Append conversation turns to daily logs and consolidate them in background."""

    MAX_MESSAGE_CHARS = 2000
    MAX_INPUT_CHARS = 20000

    CONSOLIDATION_PROMPT = """你在维护 Alex 的长期任务记忆 MEMORY.md。

输入包含两部分：现有的 MEMORY.md，以及新的每日记忆流水。
请把它们合并成一份精炼、去重后的 Markdown 文档。

规则：
- 只保留持久的事实、决策、已完成任务和未完成的后续事项。
- 删除寒暄、重复内容、临时状态和工具调用噪声。
- 按主题合并；新信息覆盖旧信息。
- 不要记录用户偏好或行为规则，它们放在 rules/ 目录。
- 全文不超过 {max_chars} 个字符。
- 只输出完整的 Markdown 正文，以 "# Memory" 开头，不要代码块。"""

    REFLECTION_PROMPT = """你是 Alex 的记忆反思器。输入包含上下文压缩摘要、最近对话和已有经验。

请从中提炼 0-3 条跨会话仍然有价值的长期经验：用户偏好、被纠正过的做法、项目约定。

规则：
- 只保留长期有效的内容；一次性任务、临时状态、寒暄不要写。
- 不要记录密钥、密码等敏感信息。
- 与“已有经验”重复或语义相同的不要输出。
- content 写清楚“怎么做 + 为什么”，不超过 300 字。
- 没有值得保留的内容时返回空数组。"""

    REFLECTION_MIN_CONFIDENCE = 0.7
    MAX_LESSONS = 40
    MAX_LESSONS_CHARS = 12000
    MAX_LESSON_CONTENT_CHARS = 2000

    MERGE_PROMPT = """你在整理 Alex 的长期经验 LESSONS.md。输入是已有的全部经验条目。

请合并语义重复、删除过时或低价值内容，输出一份精炼后的经验列表。

规则：
- 最多 {max_lessons} 条，每条正文不超过 {max_content} 字，全文控制在 {max_chars} 字符以内。
- 优先保留高置信、仍然适用、对后续任务影响大的经验。
- content 写清楚“怎么做 + 为什么”。
- 不要新增输入中不存在的事实。
- 没有可保留的内容时返回空数组。"""

    def __init__(
        self,
        workdir: str,
        memory_dir: str = "Memory",
        *,
        consolidation_minutes: int = 30,
        max_memory_chars: int = 8000,
        retention_days: int = 30,
        on_compacting: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self._workdir = str(workdir)
        self._memory_dir = memory_dir
        self._dir = Path(workdir) / memory_dir
        self._daily_dir = self._dir / "memory"
        self._memory_file = self._dir / "MEMORY.md"
        self._lessons_file = self._dir / "LESSONS.md"
        self._state_file = self._daily_dir / ".consolidation_state"
        self._consolidation_minutes = max(0, consolidation_minutes)
        self._max_memory_chars = max(1000, max_memory_chars)
        self._retention_days = max(0, retention_days)
        self._flushed_ids: set[str] | None = None
        self._consolidation_task: asyncio.Task | None = None
        self._reflection_task: asyncio.Task | None = None
        self._reflection_lock = asyncio.Lock()
        self._on_compacting = on_compacting

    # ------------------------------------------------------------------ hooks
    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        # 只记录本次 call 新产生的消息，避免重启后把旧上下文重复写入
        if self._flushed_ids is None:
            self._flushed_ids = {message.id for message in agent.state.context}

        last_reply: Msg | None = None
        try:
            async for item in next_handler(**input_kwargs):
                if isinstance(item, Msg) and item.role == "assistant":
                    last_reply = item
                yield item
        except Exception:
            await self._safe_flush(agent, "context overflow / error", last_reply)
            self._schedule_consolidation(agent)
            raise
        else:
            await self._safe_flush(agent, "call end", last_reply)
            self._schedule_consolidation(agent)

    async def on_compress_context(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable[None]],
    ) -> None:
        await self._safe_flush(agent, "before compaction")
        before = agent.state.summary
        # 框架每轮推理前都会调用这个钩子，真正压缩时会长时间无事件。
        # 延迟 0.4s 再对外提示，未压缩则直接取消，避免每轮闪一下。
        announced = False

        async def announce() -> None:
            nonlocal announced
            await asyncio.sleep(0.4)
            announced = True
            await self._notify_compacting(True)

        task = asyncio.create_task(announce()) if self._on_compacting else None
        try:
            await next_handler(**input_kwargs)
        finally:
            if task is not None:
                if not announced:
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if announced:
                await self._notify_compacting(False)
        # 只有真正压缩出新摘要时才反思，截断兜底不触发
        if agent.state.summary != before:
            self._schedule_reflection(agent)

    async def _notify_compacting(self, active: bool) -> None:
        """压缩状态提示失败不能影响压缩本身。"""
        if self._on_compacting is None:
            return
        try:
            await self._on_compacting(active)
        except Exception:
            logger.warning("Compaction notification failed", exc_info=True)

    async def _safe_flush(
        self,
        agent: Agent,
        reason: str,
        extra: Msg | None = None,
    ) -> None:
        """记忆写入失败不能影响正常对话。"""
        try:
            await self._flush_context(agent, reason, extra)
        except Exception:
            logger.warning("Memory flush failed", exc_info=True)

    # ------------------------------------------------------------------ flush
    async def _flush_context(
        self,
        agent: Agent,
        reason: str,
        extra: Msg | None = None,
    ) -> None:
        if self._flushed_ids is None:
            self._flushed_ids = {message.id for message in agent.state.context}
            return

        messages = list(agent.state.context)
        if extra is not None:
            messages.append(extra)

        selected: list[Msg] = []
        seen: set[str] = set()
        for message in messages:
            if not isinstance(message, Msg) or message.id in seen:
                continue
            seen.add(message.id)
            if message.id in self._flushed_ids:
                continue
            if message.role not in {"user", "assistant"}:
                continue
            if not (message.get_text_content() or "").strip():
                continue
            selected.append(message)

        if not selected:
            return

        lines = []
        for message in selected:
            label = "User" if message.role == "user" else "Alex"
            lines.append(f"**{label}:** {self._clip(message.get_text_content())}")

        await asyncio.to_thread(self._write_entry, reason, lines)
        self._flushed_ids.update(message.id for message in selected)

    def _write_entry(self, reason: str, lines: list[str]) -> None:
        now = datetime.now().astimezone()
        self._daily_dir.mkdir(parents=True, exist_ok=True)
        path = self._daily_dir / f"{now:%Y-%m-%d}.md"
        header = f"\n## {now.isoformat(timespec='seconds')} — {reason}"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(header + "\n\n" + "\n\n".join(lines) + "\n")

    def _clip(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) <= self.MAX_MESSAGE_CHARS:
            return text
        return text[: self.MAX_MESSAGE_CHARS].rstrip() + "\n\n… (truncated)"

    # ---------------------------------------------------------- consolidation
    def _schedule_consolidation(self, agent: Agent) -> None:
        try:
            if self._consolidation_task and not self._consolidation_task.done():
                return
            delay = self._consolidation_delay()
            if delay is None:
                return
            self._consolidation_task = asyncio.create_task(
                self._consolidate_later(agent, delay),
            )
            self._consolidation_task.add_done_callback(self._on_consolidation_done)
        except Exception:
            logger.warning("Memory consolidation scheduling failed", exc_info=True)

    async def _consolidate_later(self, agent: Agent, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await self._consolidate(agent)

    def _on_consolidation_done(self, task: asyncio.Task) -> None:
        self._consolidation_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("Memory consolidation failed: %s", error)

    def _consolidation_delay(self) -> float | None:
        if not self._daily_dir.is_dir():
            return None
        last, _offsets = self._read_state()
        if not any(
            path.stat().st_mtime > last
            for path in self._daily_dir.glob("????-??-??.md")
        ):
            return None
        wait = self._consolidation_minutes * 60 - (time.time() - last)
        return max(0.0, wait)

    async def _consolidate(self, agent: Agent) -> None:
        async with _CONSOLIDATION_LOCK:
            last, offsets = self._read_state()
            chunk, new_offsets = await asyncio.to_thread(
                self._read_new_daily, offsets, last
            )
            if not chunk:
                await asyncio.to_thread(self._prune_daily)
                return
            existing = self._read_memory()
            # 先用字符数做一次不花 token 的预估：未超上限就直接追加，
            # 超过才让模型把已有 MEMORY.md 和新条目一起压到上限内。
            if len(existing) + len(chunk) <= self._max_memory_chars:
                merged = self._append_memory(existing, chunk)
            else:
                merged = await self._compress_memory(agent, existing, chunk)
            await asyncio.to_thread(self._write_memory, merged)
            await asyncio.to_thread(self._prune_daily)
            await asyncio.to_thread(self._write_state, time.time(), new_offsets)

    def _append_memory(self, existing: str, chunk: str) -> str:
        body = existing.strip() or "# Memory"
        return f"{body}\n\n{chunk.strip()}\n"

    async def _compress_memory(
        self, agent: Agent, existing: str, chunk: str
    ) -> str:
        payload = (
            "Current MEMORY.md:\n"
            f"{existing or '(empty)'}\n\n"
            "New daily entries:\n"
            f"{chunk[: self.MAX_INPUT_CHARS]}"
        )
        memory = ""
        try:
            response = await agent.model.generate_structured_output(
                [
                    SystemMsg(
                        name="system",
                        content=self.CONSOLIDATION_PROMPT.format(
                            max_chars=self._max_memory_chars,
                        ),
                    ),
                    UserMsg(name="user", content=payload),
                ],
                structured_model=_CuratedMemory,
            )
            content = getattr(response, "content", {}) or {}
            memory = str(content.get("memory", "")).strip()
        except Exception:
            logger.warning("Memory compression failed", exc_info=True)
        if not memory:
            memory = f"{existing.strip()}\n\n{chunk.strip()}"
        return self._trim_memory(memory)

    def _trim_memory(self, text: str) -> str:
        """兜底裁剪：优先在章节边界截断，避免把一句话切成两半。"""
        text = text.strip()
        if len(text) <= self._max_memory_chars:
            return text
        head = text[: self._max_memory_chars]
        cut = head.rfind("\n## ")
        if cut > 0:
            head = head[:cut]
        return head.rstrip() + "\n\n… (truncated)"

    def _read_state(self) -> tuple[float, dict[str, int]]:
        """整理状态：上次完成时间和每个日志文件已处理的字符偏移。"""
        try:
            raw = self._state_file.read_text(encoding="utf-8").strip()
        except OSError:
            return 0.0, {}
        if not raw:
            return 0.0, {}
        try:
            data = json.loads(raw)
        except ValueError:
            # 兼容旧版：状态文件里只是一个时间戳
            try:
                return float(raw), {}
            except ValueError:
                return 0.0, {}
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            # 旧版状态就是一个裸时间戳
            return float(data), {}
        if not isinstance(data, dict):
            return 0.0, {}
        try:
            stamp = float(data.get("time", 0.0))
        except (TypeError, ValueError):
            stamp = 0.0
        offsets: dict[str, int] = {}
        raw_offsets = data.get("offsets")
        if isinstance(raw_offsets, dict):
            for name, value in raw_offsets.items():
                try:
                    offsets[str(name)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
        return stamp, offsets

    def _write_state(self, stamp: float, offsets: dict[str, int]) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(
            json.dumps({"time": stamp, "offsets": offsets}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_new_daily(
        self, offsets: dict[str, int], last: float
    ) -> tuple[str, dict[str, int]]:
        """只读取每个日期文件自上次整理后新增的部分，避免重复喂给模型。"""
        if not self._daily_dir.is_dir():
            return "", {}
        new_offsets: dict[str, int] = {}
        chunks: list[str] = []
        for path in sorted(self._daily_dir.glob("????-??-??.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            start = offsets.get(path.name)
            if start is not None and 0 <= start <= len(text):
                chunk = text[start:]
            elif last > 0 and not offsets:
                # 旧版只有时间戳：按小节头的时间筛选，避免把历史再追加一遍
                chunk = self._sections_after(text, last)
            else:
                chunk = text
            new_offsets[path.name] = len(text)
            body = self._strip_section_headers(chunk).strip()
            if body:
                chunks.append(f"## {path.stem}\n\n{body}")
        return "\n\n".join(chunks), new_offsets

    @staticmethod
    def _sections_after(text: str, last: float) -> str:
        kept: list[str] = []
        for part in re.split(r"^##\s+", text, flags=re.M)[1:]:
            head, _, body = part.partition("\n")
            try:
                stamp = datetime.fromisoformat(head.split("—")[0].strip())
            except ValueError:
                continue
            if stamp.timestamp() > last:
                kept.append(body)
        return "\n\n".join(item.strip() for item in kept if item.strip())

    @staticmethod
    def _strip_section_headers(text: str) -> str:
        """去掉日志里的 "## 时间 — 原因" 头，只留消息内容。"""
        body = re.sub(r"^##\s+\S+\s+—\s+.*$", "", text, flags=re.M)
        return re.sub(r"\n{3,}", "\n\n", body)

    def _prune_daily(self) -> None:
        """超过保留期的日期文件归档到 memory/archive/YYYY-MM.md 后删除。"""
        if self._retention_days <= 0 or not self._daily_dir.is_dir():
            return
        cutoff = (
            datetime.now().astimezone() - timedelta(days=self._retention_days)
        ).date()
        for path in sorted(self._daily_dir.glob("????-??-??.md")):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            archive_dir = self._daily_dir / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive = archive_dir / f"{path.stem[:7]}.md"
            with archive.open("a", encoding="utf-8") as stream:
                stream.write(f"\n\n## {path.stem}\n\n{text}\n")
            try:
                path.unlink()
            except OSError:
                continue

    def _read_memory(self) -> str:
        try:
            return self._memory_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _write_memory(self, content: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._memory_file.with_suffix(".tmp")
        tmp.write_text(content + "\n", encoding="utf-8")
        tmp.replace(self._memory_file)

    # ------------------------------------------------------------- reflection
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

    # ------------------------------------------------------------------ reset
    async def clear(self) -> None:
        """取消后台整理并清空所有记忆文件和流水。"""
        for task in (self._consolidation_task, self._reflection_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._consolidation_task = None
        self._reflection_task = None
        self._flushed_ids = None
        await asyncio.to_thread(clear_memory, self._workdir, self._memory_dir)


def find_memory_middleware(agent: Agent) -> DailyMemoryMiddleware | None:
    """从 Agent 的中间件链里找到当前的记忆中间件。"""
    for name in ("_reply_middlewares", "_compress_context_middlewares"):
        for middleware in getattr(agent, name, ()):
            if isinstance(middleware, DailyMemoryMiddleware):
                return middleware
    return None


def clear_memory(workdir: str, memory_dir: str = "Memory") -> None:
    """删除记忆目录下的所有内容，只保留一个空的 MEMORY.md。"""
    root = Path(workdir) / memory_dir
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
