"""Alex 的五套主题：Crimson（默认）、Dark、Light、Mono、Claude。

Crimson 取深红 #D31145 与炭灰 #333D47 作为强调色；Mono 为白底黑字灰阶；
Claude 取暖橙 #D97757、米白 #F0EEE6 与暖炭灰，偏 Claude Code 的终端观感。
"""

from __future__ import annotations

from termui import ThemeSpec

DEFAULT_THEME = "crimson"

# 顺序即 Ctrl+T 的循环顺序
THEMES: dict[str, ThemeSpec] = {
    "crimson": ThemeSpec(
        name="crimson",
        primary="#d31145",
        secondary="#8a929b",
        accent="#ff7a93",
        success="#5fb98a",
        warning="#e3a857",
        error="#ff8a8a",
        foreground="#edeff2",
        background="#0d1013",
        surface="#14181c",
        panel="#1a1f24",
    ),
    "dark": ThemeSpec(
        name="dark",
        primary="#8b9cf7",
        secondary="#7e879c",
        accent="#b39dfa",
        success="#6cc98a",
        warning="#d9b26a",
        error="#e8788a",
        foreground="#e6e8ee",
        background="#000000",
        surface="#111318",
        panel="#191c22",
    ),
    "light": ThemeSpec(
        name="light",
        primary="#d31145",
        secondary="#6b7480",
        accent="#a80e37",
        success="#1f7a4d",
        warning="#a86400",
        error="#c4213c",
        foreground="#333d47",
        background="#ffffff",
        surface="#f4f5f7",
        panel="#eceef1",
        dark=False,
    ),
    "mono": ThemeSpec(
        name="mono",
        primary="#111111",
        secondary="#6b6b6b",
        accent="#000000",
        success="#808080",
        warning="#5a5a5a",
        error="#000000",
        foreground="#111111",
        background="#ffffff",
        surface="#f6f6f6",
        panel="#e6e6e6",
        dark=False,
    ),
    "claude": ThemeSpec(
        name="claude",
        primary="#d97757",
        secondary="#9a9489",
        accent="#d4a27f",
        success="#8fbf7f",
        warning="#e0b15c",
        error="#e06c5f",
        foreground="#f0eee6",
        background="#1f1e1d",
        surface="#262624",
        panel="#30302e",
    ),
}

LABELS = {
    "crimson": "Crimson",
    "dark": "Dark",
    "light": "Light",
    "mono": "Mono",
    "claude": "Claude",
}


def theme_label(name: str) -> str:
    return LABELS.get(name, name)


def theme_name(label: str) -> str:
    """把弹窗里显示的名称转回主题 key。"""
    for key, text in LABELS.items():
        if text == label:
            return key
    return label


def next_theme(current: str) -> str:
    names = list(THEMES)
    try:
        return names[(names.index(current) + 1) % len(names)]
    except ValueError:
        return names[0]
