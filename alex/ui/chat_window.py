"""聊天区的可见窗口计算。

窗口只决定“哪些 history 记录需要变成控件”，不改动 history 本身。
边界按用户消息/压缩卡对齐，避免把一轮对话从中间切开。
"""

from __future__ import annotations

from alex.ui.constants import USER

# 首屏保留的“权重”预算；权重≈控件数量，长文本按体量加权。
TAIL_UNITS = 120
# 向上/向下翻一页的预算。
PAGE_UNITS = 90
# 一开始进入历史模式时窗口的总预算。
WINDOW_UNITS = 150
# 活跃窗口超过这个权重就触发一次裁剪重建。
TRIM_UNITS = 190

# 可以作为窗口边界的记录类型：一轮的开始，或一次压缩的分界。
_BOUNDARY_KINDS = (USER, "compaction")


def record_weight(kind: str, text: str) -> int:
    """一条记录大约会变成多少个控件。

    普通记录 1；超长正文按 2000 字符一档加权，避免一条 30k 的回复
    在预算里只算作 1 而把窗口撑爆。
    """
    if kind in ("compaction", "stats", "changes"):
        return 1
    return 1 + max(len(text) - 2000, 0) // 2000


def window_units(
    history: list[tuple[str, str]], start: int = 0, end: int | None = None
) -> int:
    end = len(history) if end is None else min(end, len(history))
    return sum(record_weight(kind, text) for kind, text in history[start:end])


def _snap_back(history: list[tuple[str, str]], index: int) -> int:
    """把起点往前挪到最近的轮次边界（不越过 0）。"""
    index = max(0, min(index, len(history) - 1)) if history else 0
    while index > 0 and history[index][0] not in _BOUNDARY_KINDS:
        index -= 1
    return index


def _snap_forward(history: list[tuple[str, str]], index: int) -> int:
    """把终点往后挪到下一轮边界，保证不把一轮切成两半。"""
    while index < len(history) and history[index][0] not in _BOUNDARY_KINDS:
        index += 1
    return index


def tail_start(
    history: list[tuple[str, str]], budget: int = TAIL_UNITS
) -> int:
    """最近一屏（含缓冲）的起点；短历史返回 0。"""
    if not history:
        return 0
    units = 0
    index = len(history)
    while index > 0 and units < budget:
        index -= 1
        units += record_weight(*history[index])
    return _snap_back(history, index)


def forward_end(
    history: list[tuple[str, str]],
    start: int,
    budget: int = WINDOW_UNITS,
) -> int:
    """从 start 往后最多 budget 个权重，并对齐到轮次边界。"""
    index = max(0, min(start, len(history)))
    units = 0
    while index < len(history) and units < budget:
        units += record_weight(*history[index])
        index += 1
    return _snap_forward(history, index)


def earlier_start(
    history: list[tuple[str, str]],
    start: int,
    budget: int = PAGE_UNITS,
) -> int:
    """当前窗口起点之前的一页起点；已经到头时返回 0。"""
    if start <= 0:
        return 0
    index = start
    units = 0
    while index > 0 and units < budget:
        index -= 1
        units += record_weight(*history[index])
    return _snap_back(history, index)

