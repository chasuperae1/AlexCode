"""跨层共用的小型格式化函数。"""

from __future__ import annotations


def format_tokens(value: int) -> str:
    """把 token 数压成 999 / 1.2k / 1.5m。"""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def format_duration(seconds: float) -> str:
    """把秒数格式化为 12s / 6m12s / 1h06m。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        if remainder:
            return f"{minutes}m{remainder:02d}s"
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
