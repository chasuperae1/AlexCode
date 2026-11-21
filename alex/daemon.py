"""后台 daemon：持有 AgentSession，接受本地 TUI 客户端连接。

启动后先绑定端口并写出 data/daemon.json，再在后台初始化 Agent；
关掉客户端不影响任务，任务结束且无客户端连接时自动退出。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import hmac
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import ROOT
from .protocol import (
    MAX_LINE_BYTES,
    MSG_BYE,
    MSG_EVENT,
    MSG_HELLO,
    MSG_HELLO_OK,
    MSG_PING,
    MSG_RESYNC,
    MSG_RPC,
    MSG_SNAPSHOT,
    PROTOCOL_VERSION,
    SessionInfo,
    clear_session_file,
    encode,
    read_line,
    rpc_error,
    rpc_ok,
    write_session_file,
)
from .session import AgentSession, SessionError

PERSIST_EVENTS = {
    "exchange_start",
    "tool_start",
    "tool_result",
    "reply_end",
    "compaction",
    "history_reset",
    "confirm_request",
    "confirm_end",
    "ask_request",
    "ask_end",
    "changes",
    "notice",
}
BUFFER_SIZE = 2000
EXIT_GRACE = 300.0  # 空闲后保活 5 分钟：再次打开窗口无需重启 daemon
IDLE_NO_CLIENT = 60.0


class _Client:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.closed = False

    def send(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self.writer.write(encode(message))
        except (OSError, RuntimeError):
            self.closed = True

    def close(self) -> None:
        self.closed = True
        try:
            self.writer.close()
        except Exception:
            pass


class Daemon:
    def __init__(self, root: Path, session: AgentSession | None = None) -> None:
        self.root = root
        self.session = session or AgentSession(self._emit)
        if session is not None:
            session.emit = self._emit
        self.info: SessionInfo | None = None
        self._server: asyncio.AbstractServer | None = None
        self._clients: list[_Client] = []
        self._seq = 0
        self._buffer: deque[dict[str, Any]] = deque(maxlen=BUFFER_SIZE)
        self._busy = False
        self._had_client = False
        self._shutdown = asyncio.Event()
        self._exit_task: asyncio.Task | None = None
        self._log_file = None

    # ------------------------------------------------------------ 事件广播
    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        self._seq += 1
        message = {
            "type": MSG_EVENT,
            "seq": self._seq,
            "ts": time.time(),
            "event_type": kind,
            "data": data,
        }
        self._buffer.append(message)
        if kind in PERSIST_EVENTS:
            self._write_log(message)
        if kind == "status":
            self._busy = bool(data.get("busy"))
        for client in list(self._clients):
            client.send(message)
        if kind == "status":
            self._maybe_exit()
        if kind in {"text_delta", "thinking_delta", "reply_end"}:
            self._schedule_drain()

    def _schedule_drain(self) -> None:
        """异步 drain 所有客户端的写缓冲，避免事件密集期内存膨胀。"""
        for client in list(self._clients):
            if client.closed:
                continue
            try:
                writer = client.writer
                if writer.transport and writer.transport.get_write_buffer_size() > 0:
                    asyncio.ensure_future(self._drain_client(writer))
            except Exception:
                pass

    @staticmethod
    async def _drain_client(writer: asyncio.StreamWriter) -> None:
        try:
            await writer.drain()
        except (OSError, ConnectionError):
            pass

    def _write_log(self, message: dict[str, Any]) -> None:
        if self._log_file is None:
            return
        try:
            line = json.dumps(message, ensure_ascii=False) + "\n"
        except (ValueError, TypeError):
            return
        asyncio.ensure_future(self._async_write_log(line))

    async def _async_write_log(self, line: str) -> None:
        if self._log_file is None:
            return
        try:
            await asyncio.to_thread(self._sync_write_log, line)
        except Exception:
            pass

    def _sync_write_log(self, line: str) -> None:
        if self._log_file is None:
            return
        self._log_file.write(line)
        self._log_file.flush()

    def _maybe_exit(self) -> None:
        if self._shutdown.is_set():
            return
        if self._clients or self._busy or not self._had_client:
            return
        self._schedule_exit(EXIT_GRACE)

    def _schedule_exit(self, delay: float) -> None:
        if self._exit_task is not None and not self._exit_task.done():
            self._exit_task.cancel()
        self._exit_task = asyncio.create_task(self._exit_after(delay))

    async def _exit_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._clients or self._busy:
            return
        await self.shutdown("task_finished")

    async def shutdown(self, reason: str) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        for client in list(self._clients):
            client.send({"type": MSG_BYE, "reason": reason})
            client.close()
        self._clients.clear()
        # 清理必须能在有限时间内结束，否则关窗口后 daemon 会残留
        try:
            await asyncio.wait_for(self.session.close(), timeout=10)
        except (asyncio.TimeoutError, Exception):
            pass
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
        if self.info is not None:
            clear_session_file(self.root / "data" / "daemon.json", pid=os.getpid())

    # ------------------------------------------------------------ 客户端处理
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client = _Client(writer)
        try:
            hello = await asyncio.wait_for(read_line(reader), timeout=5.0)
        except (asyncio.TimeoutError, ValueError):
            client.close()
            return
        if (
            hello is None
            or hello.get("type") != MSG_HELLO
            or self.info is None
            or not hmac.compare_digest(
                str(hello.get("token", "")), self.info.token
            )
        ):
            client.close()
            return

        # 允许多个窗口同时连接（事件广播），避免互相踢下线
        self._clients.append(client)
        self._had_client = True
        if self._exit_task is not None and not self._exit_task.done():
            self._exit_task.cancel()

        client.send(
            {
                "type": MSG_HELLO_OK,
                "v": PROTOCOL_VERSION,
                "session_id": self.info.token[:8],
                "pid": os.getpid(),
            }
        )
        snapshot = self.session.snapshot()
        snapshot.update({"type": MSG_SNAPSHOT, "last_seq": self._seq})
        client.send(snapshot)

        try:
            while True:
                message = await read_line(reader)
                if message is None:
                    break
                await self._handle_message(client, message)
        except (OSError, ValueError, asyncio.CancelledError):
            pass
        finally:
            try:
                if client in self._clients:
                    self._clients.remove(client)
            except Exception:
                pass
            client.close()
            self._maybe_exit()

    async def _handle_message(
        self, client: _Client, message: dict[str, Any]
    ) -> None:
        kind = message.get("type")
        if kind == MSG_PING:
            client.send({"type": "pong"})
            return
        if kind == MSG_RESYNC:
            after = int(message.get("after_seq", 0))
            for item in self._buffer:
                if int(item["seq"]) > after:
                    client.send(item)
            return
        if kind != MSG_RPC:
            return

        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if method == "shutdown":
            client.send(rpc_ok(request_id, {"ok": True}))
            asyncio.create_task(self.shutdown("client requested restart"))
            return
        handler = getattr(self.session, method, None)
        if handler is None:
            client.send(rpc_error(request_id, "internal", f"unknown method: {method}"))
            return
        try:
            result = handler(**params)
            if asyncio.iscoroutine(result):
                result = await result
            client.send(rpc_ok(request_id, result))
        except SessionError as exc:
            client.send(rpc_error(request_id, exc.code, str(exc)))
        except TypeError as exc:
            client.send(rpc_error(request_id, "internal", f"bad params: {exc}"))
        except Exception as exc:
            client.send(rpc_error(request_id, "internal", str(exc)))

    # ------------------------------------------------------------ 主流程
    async def run(self) -> None:
        log_dir = self.root / "data" / "sessions" / "events"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._log_file = open(
            log_dir / f"{stamp}-{os.getpid()}.jsonl", "a", encoding="utf-8"
        )

        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", 0, limit=MAX_LINE_BYTES
        )
        port = self._server.sockets[0].getsockname()[1]
        self.info = SessionInfo.new(port)
        write_session_file(self.root / "data" / "daemon.json", self.info)

        init_task = asyncio.create_task(self.session.initialize())
        watchdog = asyncio.create_task(self._idle_watchdog())
        try:
            await init_task
            if self.session.phase == "failed":
                await self.shutdown("init_failed")
            await self._shutdown.wait()
        finally:
            watchdog.cancel()
            if self._server is not None:
                self._server.close()
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except Exception:
                    pass
            if self.info is not None:
                clear_session_file(
                    self.root / "data" / "daemon.json", pid=os.getpid()
                )

    async def _idle_watchdog(self) -> None:
        """长时间没有任何客户端连接时退出，避免拉起失败后留下僵尸进程。"""
        await asyncio.sleep(IDLE_NO_CLIENT)
        if not self._had_client and not self._clients:
            await self.shutdown("no_client")


def _acquire_lock(root: Path):
    """同机同项目只允许一个 daemon。"""
    name = "Local\\AlexDaemon-" + hashlib.sha1(
        str(root).encode("utf-8")
    ).hexdigest()[:16]
    if os.name == "nt":
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return None
        return handle
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="alex.daemon")
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    log_path = root / "data" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stderr = log_file
    # 这里已是真实文件句柄，MCP 的 stdio 客户端在 build_mcps 时再导入即可，
    # 不必提前预热阻塞 daemon.json 的发布。

    lock = _acquire_lock(root)
    if lock is None:
        print("[alex] daemon lock held by another instance; exiting", flush=True)
        return

    daemon = Daemon(root)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
