"""状态提示面板：分级消息、自动消失、忙碌动画。"""

from __future__ import annotations

from rich.text import Text
from textual.app import RenderResult
from textual.widget import Widget

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# 使用 ASCII 符号，避免部分终端缺少 ℹ ✔ 等字形显示成方块
_ICONS = {
    "info": ("i", "status--info"),
    "success": ("+", "status--success"),
    "warning": ("!", "status--warning"),
    "error": ("x", "status--error"),
}


class StatusPanel(Widget):
    """底部状态面板。

    `notify("已保存", level="success", timeout=3)` 会在面板上停留 3 秒；
    `busy = True` 时显示旋转指示器，仅在忙碌期间定时刷新。
    """

    COMPONENT_CLASSES = {
        "status--info",
        "status--success",
        "status--warning",
        "status--error",
        "status--message",
    }

    DEFAULT_CSS = """
    StatusPanel {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $foreground;
    }
    StatusPanel > .status--info {
        color: $primary;
    }
    StatusPanel > .status--success {
        color: $success;
    }
    StatusPanel > .status--warning {
        color: $warning;
    }
    StatusPanel > .status--error {
        color: $error;
    }
    StatusPanel > .status--message {
        color: $foreground;
    }
    StatusPanel.-hidden {
        display: none;
    }
    """

    def __init__(
        self,
        *,
        text: str = "Ready",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.default_text = text
        self.message = text
        self.level = "info"
        self._busy = False
        self._spin_index = 0
        self._timer = None
        self._reset_timer = None
        self._sync_visibility()

    # ------------------------------------------------------------------ 状态
    @property
    def busy(self) -> bool:
        return self._busy

    @busy.setter
    def busy(self, value: bool) -> None:
        self._busy = value
        if value and self._timer is None:
            self._timer = self.set_interval(1 / 15, self._tick)
        elif not value and self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._sync_visibility()
        self.refresh()

    def _sync_visibility(self) -> None:
        """空闲且没有消息时整行隐藏，不占用界面高度。"""
        self.set_class(not self._busy and not self.message, "-hidden")

    def _tick(self) -> None:
        self._spin_index = (self._spin_index + 1) % len(_SPINNER)
        self.refresh()

    def notify(self, message: str, *, level: str = "info", timeout: float | None = None) -> None:
        """显示一条状态消息，timeout 秒后恢复默认文本。"""
        self.message = message
        self.level = level if level in _ICONS else "info"
        if self._reset_timer is not None:
            self._reset_timer.stop()
            self._reset_timer = None
        if timeout:
            self._reset_timer = self.set_timer(timeout, self.reset)
        self._sync_visibility()
        self.refresh()

    def reset(self) -> None:
        self.message = self.default_text
        self.level = "info"
        self._reset_timer = None
        self._sync_visibility()
        self.refresh()

    # ------------------------------------------------------------------ 渲染
    def render(self) -> RenderResult:
        icon, level_class = _ICONS[self.level]
        level_style = self.get_component_rich_style(level_class, partial=True)
        message_style = self.get_component_rich_style("status--message", partial=True)

        text = Text(no_wrap=True, overflow="ellipsis")
        if self._busy:
            text.append(_SPINNER[self._spin_index] + " ", style=level_style)
        else:
            text.append(icon + " ", style=level_style)
        text.append(self.message, style=message_style)
        return text
