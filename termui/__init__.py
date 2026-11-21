"""termui 组件库。

统一导出核心组件，便于 `from termui import ScrollList, ...`。
"""

from __future__ import annotations

from .app import TermUIApp
from .theme import DEFAULT_THEME, THEMES, ThemeSpec, next_theme, register_themes
from .widgets import (
    CheckboxGroup,
    HierarchyMenu,
    ListItem,
    MenuNode,
    ProgressBar,
    PromptInput,
    ScrollList,
    StatusPanel,
)

__all__ = [
    "TermUIApp",
    "ThemeSpec",
    "THEMES",
    "DEFAULT_THEME",
    "register_themes",
    "next_theme",
    "ScrollList",
    "ListItem",
    "CheckboxGroup",
    "PromptInput",
    "ProgressBar",
    "HierarchyMenu",
    "MenuNode",
    "StatusPanel",
]
