"""网页搜索与跟踪本地后端。"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from agentscope.message import TextBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import (
    ExecResult,
    LocalBackend,
    ToolBase,
    ToolChunk,
)
from agentscope.tool._response import ToolResultState

from alex.core.permissions import is_locked_skill_path

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AlexAgent/1.0"


def _fetch(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _subprocess_kwargs() -> dict[str, Any]:
    """Platform flags: hide the console window, isolate the process group."""
    if os.name == "nt":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
            ),
        }
    return {"start_new_session": True}


class TrackedLocalBackend(LocalBackend):
    """LocalBackend that keeps child process handles so they can be killed.

    The framework backend runs subprocesses internally but does not expose the
    handle. This subclass mirrors its behavior while tracking each process,
    which lets ``Esc Esc`` terminate a running command and its children.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._processes: set[asyncio.subprocess.Process] = set()

    @property
    def active_count(self) -> int:
        return len(self._processes)

    # 内置只读技能：写/删一律拒绝，即使源文件被找到也改不动
    async def write_file(self, path: str, data: bytes) -> None:
        if is_locked_skill_path(path):
            raise PermissionError("alexcode is a built-in read-only skill")
        await super().write_file(path, data)

    async def write_stream(self, path: str, stream) -> None:
        if is_locked_skill_path(path):
            raise PermissionError("alexcode is a built-in read-only skill")
        await super().write_stream(path, stream)

    async def delete_path(self, path: str) -> None:
        if is_locked_skill_path(path):
            raise PermissionError("alexcode is a built-in read-only skill")
        await super().delete_path(path)

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                **_subprocess_kwargs(),
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            return ExecResult(
                exit_code=127,
                stdout=b"",
                stderr=str(exc).encode("utf-8"),
            )

        self._processes.add(process)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._terminate(process)
            await process.communicate()
            return ExecResult(exit_code=-1, stdout=b"", stderr=b"timed out")
        except asyncio.CancelledError:
            await self._terminate(process)
            raise
        finally:
            self._processes.discard(process)

        return ExecResult(
            exit_code=(
                process.returncode if process.returncode is not None else 0
            ),
            stdout=stdout,
            stderr=stderr,
        )

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(process.pid),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    **_subprocess_kwargs(),
                )
                try:
                    await asyncio.wait_for(killer.wait(), timeout=3)
                except asyncio.TimeoutError:
                    killer.kill()
                    try:
                        await asyncio.wait_for(killer.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        pass
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=1)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        except ProcessLookupError:
            pass

    async def kill_all(self) -> None:
        """Kill every tracked child process tree."""
        processes = list(self._processes)
        if processes:
            await asyncio.gather(
                *(self._terminate(process) for process in processes),
                return_exceptions=True,
            )


class WebSearch(ToolBase):
    """无需 API Key 的网页搜索（Bing RSS 结果）。"""

    name: str = "WebSearch"
    description: str = """Searches the web and returns titles, URLs and snippets.

No API key is required. Results come from Bing's RSS endpoint, so they are
plain links and summaries rather than full page content.

Usage:
- Use it when you need current information, docs, or links you don't know.
- Query with concrete keywords; Chinese and English both work.
- To read a specific result, fetch its URL with Cmd (curl.exe -s URL).
- Treat snippets as hints; verify important facts by opening the source.
"""
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keywords."},
            "count": {
                "type": "integer",
                "description": "Number of results (1-10).",
                "default": 8,
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["query"],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    is_external_tool: bool = False
    is_state_injected: bool = False

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.PASSTHROUGH,
            message="WebSearch is read-only network access.",
        )

    async def call(self, query: str, count: int = 8) -> ToolChunk:
        count = max(1, min(int(count), 10))
        encoded = urllib.parse.quote_plus(query)
        urls = [
            f"https://cn.bing.com/search?format=rss&q={encoded}&count={count}",
            f"https://www.bing.com/search?format=rss&q={encoded}&count={count}",
        ]
        last_error = ""
        for url in urls:
            try:
                xml_text = await asyncio.to_thread(_fetch, url, 15.0)
                items = self._parse(xml_text, count)
                if items:
                    return ToolChunk(
                        content=[TextBlock(text=self._format(query, items))],
                    )
                last_error = "no results returned"
            except Exception as exc:  # 网络错误、超时、解析失败
                last_error = str(exc)
        return ToolChunk(
            content=[
                TextBlock(
                    text=(
                        f"WebSearch failed: {last_error}. "
                        "Use curl.exe to fetch a known URL instead."
                    )
                )
            ],
            state=ToolResultState.ERROR,
        )

    @staticmethod
    def _parse(xml_text: str, count: int) -> list[dict[str, str]]:
        root = ET.fromstring(xml_text)
        results = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = item.findtext("description") or ""
            desc = re.sub(r"<[^>]+>", "", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            if title and link:
                results.append(
                    {"title": title, "link": link, "desc": desc[:200]}
                )
            if len(results) >= count:
                break
        return results

    @staticmethod
    def _format(query: str, items: list[dict[str, str]]) -> str:
        lines = [f"Search: {query} ({len(items)} results)"]
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {item['title']}\n   {item['link']}")
            if item["desc"]:
                lines.append(f"   {item['desc']}")
        return "\n".join(lines)
