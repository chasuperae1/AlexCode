"""工具返回值的共享构造器。"""

from __future__ import annotations

from agentscope.message import TextBlock
from agentscope.tool import ToolChunk
from agentscope.tool._response import ToolResultState


def error_chunk(message: str) -> ToolChunk:
    """构造一个纯文本的工具错误结果。"""
    return ToolChunk(
        content=[TextBlock(text=message)],
        state=ToolResultState.ERROR,
    )
