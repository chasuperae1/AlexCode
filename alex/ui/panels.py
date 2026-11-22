"""聊天流面板的兼容入口：实现已按职责拆到 panels_* 模块。"""

from alex.ui.panels_chat import ChatLog
from alex.ui.panels_concise import ConciseSummary
from alex.ui.panels_goal import GoalCard
from alex.ui.panels_queue import QueueButton, QueueCard

__all__ = [
    "ChatLog",
    "ConciseSummary",
    "QueueButton",
    "QueueCard",
    "GoalCard",
]
