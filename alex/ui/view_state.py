"""聊天区视图状态：TUI 对会话状态的投影。

会话进程（daemon 或同进程 LocalLink）是唯一事实源；这里只保存渲染所需的
最近一次投影。界面代码统一通过本类读写，避免再在 AlexApp 上散养一份同名状态。

历史的写入统一走 ``record(kind, payload)``：daemon 在事件里带上 ``record``，
界面不再自行猜测该写什么；未知类型直接报错，避免拼写错误静默丢记录。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from alex.core.snapshots import Checkpoint
from alex.ui.constants import ASSISTANT, USER

# 一条历史记录允许的类型：daemon 发来的 record 与本地写入共用这份白名单。
HISTORY_KINDS: frozenset[str] = frozenset(
    {
        USER,
        ASSISTANT,
        "think",
        "tool",
        "mcp",
        "skill",
        "rule",
        "result",
        "error",
        "ask",
        "queue",
        "queue_summary",
        "queue_card",
        "queue_result",
        "goal_card",
        "verifier",
        "verifier_text",
        "shell_card",
        "compaction",
        "changes",
        "stats",
    }
)

# 同一条记录重复到达时按这些字段认人：重连回放同一个事件不能写出两条，
# 状态会变的记录（shell 卡）则原地覆盖成最新状态。
DEDUPE_FIELDS: dict[str, tuple[str, ...]] = {
    "compaction": ("segment_id",),
    "changes": ("checkpoint", "exchange_id"),
    "shell_card": ("request_id",),
    "stats": ("exchange_id",),
}


def record_key(kind: str, payload: Any) -> str | None:
    """取出用于去重/覆盖的键；没有键的记录按到达顺序逐条追加。"""
    path = DEDUPE_FIELDS.get(kind)
    if path is None or not isinstance(payload, dict):
        return None
    value: Any = payload
    for name in path:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    if value is None or str(value) == "":
        return None
    return str(value)


@dataclass
class ChatViewState:
    """历史、流式正文、思考与快照的界面临时状态。"""

    history: list[tuple[str, str]] = field(default_factory=list)
    stream_text: str = ""
    thinking: str = ""
    checkpoints: list[Checkpoint] = field(default_factory=list)
    exchange_seq: int = 0
    # 去重索引：键是 "kind:key"，值是该记录在 history 里的下标
    _record_slots: dict[str, int] = field(default_factory=dict, repr=False)

    def _reindex(self) -> None:
        """按当前 history 重建去重索引（快照/重放替换历史后必须重建）。"""
        slots: dict[str, int] = {}
        for index, (kind, text) in enumerate(self.history):
            payload: Any = text
            if kind in DEDUPE_FIELDS:
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
            key = record_key(kind, payload)
            if key is not None:
                slots[f"{kind}:{key}"] = index
        self._record_slots = slots

    def record(self, kind: str, payload: Any) -> bool:
        """写一条历史记录；返回 True 表示新追加，False 表示覆盖了旧记录。"""
        if kind not in HISTORY_KINDS:
            raise ValueError(f"unknown history kind: {kind}")
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False
        )
        key = record_key(kind, payload)
        slot = None if key is None else self._record_slots.get(f"{kind}:{key}")
        if slot is not None and slot < len(self.history):
            self.history[slot] = (kind, text)
            return False
        if key is not None:
            self._record_slots[f"{kind}:{key}"] = len(self.history)
        self.history.append((kind, text))
        return True

    def apply_snapshot(self, data: dict) -> None:
        """用会话快照整体覆盖本地投影。"""
        self.history = [tuple(item) for item in data.get("history", [])]
        self.stream_text = str(data.get("stream_text", "") or "")
        self.thinking = str(data.get("thinking", "") or "")
        self.exchange_seq = int(data.get("exchange_seq", 0) or 0)
        self.checkpoints = [
            Checkpoint(**item) for item in data.get("checkpoints", []) or []
        ]
        self._reindex()

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
        self._reindex()

    def begin_thinking(self) -> None:
        self.thinking = ""

    def add_thinking(self, delta: str) -> None:
        self.thinking += delta

    def end_thinking(self, text: str = "") -> str:
        """结束思考段，返回定稿文本；有内容时写入历史。"""
        final = (text or self.thinking).strip()
        if final:
            self.record("think", final)
        self.thinking = ""
        return final

    def add_text(self, delta: str) -> None:
        self.stream_text += delta

    def set_checkpoints(self, items) -> None:
        self.checkpoints = [
            Checkpoint(**item) if isinstance(item, dict) else item
            for item in items
        ]
