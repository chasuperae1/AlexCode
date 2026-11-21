"""可直接运行的 CLI 演示程序：`python -m termui.demo`。"""

from __future__ import annotations

import random

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header

from .app import TermUIApp
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

MENU = [
    MenuNode(
        "File",
        children=[
            MenuNode("New file", value="Ctrl+N"),
            MenuNode("Open file", value="Ctrl+O"),
            MenuNode("Recent", children=[
                MenuNode("main.py"),
                MenuNode("config.json"),
            ]),
        ],
        expanded=True,
    ),
    MenuNode(
        "View",
        children=[
            MenuNode("Theme", value="Ctrl+T"),
            MenuNode("Fullscreen", value="F11"),
        ],
    ),
    MenuNode(
        "Tools",
        children=[
            MenuNode("Task list"),
            MenuNode("Memory"),
        ],
    ),
]

ITEMS = [
    ListItem("Build", "compile + package", value="build"),
    ListItem("Run tests", "pytest -q", value="test"),
    ListItem("Lint", "ruff + mypy", value="lint"),
    ListItem("Deploy", "not configured", value="deploy", disabled=True),
    ListItem("Logs", "tail -f app.log", value="logs"),
] + [ListItem(f"Item {i:02d}", "scrolling demo", value=i) for i in range(1, 41)]


class DemoApp(TermUIApp):
    """演示所有组件的键盘交互、动画与主题切换。"""

    TITLE = "termui"
    SUB_TITLE = "Claude Code style terminal UI kit"

    BINDINGS = [
        Binding("ctrl+t", "cycle_theme", "Theme"),
        Binding("ctrl+r", "random_progress", "Random progress"),
        Binding("f1", "help", "Help"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        background: $background;
        color: $foreground;
    }
    Header {
        background: $panel;
        color: $foreground;
    }
    #body {
        layout: horizontal;
        height: 1fr;
    }
    #menu {
        width: 30;
        margin: 0 1 0 0;
    }
    #main {
        width: 1fr;
    }
    #list {
        height: 1fr;
    }
    #checks {
        height: auto;
        max-height: 9;
        margin: 1 0 0 0;
    }
    #progress {
        height: auto;
        margin: 1 0 0 0;
    }
    #bottom {
        height: auto;
        padding: 0 1;
    }
    #status {
        margin: 0;
    }
    #body.-narrow {
        layout: vertical;
    }
    #body.-narrow #menu {
        width: 1fr;
        height: 10;
        margin: 0 0 1 0;
    }
    #body.-narrow #checks {
        max-height: 7;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.progress_target = 0.0

    def compose(self):
        yield Header()
        with Horizontal(id="body"):
            yield HierarchyMenu(MENU, id="menu")
            with Vertical(id="main"):
                yield ScrollList(ITEMS, id="list")
                yield CheckboxGroup(
                    ["Bundle", "Push image", "Notify team"],
                    title="Extra steps",
                    id="checks",
                )
                yield ProgressBar(label="Build progress", id="progress")
        with Vertical(id="bottom"):
            yield PromptInput(
                placeholder="Type and press Enter; ↑↓ history, /help for commands",
                id="prompt",
            )
            yield StatusPanel(text="Ready · ↑↓ navigate · Tab focus · Ctrl+T theme · Ctrl+Q quit", id="status")
        yield Footer()

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#list", ScrollList).focus()
        self.set_interval(2.5, self._auto_progress)

    def on_resize(self, event: events.Resize) -> None:
        # 窄屏（<90 列）切为上下布局，宽屏保持左右分栏
        body = self.query_one("#body")
        body.set_class(event.size.width < 90, "-narrow")

    # ---------------------------------------------------------------- 进度动画
    def _auto_progress(self) -> None:
        if self.progress_target >= 100:
            self.progress_target = 0
        self.progress_target = min(100, self.progress_target + random.randint(5, 18))
        self.query_one("#progress", ProgressBar).advance_to(self.progress_target)

    def action_random_progress(self) -> None:
        self.progress_target = random.randint(0, 100)
        self.query_one("#progress", ProgressBar).advance_to(self.progress_target)
        self.query_one("#status", StatusPanel).notify(
            f"Progress jumped to {self.progress_target}%", level="info", timeout=2
        )

    def action_help(self) -> None:
        self.query_one("#status", StatusPanel).notify(
            "↑↓ move · ←→ expand/collapse · Space check · A all · Enter confirm · Ctrl+T theme",
            level="warning",
            timeout=5,
        )

    # ------------------------------------------------------------ 组件消息处理
    def on_scroll_list_selected(self, event: ScrollList.Selected) -> None:
        self.query_one("#status", StatusPanel).notify(
            f"Selected: {event.item.label}", level="success", timeout=3
        )

    def on_checkbox_group_selection_changed(
        self, event: CheckboxGroup.SelectionChanged
    ) -> None:
        self.query_one("#status", StatusPanel).notify(
            f"Checked {len(event.values)}: {', '.join(map(str, event.values)) or 'none'}",
            level="info",
        )

    def on_checkbox_group_submitted(self, event: CheckboxGroup.Submitted) -> None:
        self.query_one("#status", StatusPanel).notify(
            f"Submitted extra steps: {event.values or 'none'}", level="success", timeout=3
        )

    def on_hierarchy_menu_activated(self, event: HierarchyMenu.Activated) -> None:
        self.query_one("#status", StatusPanel).notify(
            "Open: " + " / ".join(event.path), level="success", timeout=3
        )

    def on_prompt_input_rejected(self, event: PromptInput.Rejected) -> None:
        self.query_one("#status", StatusPanel).notify(
            f"Invalid input: {event.reason}", level="error", timeout=4
        )

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        value = event.value.strip()
        status = self.query_one("#status", StatusPanel)
        if not value:
            return
        if value in {"/theme", "theme"}:
            self.action_cycle_theme()
        elif value in {"/expand", "expand"}:
            self.query_one("#menu", HierarchyMenu).expand_all()
            status.notify("Expanded all levels", level="info", timeout=2)
        elif value in {"/collapse", "collapse"}:
            self.query_one("#menu", HierarchyMenu).collapse_all()
            status.notify("Collapsed all levels", level="info", timeout=2)
        elif value in {"/help", "help"}:
            self.action_help()
        elif value == "/clear":
            self.query_one("#list", ScrollList).set_items([])
            status.notify("List cleared", level="warning", timeout=2)
        else:
            scroll_list = self.query_one("#list", ScrollList)
            scroll_list.set_items(
                [ListItem(value, "from input", value=value), *scroll_list.items]
            )
            status.notify(f"Inserted item: {value}", level="success", timeout=2)


def main() -> None:
    DemoApp().run()


if __name__ == "__main__":
    main()
