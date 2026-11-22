"""进度条：确定/不确定两种模式，数值变化带缓动动画。"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import RenderResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget

from .spinner import SPINNER_FRAMES as _SPINNER

_BLOCKS = " ▏▎▍▌▋▊▉█"


class ProgressBar(Widget, can_focus=True):
    """进度条。

    - `advance_to(value)` 平滑过渡到目标进度
    - `indeterminate = True` 时进入循环动画
    - ← / → 微调进度，空格暂停动画，R 归零
    """

    COMPONENT_CLASSES = {
        "progress--filled",
        "progress--track",
        "progress--label",
        "progress--value",
    }

    DEFAULT_CSS = """
    ProgressBar {
        height: auto;
        min-height: 3;
        padding: 0 1;
        background: $surface;
        border: round $primary 20%;
    }
    ProgressBar:focus {
        border: round $primary;
    }
    ProgressBar > .progress--filled {
        color: $primary;
    }
    ProgressBar > .progress--track {
        color: $primary 25%;
    }
    ProgressBar > .progress--label {
        color: $foreground;
        text-style: bold;
    }
    ProgressBar > .progress--value {
        color: $accent;
    }
    """

    BINDINGS = [
        Binding("left", "nudge(-5)", "Decrease", show=False),
        Binding("right", "nudge(5)", "Increase", show=False),
        Binding("space", "toggle_pause", "Pause / resume", show=False),
        Binding("r", "reset", "Reset", show=False),
    ]

    value = reactive(0.0, layout=False)

    def __init__(
        self,
        *,
        label: str = "",
        indeterminate: bool = False,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.label = label
        self.indeterminate = indeterminate
        self.paused = False
        self._offset = 0
        self._timer = None

    def on_mount(self) -> None:
        if self.indeterminate:
            self._start_animation()

    # ------------------------------------------------------------------ 动画
    def _start_animation(self) -> None:
        if self._timer is None:
            self._timer = self.set_interval(1 / 30, self._tick)

    def _tick(self) -> None:
        if self.paused or not self.indeterminate:
            return
        self._offset = (self._offset + 1) % 1000
        self.refresh()

    def advance_to(self, target: float, *, animate: bool = True) -> None:
        """平滑推进到目标进度（0-100）。"""
        target = max(0.0, min(100.0, target))
        if animate:
            self.animate("value", target, duration=0.35, easing="out_cubic")
        else:
            self.value = target

    def watch_value(self) -> None:
        self.refresh()

    # ------------------------------------------------------------ 键盘与渲染
    def action_nudge(self, delta: str) -> None:
        self.advance_to(self.value + float(delta), animate=False)

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        self.refresh()

    def action_reset(self) -> None:
        self.value = 0.0
        self.refresh()

    def render(self) -> RenderResult:
        text = Text(no_wrap=True, overflow="ellipsis")
        label_style = self.get_component_rich_style("progress--label", partial=True)
        filled_style = self.get_component_rich_style("progress--filled", partial=True)
        track_style = self.get_component_rich_style("progress--track", partial=True)
        value_style = self.get_component_rich_style("progress--value", partial=True)

        suffix = "  paused" if self.paused else ""
        percent = f"{self.value:5.1f}%"
        head = f"{self.label or 'Progress'}"
        text.append(head, style=label_style)
        text.append(f"  {percent}{suffix}\n", style=value_style)

        width = max(self.size.width - 2, 8)
        if self.indeterminate:
            self._render_indeterminate(text, width, filled_style, track_style)
        else:
            self._render_determinate(text, width, filled_style, track_style)
        return text

    def _render_determinate(self, text: Text, width: int, filled, track) -> None:
        filled_cells = int(width * self.value / 100)
        remainder = int((width * self.value / 100 - filled_cells) * 8)
        text.append("█" * filled_cells, style=filled)
        if filled_cells < width and remainder:
            text.append(_BLOCKS[remainder], style=filled)
            text.append("░" * (width - filled_cells - 1), style=track)
        else:
            text.append("░" * (width - filled_cells), style=track)

    def _render_indeterminate(self, text: Text, width: int, filled, track) -> None:
        head = self._offset % (width + 8) - 8
        for cell in range(width):
            inside = 0 <= cell - head < 6
            text.append("█" if inside else "░", style=filled if inside else track)
        glyph = _SPINNER[int(time.monotonic() * 12) % len(_SPINNER)]
        text.append(f"  {glyph}", style=filled)
