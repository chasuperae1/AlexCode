"""记忆中间件分包：对话写入与上下文压缩时的日志落盘。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime
from pathlib import Path


from agentscope.agent import Agent
from agentscope.message import Msg

from alex.memory.models import (
    clear_memory,
    logger,
)


class CoreMixin:
    """对话写入与上下文压缩时的日志落盘。"""

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

    async def clear(self) -> None:
        """取消后台整理并清空所有记忆文件和流水。"""
        for task in (self._consolidation_task, self._reflection_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:  # 取消后台整理任务，CancelledError 属预期
                    pass
        self._consolidation_task = None
        self._reflection_task = None
        self._flushed_ids = None
        await asyncio.to_thread(clear_memory, self._workdir, self._memory_dir)
