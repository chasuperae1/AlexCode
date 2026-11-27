"""tui 拆出来的常量：标识符、尺寸上限、启动页文案等。"""

from __future__ import annotations

import re

from alex.config import ROOT


LOCAL_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "PowerShell", "Bash"}


DEBUG_KINDS = {"think", "tool", "mcp", "skill", "rule", "result", "error"}


USER = "user"


ASSISTANT = "assistant"


_IMAGE_TOKEN = re.compile(r"\[image-\d+\]")


PROMPT_PLACEHOLDER = "Message…  Enter to send · Shift+Enter newline · Ctrl+V / F6 image"


QUEUE_CARD_LIMIT = 10


UI_STATE_PATH = ROOT / "data" / "tui.json"


_TDC_MAX_LINES = 11


_BOX_START = set("┌╭╔")


_BOX_END = set("└╰╚")


_BOX_MID = set("├╞╟")


_BOX_V = set("│║")


_BOOT_SPINNER = "◐◓◑◒"


_BOOT_BANNER = [
    "   █████╗ ██╗     ███████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗",
    "  ██╔══██╗██║     ██╔════╝╚██╗██╔╝██╔════╝██╔═══██╗██╔══██╗██╔════╝",
    "  ███████║██║     █████╗   ╚███╔╝ ██║     ██║   ██║██║  ██║█████╗",
    "  ██╔══██║██║     ██╔══╝   ██╔██╗ ██║     ██║   ██║██║  ██║██╔══╝",
    "  ██║  ██║███████╗███████╗██╔╝ ██╗╚██████╗╚██████╔╝██████╔╝███████╗",
    "  ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝",
]


# daemon 是外层连接门：必须先连上后台会话，skills/session/model/mcp
# 才有机会上报状态。面板按真实执行顺序排列，避免首个 active 出现在第三行。
_BOOT_ORDER = ("daemon", "skills", "session", "model", "mcp")


_BOOT_LABELS = {
    "skills": "Skills & rules",
    "session": "Session history",
    "daemon": "Background daemon",
    "model": "Model & tools",
    "mcp": "MCP servers",
}
