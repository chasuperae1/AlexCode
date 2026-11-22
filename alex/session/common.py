"""会话内核的公共常量与异常（原 alex/session.py 模块级定义）。"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from alex.core.formatting import format_tokens as _format_tokens  # noqa: F401

Emit = Callable[[str, dict[str, Any]], None]

USER = "user"

ASSISTANT = "assistant"

DEFAULT_WELCOME = (
    "Hi. I can read and write files, run commands, and manage tasks. "
    "Give me a task to start."
)

BOOT_STAGES = (
    ("skills", "Skills & rules"),
    ("session", "Session history"),
    ("model", "Model & tools"),
    ("mcp", "MCP servers"),
)

READONLY_TOOLS = {
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "Skill",
    "AskUser",
    "TaskCreate",
    "TaskList",
    "TaskGet",
    "TaskUpdate",
}

GOAL_RESTATEMENT_PROMPT = (
    "你只负责复述用户想完成的目标，不执行任务、不调用工具、不做实现。\n"
    "用中文输出 1 到 3 个简洁要点，明确：目标成果、关键范围、必要约束。\n"
    "不要把它写成第一人称，也不要说‘我理解的任务是’。\n"
    "如果原始需求不完整，保留不确定性，不要擅自补充假设。"
)


class SessionError(Exception):
    """可映射为 RPC 错误码的会话异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GoalRestatement(BaseModel):
    restatement: str = Field(
        description="A concise clarification of what the user wants achieved. Do not execute it."
    )



class GoalOption(BaseModel):
    """澄清问题里的一个选项。"""

    label: str = Field(description="One short option label, at most 12 characters.")
    description: str = Field(default="", description="Why this option matters, at most 40 chars.")


class GoalQuestion(BaseModel):
    """一道澄清选择题：只问会改变结果的一个维度。"""

    key: str = Field(description="Dimension name for the brief, e.g. 岗位方向 / 城市 / 产出.")
    question: str = Field(description="Question text in Chinese, without numbering.")
    options: list[GoalOption] = Field(
        default_factory=list,
        description="2-4 concrete options. Add a catch-all such as 都可以 only when useful.",
    )
    multi: bool = False


class GoalInterview(BaseModel):
    """一次调用同时拿到复述与澄清问题；questions 可以为空。"""

    restatement: str = Field(
        description="A concise clarification of what the user wants achieved. Do not execute it."
    )
    questions: list[GoalQuestion] = Field(
        default_factory=list,
        description="0-10 clarification questions, only for missing dimensions that change the result.",
    )


GOAL_INTERVIEW_PROMPT = (
    "你在帮 Alex 理解一个将要执行的目标，只做理解与提问，不执行任务、不调用工具。\n"
    "输出两部分：\n"
    "1. restatement：用中文 1-3 句复述目标成果、关键范围、必要约束；不要第一人称，"
    "不要写“我理解的任务是”；信息缺失就保持不确定，不要编造。\n"
    "2. questions：只针对“会改变最终结果”的缺失维度提问，0 到 10 道，宁少勿多。\n"
    "   - 已经说清楚、或猜错也不影响结果的维度，不要问；\n"
    "   - 每道题给 2-4 个具体选项（不要“是/否”式空选项），需要时附一句 description；\n"
    "   - 能合并的问题合并成一道，不要追问同一维度；\n"
    "   - 完全说清楚时 questions 返回空数组。\n"
    "示例：user 说“帮我去 boss 找 5 个适合我的工作”，信息明显不足，应问岗位方向、城市、"
    "经验/薪资、产出形式这类维度；而不是问“你想找什么工作”。"
)
