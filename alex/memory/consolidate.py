"""记忆中间件分包：后台整理 MEMORY.md：调度、合并压缩、状态读写、日志裁剪。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta


from agentscope.agent import Agent
from agentscope.message import SystemMsg, UserMsg

from alex.memory.models import _CONSOLIDATION_LOCK, _CuratedMemory, logger


class ConsolidateMixin:
    """后台整理 MEMORY.md：调度、合并压缩、状态读写、日志裁剪。"""

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
