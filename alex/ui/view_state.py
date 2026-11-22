"""聊天区视图状态：TUI 对会话状态的投影。

会话进程（daemon 或同进程 LocalLink）是唯一事实源；这里只保存渲染所需的
最近一次投影。界面代码统一通过本类读写，避免再在 AlexApp 上散养一份同名状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alex.core.snapshots import Checkpoint
from alex.ui.constants import USER


@dataclass
class ChatViewState:
    """历史、流式正文、思考与快照的界面临时状态。"""

    history: list[tuple[str, str]] = field(default_factory=list)
    stream_text: str = ""
    thinking: str = ""
    checkpoints: list[Checkpoint] = field(default_factory=list)
    exchange_seq: int = 0

    def apply_snapshot(self, data: dict) -> None:
        """用会话快照整体覆盖本地投影。"""
        self.history = [tuple(item) for item in data.get("history", [])]
        self.stream_text = str(data.get("stream_text", "") or "")
        self.thinking = str(data.get("thinking", "") or "")
        self.exchange_seq = int(data.get("exchange_seq", 0) or 0)
        self.checkpoints = [
            Checkpoint(**item) for item in data.get("checkpoints", []) or []
        ]

    def begin_exchange(self, exchange_id: int, text: str) -> None:
        """新一轮开始：记录用户消息，清空上一轮的流式缓冲。"""
        self.exchange_seq = exchange_id
        self.history.append((USER, text))
        self.stream_text = ""
        self.thinking = ""

    def reset_session(self, history: list[tuple[str, str]] | None = None) -> None:
        """会话被清空或重放：替换历史并丢弃未完成的流。"""
        self.history = [tuple(item) for item in history or []]
        self.stream_text = ""
        self.thinking = ""

    def begin_thinking(self) -> None:
        self.thinking = ""

    def add_thinking(self, delta: str) -> None:
        self.thinking += delta

    def end_thinking(self, text: str = "") -> str:
        """结束思考段，返回定稿文本；有内容时写入历史。"""
        final = (text or self.thinking).strip()
        if final:
            self.history.append(("think", final))
        self.thinking = ""
        return final

    def add_text(self, delta: str) -> None:
        self.stream_text += delta

    def set_checkpoints(self, items) -> None:
        self.checkpoints = [
            Checkpoint(**item) if isinstance(item, dict) else item
            for item in items
        ]
