"""启动遮罩：ALEXCODE 字符画、渐变色与阶段进度。"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from alex.ui.constants import _BOOT_BANNER, _BOOT_SPINNER


def _hex_rgb(value: str) -> tuple[int, int, int] | None:
    """把 #rrggbb 解析成 RGB；非十六进制颜色返回 None。"""
    value = value.strip()
    if len(value) != 7 or not value.startswith("#"):
        return None
    try:
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None


def _gradient_banner(lines: list[str], start: str, end: str) -> Text:
    """字符画按行做上下渐变，颜色无法解析时退回单色。"""
    start_rgb = _hex_rgb(start)
    end_rgb = _hex_rgb(end)
    if start_rgb is None or end_rgb is None:
        return Text("\n".join(lines), style=start)
    total = max(1, len(lines) - 1)
    text = Text()
    for index, line in enumerate(lines):
        ratio = index / total
        rgb = tuple(
            round(a + (b - a) * ratio) for a, b in zip(start_rgb, end_rgb)
        )
        if index:
            text.append("\n")
        text.append(line, style="#{:02x}{:02x}{:02x}".format(*rgb))
    return text


def _render_boot_panel(
    stages: list[dict],
    frame: int = 0,
    theme=None,
    show_banner: bool = True,
):
    """启动面板：字符画 + 阶段列表 + 进度条 + 锁定提示。颜色跟随当前主题。"""
    from rich.console import Group
    from rich.table import Table

    primary = str(getattr(theme, "primary", "") or "#6d93ff")
    accent = str(getattr(theme, "accent", "") or primary)
    muted = str(getattr(theme, "secondary", "") or "#8a90a0")
    success = str(getattr(theme, "success", "") or "#5fb98a")
    warning = str(getattr(theme, "warning", "") or "#d9b26a")
    error = str(getattr(theme, "error", "") or "#e8788a")
    track = str(getattr(theme, "panel", "") or "#262a33")

    top: list = []
    if show_banner:
        top = [_gradient_banner(_BOOT_BANNER, primary, accent), Text("")]

    head = Text()
    head.append("Starting Alex", style="bold")
    head.append("   please wait", style=muted)

    lock = Text(
        "⌁ Input is locked until loading finishes",
        style=muted,
    )

    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=2)
    table.add_column(ratio=1)
    table.add_column(justify="right")

    finished = 0
    for stage in stages:
        status = str(stage.get("status", "idle"))
        label = str(stage.get("label", ""))
        detail = str(stage.get("detail", ""))
        if status == "done":
            dot, color, name_style, state_style = "●", success, muted, muted
            state = "ready"
            finished += 1
        elif status == "warn":
            dot, color, name_style, state_style = (
                "●", warning, warning, warning
            )
            state = detail or "warning"
            finished += 1
        elif status == "failed":
            dot, color, name_style, state_style = "●", error, error, error
            state = detail or "failed"
            finished += 1
        elif status == "active":
            dot = _BOOT_SPINNER[frame % len(_BOOT_SPINNER)]
            color, name_style, state_style = primary, "bold", primary
            state = "starting…"
        else:
            dot, color, name_style, state_style = "○", muted, muted, muted
            state = "waiting"
        table.add_row(
            Text(dot, style=color),
            Text(label, style=name_style),
            Text(state, style=state_style),
        )

    total = max(1, len(stages))
    filled = int(34 * finished / total)
    progress = Text()
    progress.append("█" * filled, style=primary)
    progress.append("░" * (34 - filled), style=track)
    progress.append(f"  {finished} of {total} · {finished * 100 // total}%", style=muted)

    return Group(*top, head, Text(""), table, Text(""), progress, Text(""), lock)


class LoadingOverlay(Vertical):
    """启动遮罩：覆盖整屏，加载完成前拦截所有交互。"""

    DEFAULT_CSS = """
    LoadingOverlay {
        layer: overlay;
        width: 100%;
        height: 100%;
        background: $background 78%;
        align: center middle;
    }
    LoadingOverlay #loading-panel {
        width: 74;
        max-width: 96%;
        height: auto;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="loading-panel")

    def update_stages(self, stages: list[dict], frame: int = 0) -> None:
        theme = getattr(self.app, "current_theme", None)
        size = self.app.size
        show_banner = size.width >= 76 and size.height >= 28
        self.query_one("#loading-panel", Static).update(
            _render_boot_panel(stages, frame, theme, show_banner)
        )
