"""Agent 会话内核：与界面无关，供 TUI 与后台 daemon 共用。

原先是一个 2900 行的单文件；现在按职责拆成多个 Mixin，本模块只负责
组装 ``AgentSession`` 并重导出公共名字，对外接口保持不变。
"""

from __future__ import annotations

from alex.session.common import (  # noqa: F401
    ASSISTANT,
    BOOT_STAGES,
    DEFAULT_WELCOME,
    Emit,
    GOAL_RESTATEMENT_PROMPT,
    GoalRestatement,
    READONLY_TOOLS,
    SessionError,
    USER,
    _format_tokens,
)
from alex.session.core import CoreMixin
from alex.session.goal import GoalMixin
from alex.session.history import HistoryMixin
from alex.session.info import InfoMixin
from alex.session.queue import QueueMixin
from alex.session.reply import ReplyMixin
from alex.session.tools import ToolsMixin
from alex.session.workspace import WorkspaceMixin


class AgentSession(
    CoreMixin,
    GoalMixin,
    QueueMixin,
    ReplyMixin,
    ToolsMixin,
    HistoryMixin,
    WorkspaceMixin,
    InfoMixin,
):
    """一次对话的会话内核（原 ``alex.session.AgentSession``）。"""
