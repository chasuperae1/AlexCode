"""主题系统。

组件样式统一使用 Textual 主题变量（$primary/$surface/$panel 等），
因此切换主题后所有已挂载组件会立即重新着色，无需重建 UI。

扩展方式：
- 用 `ThemeSpec(...)` 新增配色，`register_themes(app)` 后 `app.theme = "名字"`
- 只改单个组件外观时，覆盖其 `DEFAULT_CSS` 或追加组件样式类（见各组件 COMPONENT_CLASSES）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.app import App
from textual.theme import Theme


@dataclass(frozen=True)
class ThemeSpec:
    """一套终端配色。字段与 Textual 主题一一对应。"""

    name: str
    primary: str
    secondary: str
    accent: str
    success: str
    warning: str
    error: str
    foreground: str
    background: str
    surface: str
    panel: str
    dark: bool = True
    variables: dict[str, str] = field(default_factory=dict)

    def to_textual_theme(self) -> Theme:
        return Theme(
            name=self.name,
            primary=self.primary,
            secondary=self.secondary,
            accent=self.accent,
            success=self.success,
            warning=self.warning,
            error=self.error,
            foreground=self.foreground,
            background=self.background,
            surface=self.surface,
            panel=self.panel,
            dark=self.dark,
            variables=dict(self.variables),
        )


# 默认配色遵循「极简 + 灰/蓝/紫」：主色偏石板蓝，强调色用紫，其余走中性灰
THEMES: dict[str, ThemeSpec] = {
    "slate": ThemeSpec(
        name="slate",
        primary="#8b9cf7",
        secondary="#7e879c",
        accent="#b39dfa",
        success="#6cc98a",
        warning="#d9b26a",
        error="#e8788a",
        foreground="#e6e8ee",
        background="#0f1117",
        surface="#171a22",
        panel="#1e222d",
    ),
    "slate-light": ThemeSpec(
        name="slate-light",
        primary="#4353b8",
        secondary="#5c6478",
        accent="#6a4fb5",
        success="#2f7d4f",
        warning="#8a6218",
        error="#b3354b",
        foreground="#1c1f27",
        background="#f5f6f8",
        surface="#eceef3",
        panel="#e2e5ec",
        dark=False,
    ),
    "high-contrast": ThemeSpec(
        name="high-contrast",
        primary="#9fb4ff",
        secondary="#c8cdd8",
        accent="#cbb2ff",
        success="#7ef0a2",
        warning="#ffd479",
        error="#ff8fa3",
        foreground="#ffffff",
        background="#000000",
        surface="#111111",
        panel="#1b1b1b",
    ),
}

DEFAULT_THEME = "slate"


def register_themes(app: App) -> None:
    """把内置主题注册到 App，并切换到默认主题。"""
    for spec in THEMES.values():
        app.register_theme(spec.to_textual_theme())
    # 只在 App 还没选过主题时切默认，避免覆盖子类在 on_mount 里设置的主题
    if app.theme not in THEMES and app.theme == "textual-dark":
        app.theme = DEFAULT_THEME


def next_theme(current: str) -> str:
    """按注册顺序返回下一个主题名。"""
    names = list(THEMES)
    try:
        return names[(names.index(current) + 1) % len(names)]
    except ValueError:
        return names[0]
