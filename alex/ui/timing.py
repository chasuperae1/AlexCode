"""Textual 计时器的共享样板。"""

from __future__ import annotations

from typing import Any, Callable


def stop_timer(timer: Any) -> None:
    """尽力停止计时器；计时器可能已被 Textual 回收。"""
    if timer is not None:
        try:
            timer.stop()
        except Exception:  # 计时器可能已被回收
            pass
    return None


def ensure_interval(
    widget: Any,
    timer: Any,
    callback: Callable[[], None],
    interval: float = 0.1,
) -> Any:
    """没有计时器时创建一个，否则沿用现有的。"""
    if timer is None:
        return widget.set_interval(interval, callback)
    return timer
