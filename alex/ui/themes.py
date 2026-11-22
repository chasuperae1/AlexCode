"""Alex 的五套主题：Crimson（默认）、Dark、Light、Mono、Ember。

Crimson 取深红 #D31145 与炭灰 #333D47 作为强调色；Dark 为黑底荧光绿；
Mono 为微黄暖纸护眼配色；Ember 取暖橙 #D97757、米白 #F0EEE6 与暖炭灰。
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
        variables={
            "markdown-code-bg": "#251b20",
            "markdown-code-fg": "#ff7a93",
            "markdown-code-key": "#ff7a93",
            "markdown-code-string": "#9ad6ba",
            "markdown-code-number": "#e3a857",
            "markdown-code-comment": "#8a929b",
            "markdown-code-punct": "#8a929b",
            "markdown-code-error": "#ff8a8a",
            "markdown-table-border": "#42474c",
        },
    ),
    "dark": ThemeSpec(
        name="dark",
        primary="#44d62c",
        secondary="#80907f",
        accent="#44d62c",
        success="#6fe45b",
        warning="#e0b84c",
        error="#ff6b6b",
        foreground="#e4eee2",
        background="#040705",
        surface="#0a100c",
        panel="#101912",
        variables={
            "markdown-code-bg": "#0a140c",
            "markdown-code-fg": "#6bff55",
            "markdown-code-key": "#44d62c",
            "markdown-code-string": "#a6ff8f",
            "markdown-code-number": "#e0b84c",
            "markdown-code-comment": "#7a8d77",
            "markdown-code-punct": "#80907f",
            "markdown-code-error": "#ff6b6b",
            "markdown-table-border": "#274c2d",
        },
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
        variables={
            "text": "#333d47",
            "text-muted": "#5f6873",
            "text-disabled": "#6f7884",
            "markdown-code-bg": "#f8ecef",
            "markdown-code-fg": "#a80e37",
            "markdown-code-key": "#a80e37",
            "markdown-code-string": "#1f7a4d",
            "markdown-code-number": "#8a5200",
            "markdown-code-comment": "#5f6873",
            "markdown-code-punct": "#5f6873",
            "markdown-code-error": "#c4213c",
            "markdown-table-border": "#b3bbc4",
        },
    ),
    "mono": ThemeSpec(
        name="mono",
        primary="#4b3f2b",
        secondary="#746b5b",
        accent="#68471d",
        success="#52684e",
        warning="#8a5f1a",
        error="#8a3b32",
        foreground="#2f2b24",
        background="#f7f3e8",
        surface="#f1eadb",
        panel="#e8dfcc",
        dark=False,
        variables={
            "text": "#2f2b24",
            "text-muted": "#6b6254",
            "text-disabled": "#7a7263",
            "markdown-code-bg": "#ece3d0",
            "markdown-code-fg": "#5b3c15",
            "markdown-code-key": "#5b3c15",
            "markdown-code-string": "#7d5a2f",
            "markdown-code-number": "#7c5219",
            "markdown-code-comment": "#6b6254",
            "markdown-code-punct": "#655c4f",
            "markdown-code-error": "#8a3b32",
            "markdown-table-border": "#bcae92",
        },
    ),
    "ember": ThemeSpec(
        name="ember",
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
        variables={
            "markdown-code-bg": "#2d2925",
            "markdown-code-fg": "#d4a27f",
            "markdown-code-key": "#d4a27f",
            "markdown-code-string": "#8fbf7f",
            "markdown-code-number": "#e0b15c",
            "markdown-code-comment": "#9a9489",
            "markdown-code-punct": "#9a9489",
            "markdown-code-error": "#e06c5f",
            "markdown-table-border": "#53504a",
        },
    ),
}

LABELS = {
    "crimson": "Crimson",
    "dark": "Dark",
    "light": "Light",
    "mono": "Mono",
    "ember": "Ember",
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
