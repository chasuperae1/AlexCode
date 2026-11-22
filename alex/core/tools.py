"""Alex 自研工具：无需 API Key 的网页搜索，以及 cmd 命令执行。

框架未提供联网与 cmd 工具，这里按官方扩展点继承 ``ToolBase`` 实现，
不改动 agentscope 源码。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Awaitable, Callable

from agentscope.message import TextBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import (
    ExecResult,
    LocalBackend,
    ToolBase,
    ToolChunk,
)
from agentscope.tool._response import ToolResultState

from alex.config import locked_skill_dirs
from alex.core.permissions import is_dangerous_command

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AlexAgent/1.0"

# 命中这些写法就视为“要改动文件”，配合内置技能路径做拒绝判断
_WRITE_INTENT = re.compile(
    r"(?:>>?|\bdel\b|\berase\b|\bren(?:ame)?\b|\bmove\b|\bcopy\b|\bxcopy\b"
    r"|\battrib\b|\bicacls\b|\btakeown\b|\bmv\b|\brm\b|\bcp\b|\btee\b"
    r"|\bset-content\b|\bout-file\b|\bremove-item\b|\bnew-item\b)",
    re.IGNORECASE,
)


def is_locked_skill_path(path: str) -> bool:
    """路径是否位于内置只读技能目录内。"""
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for folder in locked_skill_dirs():
        try:
            target.relative_to(folder.resolve())
            return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


from alex.core.tools_web import TrackedLocalBackend, WebSearch


class Cmd(ToolBase):
    """通过 cmd.exe 执行 Windows 命令（不依赖 PowerShell）。"""

    name: str = "Cmd"
    description: str = """Executes a Windows cmd.exe command and returns its output.

Use this instead of PowerShell when PowerShell execution policy or
permissions may block commands. Each call runs in the configured working
directory; session state does not persist. Encoding is switched to UTF-8
(chcp 65001) so Chinese output is readable.

IMPORTANT: Prefer dedicated tools over shell for file work:
 - File search: Glob (NOT dir /s)
 - Content search: Grep (NOT findstr)
 - Read files: Read (NOT type)
 - Edit files: Edit
 - Write files: Write (NOT echo >)

Use Cmd for things only a shell can do: running programs, git, pip, curl.exe.
"""
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "cmd.exe command."},
            "description": {
                "type": "string",
                "description": "Short description of what the command does.",
                "default": "",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds (max 600000).",
                "default": 120000,
                "minimum": 1,
                "maximum": 600000,
            },
        },
        "required": ["command"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = False
    is_external_tool: bool = False
    is_state_injected: bool = False

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        backend=None,
    ) -> None:
        super().__init__()
        self._cwd = os.fspath(cwd) if cwd is not None else None
        self._backend = backend or TrackedLocalBackend()

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context,
    ) -> PermissionDecision:
        command = str(tool_input.get("command", ""))
        if "alexcode" in command.lower() and _WRITE_INTENT.search(command):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="alexcode is a built-in read-only skill.",
                decision_reason="Protected built-in skill",
                bypass_immune=True,
            )
        pattern = is_dangerous_command(command)
        if pattern:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=(
                    f"Permission required: command matches dangerous pattern "
                    f"'{pattern}'\n{command}"
                ),
                decision_reason="Safety check: dangerous command",
                bypass_immune=True,
            )
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Non-destructive command is allowed.",
            decision_reason="Command is not in the dangerous list",
        )

    async def call(
        self,
        command: str,
        description: str = "",
        timeout: int = 120000,
    ) -> ToolChunk:
        timeout_ms = max(1, min(int(timeout), 600000))
        script = f"chcp 65001>nul & {command}"
        try:
            result = await self._backend.exec_shell(
                ["cmd.exe", "/d", "/s", "/c", script],
                cwd=self._cwd,
                timeout=timeout_ms / 1000.0,
            )
        except Exception as exc:
            return ToolChunk(
                content=[TextBlock(text=f"Command failed: {command}\n{exc}")],
                state=ToolResultState.ERROR,
            )

        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if result.exit_code == -1 and result.stderr == b"timed out":
            return ToolChunk(
                content=[TextBlock(text=f"Command timed out ({timeout_ms}ms): {command}")],
                state=ToolResultState.ERROR,
            )
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        parts.append(f"[exit code] {result.exit_code}")
        return ToolChunk(
            content=[TextBlock(text="\n".join(parts))],
            state=(
                ToolResultState.SUCCESS
                if result.exit_code == 0
                else ToolResultState.ERROR
            ),
        )


AskCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class AskUser(ToolBase):
    """向用户展示一道选择题并等待回答。

    UI 侧的呈现方式由回调决定（TUI 内联卡片 / 终端编号选择），
    工具本身只负责校验参数和把回答转成模型可读的文本。
    """

    name: str = "AskUser"
    description: str = """Asks the user to choose from a short list of options and waits.

Use it when:
- several approaches are valid and the user's preference decides;
- a required detail is missing (budget, scope, target file);
- guessing would be wasteful or hard to undo.

Do NOT use it for permission-style yes/no checks (they have their own prompt)
or when the answer can be found with file or command tools.

Keep the question to one sentence and offer 2-6 concrete options. The user may
pick one, pick several when multi=true, type a custom answer, or skip. If they
skip, continue with the most reasonable default and state the assumption.
"""
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "One-sentence question.",
            },
            "options": {
                "type": "array",
                "description": "2-9 concrete choices.",
                "minItems": 2,
                "maxItems": 9,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "Short option label.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional one-line hint.",
                            "default": "",
                        },
                    },
                    "required": ["label"],
                },
            },
            "multi": {
                "type": "boolean",
                "description": "Allow selecting more than one option.",
                "default": False,
            },
        },
        "required": ["question", "options"],
    }
    is_concurrency_safe: bool = False
    is_read_only: bool = True
    is_external_tool: bool = False
    is_state_injected: bool = False

    def __init__(self, ask: AskCallback | None = None) -> None:
        super().__init__()
        self._ask = ask

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context,
    ) -> PermissionDecision:
        # 提问本身不修改任何东西，不需要走权限确认
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Interactive question; no permission required.",
            decision_reason="Asking the user a question",
        )

    async def call(
        self,
        question: str,
        options: list[Any],
        multi: bool = False,
    ) -> ToolChunk:
        question = str(question or "").strip()
        normalized = self._normalize(options)
        if not question:
            return self._error("`question` is required.")
        if len(normalized) < 2:
            return self._error("Provide at least 2 options.")

        if self._ask is None:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "No interactive UI is available, so the question was "
                            "skipped. Proceed with the most reasonable default and "
                            "state your assumption."
                        )
                    )
                ],
                state=ToolResultState.SUCCESS,
            )

        answer = await self._ask(
            {
                "question": question,
                "options": normalized,
                "multi": bool(multi),
                "allow_other": True,
            }
        )
        return ToolChunk(
            content=[TextBlock(text=self._format_answer(answer, multi))],
            state=ToolResultState.SUCCESS,
        )

    @staticmethod
    def _normalize(options: list[Any]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for raw in options or []:
            if isinstance(raw, str):
                label, description = raw, ""
            elif isinstance(raw, dict):
                label = str(raw.get("label", ""))
                description = str(raw.get("description", ""))
            else:
                continue
            label = label.strip()
            if label:
                items.append({"label": label, "description": description.strip()})
        return items[:9]

    @staticmethod
    def _format_answer(answer: dict[str, Any], multi: bool) -> str:
        images = [str(item) for item in answer.get("images", []) if str(item)]
        if answer.get("skipped"):
            text = (
                "User skipped the question. Proceed with the most reasonable "
                "default and clearly state your assumption."
            )
        else:
            other = str(answer.get("other", "")).strip()
            choices = [str(item) for item in answer.get("choices", []) if str(item)]
            if other:
                text = f'User answered with custom input: "{other}"'
            elif not choices:
                text = (
                    "User attached images as the answer."
                    if images
                    else (
                        "User submitted an empty selection. Pick the most "
                        "reasonable default and state your assumption."
                    )
                )
            elif multi or len(choices) > 1:
                joined = ", ".join(f'"{item}"' for item in choices)
                text = f"User selected {len(choices)} options: {joined}."
            else:
                text = f'User answered: "{choices[0]}"'
        if images:
            text += (
                "\n\nAttached images (use the Read tool on these paths):\n"
                + "\n".join(f"- {item}" for item in images)
            )
        return text

    @staticmethod
    def _error(message: str) -> ToolChunk:
        return ToolChunk(
            content=[TextBlock(text=message)],
            state=ToolResultState.ERROR,
        )

