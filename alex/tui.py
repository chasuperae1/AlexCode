"""Alex 的终端聊天界面：复用 termui 组件库 + AgentScope 流式事件。

运行：``python -m alex.tui``
"""

from __future__ import annotations

import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import OptionList, Static

from termui import TermUIApp
from termui.widgets import SPINNER_FRAMES

from alex.config import CONFIG, ROOT, workspace_path
from alex.core.snapshots import SnapshotStore
from alex.ui.activity import ActivityTracker
from alex.ui.app_chat import ChatMixin
from alex.ui.app_chrome import ChromeMixin
from alex.ui.app_events import EventsMixin
from alex.ui.app_input import InputMixin
from alex.ui.app_startup import StartupMixin
from alex.ui.app_style import APP_CSS
from alex.ui.boot import LoadingOverlay
from alex.ui.constants import ASSISTANT, PROMPT_PLACEHOLDER, USER  # noqa: F401
from alex.ui.panels import ChatLog, GoalCard, QueueCard
from alex.ui.sources import SourceState, load_sources
from alex.ui.state import _load_ui_state
from alex.ui.themes import DEFAULT_THEME, THEMES
from alex.ui.view_state import ChatViewState
from alex.ui.widgets import PromptInput

# 以下符号不再被 tui.py 直接使用，但仍是 ``alex.tui`` 的公开导入面：
# 测试通过 ``monkeypatch.setattr(tui, ...)`` 打桩，UI mixin 经 facade 读取。
from textual.widgets import Collapsible  # noqa: F401
from alex.core.images import grab_clipboard_image  # noqa: F401
from alex.daemon.client import LinkError, ensure_daemon, prewarm_daemon  # noqa: F401
from alex.screens.ask_choice import AskChoice  # noqa: F401
from alex.screens.message_actions import MessageActionsScreen  # noqa: F401
from alex.settings import SettingsScreen  # noqa: F401
from alex.ui.markdown import MarkdownStatic, markdown_to_text  # noqa: F401
from alex.ui.widgets import (  # noqa: F401
    ChangesSummary,
    InlineDiffCard,
    ToolDiffCard,
)


class AlexApp(StartupMixin, EventsMixin, ChatMixin, ChromeMixin, InputMixin, TermUIApp):
    """Alex 对话界面。"""

    TITLE = "Alex"
    SUB_TITLE = "AgentScope · termui"

    COMMANDS = [
        ("/model", "Switch model"),
        ("/permission", "Choose permission mode"),
        ("/settings", "Open settings"),
        ("/cd", "Browse / change workspace"),
        ("/init memory", "Clear all memory"),
        ("/reload", "Reload config and tools"),
        ("/image", "Send an image file"),
        ("/compact", "Compress the context now"),
        ("/queue", "Run independent tasks in sequence"),
        ("/goal", "Run until the goal passes verification"),
        ("/clear", "Clear the session"),
        ("/theme", "Choose the theme"),
        ("/help", "Show help"),
        ("/exit", "Quit Alex"),
    ]

    BINDINGS = [
        ("ctrl+t", "cycle_theme", "Theme"),
        ("ctrl+l", "clear_screen", "Clear"),
        ("ctrl+o", "toggle_debug", "Debug log"),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+v", "paste_image", "Paste image"),
        # cmd / Windows Terminal 会把 Ctrl+V 当成终端自己的粘贴，应用收不到，
        # 留终端不占用的键给截图粘贴用
        ("alt+v", "paste_image", "Paste image"),
        ("f6", "paste_image", "Paste image"),
        Binding("up", "palette_up", "Previous command", priority=True, show=False),
        Binding("down", "palette_down", "Next command", priority=True, show=False),
        Binding("enter", "palette_accept", "Run command", priority=True, show=False),
        Binding("tab", "palette_complete", "Complete command", priority=True, show=False),
        # 队列确认阶段复用方向键 / 回车，不影响命令面板
        Binding("up", "queue_scroll_up", "Scroll queue", priority=True, show=False),
        Binding("down", "queue_scroll_down", "Scroll queue", priority=True, show=False),
        Binding("enter", "queue_start", "Start queue", priority=True, show=False),
        Binding("enter", "goal_confirm", "Confirm goal", priority=True, show=False),
        Binding("m", "goal_edit", "Edit goal", priority=True, show=False),
        Binding("escape", "escape", "Stop", priority=True, show=False),
    ]

    CSS = APP_CSS

    def __init__(self) -> None:
        super().__init__()
        self._init_stream_state()
        self._init_runtime_state()
        self._init_interaction_state()
        self._init_queue_goal_state()
        self._init_settings_state()

    def _init_stream_state(self) -> None:
        # 会话状态的界面投影：历史、流式正文、思考与快照
        self.view = ChatViewState()
        self._tool_args: dict[str, str] = {}
        self._tool_names: dict[str, str] = {}
        self._stream_dirty = False
        self._thinking_ticks = 0
        self._stream_md = None

        # 当前正文块从 stream_text 的哪个位置开始（回答选择题后另起一块）
        self._stream_offset = 0

        # 本轮是否收到过正文增量（没有时在结束时用全文兜底）
        self._turn_text_streamed = False

        # 本轮已经挂出的工具级 diff 卡片数量，用来避免结束时再重复一张整轮大卡
        self._turn_diff_cards = 0

        # 简洁模式下当前这一轮的汇总卡
        self._concise_card = None

        # 思考独立折叠：每段思考一个 Collapsible；工具与子代理按顺序单行追加
        self._thinking_box = None
        self._thinking_static = None
        self._thinking_started = 0.0
        self._tool_rows: dict[str, Static] = {}

        # history_reset 未携带 checkpoints 时，等紧随其后的 checkpoints 事件再补渲染
        self._history_needs_checkpoints = False
        self._spinner_index = 0
        self._spinner_frames = SPINNER_FRAMES

        # 性能优化：缓存与节流
        self._chat_ref: ChatLog | None = None  # 缓存 ChatLog 引用
        self._live_runs = ActivityTracker()

    def _init_runtime_state(self) -> None:
        self.agent = None
        self.toolkit = None
        self.mcps = []
        self._mcp_tools_by_server: dict[str, list[str]] = {}
        self.link = None
        self._no_daemon = False
        self._busy = False
        self._backend = None

        # 权限门在后台初始化时创建（其依赖链约 2s，不想挡住首屏）
        self._permission_gate = None
        self._snapshots: SnapshotStore | None = None
        self._snapshot_warned = False
        self._meta_base = ""
        self._workspace = str(workspace_path())  # 跟随 daemon 推送实时更新
        self._sources: SourceState = load_sources()

    def _init_interaction_state(self) -> None:
        self._confirm_screen = None
        self._confirm_request_id = ""
        self._confirm_resolved = False
        self._confirm_future = None
        self._permission_card = None
        self._permission_payload = None
        self._permission_pending = False
        self._ask_widget = None
        self._ask_request_id = ""
        self._ask_payload: dict | None = None
        self._live_tool_data: dict[str, dict] = {}
        self._compacting_line = None
        self._palette_visible = False
        self._palette_matches: list[tuple[str, str]] = []
        self._activity = ""
        self._reply_worker = None
        self._rewinding = False
        self._stop_armed = False
        self._stop_timer = None
        self._activity_before_stop = ""
        self._stopping = False

        # 粘贴的截图：占位符 -> 本地文件
        self._pasted_images: dict[str, Path] = {}
        self._paste_seq = 0

    def _init_queue_goal_state(self) -> None:
        self._queue_confirming = False
        self._queue_armed = False
        self._queue_total = 0
        self._queue_card_widget: QueueCard | None = None
        self._goal_card_widget: GoalCard | None = None
        self._goal_confirming = False
        self._goal_editing = False
        self._goal_phase = ""
        self._verifier_mode = False

    def _init_settings_state(self) -> None:
        # 交互区显示模式：developer 逐条展开 / concise 整轮收进一行摘要
        self._display_mode = (
            "concise"
            if str(_load_ui_state().get("display_mode") or "") == "concise"
            else "developer"
        )

        self._boot: dict[str, dict[str, str]] = {
            "daemon": {
                "label": "Background daemon",
                "status": "idle",
                "detail": "",
            }
        }

        self._boot_frame = 0
        self._loading_visible = True

        # 设置页数据预取缓存
        self._settings_cache: dict | None = None
        self._model_catalog: dict[str, dict] = {}
        self._settings_spinner_frame = 0
        self._settings_spinner_timer = None


    # ------------------------------------------------------------------ 布局
    def compose(self) -> ComposeResult:
        with Vertical(id="main"):
            with Horizontal(id="topbar"):
                yield Static("", id="meta")
                yield Static("", id="tasks")
                yield Static("", id="settings-spinner")
                yield Static("Settings", id="settings-link", classes="source-link")
            yield ChatLog(id="chat")
            with Vertical(id="bottom"):
                yield OptionList(id="commands")
                with Horizontal(id="bottom-toolbar"):
                    yield Static("", id="progress-tag")
                    yield Static("", id="bottom-spacer")
                    yield Static("Jump to latest", id="jump")
                yield PromptInput(
                    id="prompt",
                    placeholder=PROMPT_PLACEHOLDER,
                )
            with Horizontal(id="statusbar"):
                yield Static("", id="workspace-line")
                yield Static("", id="task-status")
        yield LoadingOverlay(id="loading")

    async def on_mount(self) -> None:
        super().on_mount()
        for spec in THEMES.values():
            self.register_theme(spec.to_textual_theme())
        saved = str(_load_ui_state().get("theme") or CONFIG.get("theme") or "")
        self.theme = saved if saved in THEMES else DEFAULT_THEME
        self.set_interval(0.15, self._flush_stream)
        self.set_interval(0.2, self._tick_spinner)
        self.set_interval(1.0, self._tick_task_status)
        prompt = self.query_one("#prompt", PromptInput)
        prompt.focus()
        # 初始化（尤其 MCP 子进程）放在后台，界面先渲染出来
        prompt.disabled = True
        self._meta_base = "Starting…"
        self._refresh_meta_line()
        self._refresh_loading()
        self.run_worker(self._initialize(), exclusive=True)


def _acquire_instance_lock():
    """单实例锁：同一个工作区只允许一个 Alex 窗口。"""
    path = ROOT / "data" / "alex.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write("alex\n")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> None:
    import argparse

    lock = _acquire_instance_lock()
    if lock is None:
        print(
            "Alex is already running in another window. "
            "Close it before starting a new one.",
            flush=True,
        )
        raise SystemExit(1)

    parser = argparse.ArgumentParser(prog="alex.tui")
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="run the agent in this process instead of a background daemon",
    )
    args = parser.parse_args()

    # Textual 运行期间会把 sys.stdout/stderr 换成 _PrintCapture（fileno() == -1），
    # 而 MCP 的 stdio 客户端把导入时的 sys.stderr 作为子进程的 stderr。
    # 这里在启动前把它指向真实日志文件，并在此时预热导入，子进程才能正常拉起。
    log_path = ROOT / "data" / "alex.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stderr = log_file
    if args.no_daemon:
        import mcp.client.stdio  # noqa: F401  绑定有效的 stderr
    else:
        # daemon 模式下 MCP 跑在 daemon 进程里，TUI 不必预热导入（省约 1.4s）；
        # 先把 daemon 拉起来，让它的初始化与界面启动并行。
        prewarm_daemon(ROOT)

    app = AlexApp()
    app._no_daemon = args.no_daemon
    # Textual 接管全屏前先给出一行反馈，避免点击后黑屏一段时间像没反应
    print("Starting Alex…", flush=True)
    app.run()


if __name__ == "__main__":
    main()
