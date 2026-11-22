"""记忆模块的数据模型与共享对象。"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_CONSOLIDATION_LOCK = asyncio.Lock()


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
