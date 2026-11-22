"""Agent 的长期记忆中间件（对外仍通过 alex.memory 暴露）。"""

from __future__ import annotations

from agentscope.agent import Agent
from agentscope.middleware import MiddlewareBase

# clear_memory 由 alex.memory 对外再导出（alex/cli.py、session/workspace.py 使用）
from alex.memory.models import clear_memory  # noqa: F401
from alex.memory.consolidate import ConsolidateMixin
from alex.memory.core import CoreMixin
from alex.memory.lessons import LessonsMixin
from alex.memory.reflect import ReflectMixin


class DailyMemoryMiddleware(
    CoreMixin,
    ConsolidateMixin,
    ReflectMixin,
    LessonsMixin,
    MiddlewareBase,
):
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


def find_memory_middleware(agent: Agent) -> DailyMemoryMiddleware | None:
    """从 Agent 的中间件链里找到当前的记忆中间件。"""
    for name in ("_reply_middlewares", "_compress_context_middlewares"):
        for middleware in getattr(agent, name, ()):
            if isinstance(middleware, DailyMemoryMiddleware):
                return middleware
    return None

