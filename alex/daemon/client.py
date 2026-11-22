"""TUI 与 AgentSession 之间的连接层。

- ``LocalLink``：同进程直连（``--no-daemon``）
- ``DaemonClient``：连接后台 daemon（默认）
- ``ensure_daemon``：复用已有 daemon，否则拉起一个
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from alex.daemon.protocol import (
    MAX_LINE_BYTES,
    MSG_BYE,
    MSG_EVENT,
    MSG_HELLO,
    MSG_HELLO_OK,
    MSG_RPC,
    MSG_SNAPSHOT,
    PROTOCOL_VERSION,
    SessionInfo,
    encode,
    read_line,
    read_session_file,
    source_stamp,
)

EventHandler = Callable[[str, dict[str, Any]], None]


class LinkError(Exception):
    """连接失败。"""


class LocalLink:
    """同进程连接：事件直接回调，调用直接 await 会话方法。"""

    def __init__(self, session) -> None:
        self.session = session
        self._handler: EventHandler | None = None
        session.emit = self._emit

    @classmethod
    def create(cls, builder=None) -> "LocalLink":
        from alex.session import AgentSession

        return cls(AgentSession(lambda kind, data: None, builder=builder))

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self._handler is not None:
            self._handler(kind, data)

    def on_event(self, handler: EventHandler) -> None:
        self._handler = handler

    def _dispatch(self, kind: str, data: dict[str, Any]) -> None:
        self._emit(kind, data)

    async def startup(self) -> None:
        await self.session.initialize()
        self._dispatch("snapshot", self.session.snapshot())

    async def rpc(self, method: str, **params: Any) -> Any:
        handler = getattr(self.session, method, None)
        if handler is None:
            raise LinkError(f"unknown method: {method}")
        result = handler(**params)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def close(self) -> None:
        await self.session.close()


class DaemonClient:
    """后台 daemon 的 JSON Lines 客户端。"""

    def __init__(self, reader, writer) -> None:
        self._reader = reader
        self._writer = writer
        self._handler: EventHandler | None = None
        self._close_handler: Callable[[], None] | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._last_seq = -1
        self._next_id = 0
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    @classmethod
    async def connect(cls, info: SessionInfo, timeout: float = 8.0) -> "DaemonClient":
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    "127.0.0.1", info.port, limit=MAX_LINE_BYTES
                ),
                timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise LinkError(f"cannot connect to daemon: {exc}") from exc
        writer.write(
            encode(
                {
                    "type": MSG_HELLO,
                    "v": PROTOCOL_VERSION,
                    "token": info.token,
                    "pid": os.getpid(),
                }
            )
        )
        await writer.drain()
        # daemon 事件循环被阻塞时可能一直不回 HELLO，必须带超时
        try:
            reply = await asyncio.wait_for(read_line(reader), timeout)
        except (asyncio.TimeoutError, ValueError) as exc:
            writer.close()
            raise LinkError(f"daemon handshake timed out: {exc}") from exc
        if reply is None or reply.get("type") != MSG_HELLO_OK:
            writer.close()
            raise LinkError("daemon rejected the connection")
        client = cls(reader, writer)
        snapshot = await asyncio.wait_for(read_line(reader), timeout)
        if snapshot is None or snapshot.get("type") != MSG_SNAPSHOT:
            writer.close()
            raise LinkError("daemon did not send a snapshot")
        client._last_seq = int(snapshot.get("last_seq", -1))
        client._initial_snapshot = snapshot
        client._reader_task = asyncio.create_task(client._read_loop())
        return client

    @property
    def initial_snapshot(self) -> dict[str, Any]:
        return self._initial_snapshot

    def on_event(self, handler: EventHandler) -> None:
        self._handler = handler

    def on_close(self, handler: Callable[[], None]) -> None:
        self._close_handler = handler

    def _dispatch(self, kind: str, data: dict[str, Any]) -> None:
        if self._handler is None:
            return
        try:
            self._handler(kind, data)
        except Exception as exc:
            # UI 渲染异常不能拖垮读循环，否则会被误判成 daemon 断线
            print(
                f"[alex] event handler failed ({kind}): {exc!r}",
                file=sys.stderr,
            )

    async def startup(self) -> None:
        """daemon 已初始化，握手时的 snapshot 即当前状态。"""
        payload = dict(self._initial_snapshot)
        payload.pop("type", None)
        payload["last_seq"] = self._last_seq
        self._dispatch("snapshot", payload)

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await read_line(self._reader)
                if message is None or not self._handle_message(message):
                    break
        except asyncio.CancelledError:  # 读循环被取消属正常关闭
            pass
        except (OSError, ValueError) as exc:
            print(f"[alex] daemon link ended: {exc!r}", file=sys.stderr)
        except Exception as exc:
            print(f"[alex] daemon link crashed: {exc!r}", file=sys.stderr)
        finally:
            self._close_link()

    def _handle_message(self, message: dict) -> bool:
        """分发一条 daemon 消息；返回 False 表示读循环该结束了。"""
        kind = message.get("type")
        if kind == MSG_EVENT:
            self._apply_event(message)
        elif kind == "rpc_result":
            self._resolve_rpc(message)
        elif kind == MSG_BYE:
            return False
        return True

    def _apply_event(self, message: dict) -> None:
        seq = int(message.get("seq", -1))
        if seq <= self._last_seq:
            return
        self._last_seq = seq
        self._dispatch(
            str(message.get("event_type", "")), message.get("data") or {}
        )

    def _resolve_rpc(self, message: dict) -> None:
        future = self._pending.pop(message.get("id"), None)
        if future is None or future.done():
            return
        if message.get("ok"):
            future.set_result(message.get("result"))
            return
        error = message.get("error") or {}
        future.set_exception(
            LinkError(
                f"{error.get('code', 'error')}: {error.get('message', '')}"
            )
        )

    def _close_link(self) -> None:
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LinkError("connection closed"))
        self._pending.clear()
        if self._close_handler is not None:
            self._close_handler()

    async def rpc(self, method: str, **params: Any) -> Any:
        if self._closed:
            raise LinkError("connection closed")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._writer.write(
                encode(
                    {
                        "type": MSG_RPC,
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
            )
            await self._writer.drain()
        except OSError as exc:
            self._pending.pop(request_id, None)
            raise LinkError(f"send failed: {exc}") from exc
        return await future

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:  # 取消后台读任务，异常属正常收尾
                pass
            except Exception:  # 读任务退出异常不影响关闭
                pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # 连接已断开，关闭失败可忽略
            pass


def _spawn_daemon(root: Path) -> None:
    """以脱离控制台的方式拉起 daemon，关掉 cmd 不影响它。"""
    log_path = root / "data" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab", buffering=0)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, "-m", "alex.daemon", "--root", str(root)],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=flags,
        close_fds=True,
    )


def prewarm_daemon(root: Path) -> None:
    """界面启动前先拉起 daemon：它的导入与初始化可与 TUI 启动并行。"""
    if read_session_file(root / "data" / "daemon.json") is None:
        _spawn_daemon(root)


def _is_stale(info: SessionInfo) -> bool:
    """daemon 由旧源码启动时拒绝复用，避免界面显示过期数据。"""
    return info.stamp != source_stamp()


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def _retire_daemon(info: SessionInfo) -> None:
    """请旧 daemon 退出；卡死或被旧代码破坏时直接结束进程。"""
    writer = None
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", info.port, limit=MAX_LINE_BYTES
        )
        writer.write(
            encode(
                {
                    "type": MSG_HELLO,
                    "token": info.token,
                    "v": PROTOCOL_VERSION,
                }
            )
        )
        await writer.drain()
        await asyncio.wait_for(read_line(reader), 3)
        writer.write(
            encode(
                {
                    "type": MSG_RPC,
                    "id": 1,
                    "method": "shutdown",
                    "params": {},
                }
            )
        )
        await writer.drain()
        await asyncio.wait_for(read_line(reader), 3)
    except Exception as exc:  # 旧 daemon 无握手响应：继续走强制结束分支
        print(f"[alex] retire handshake failed: {exc!r}", file=sys.stderr)
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # 旧连接关闭失败不影响退役流程
                pass
    # 旧 daemon 若已卡死在协议层，优雅退出拿不到响应，直接结束进程
    if _pid_alive(info.pid):
        try:
            os.kill(info.pid, signal.SIGTERM)
        except OSError:  # 进程已提前退出
            pass
        for _ in range(20):
            if not _pid_alive(info.pid):
                break
            await asyncio.sleep(0.1)


async def ensure_daemon(
    root: Path, timeout: float = 25.0
) -> DaemonClient:
    """连接已有 daemon；源码已更新或没有则拉起新实例。"""
    session_path = root / "data" / "daemon.json"
    info = read_session_file(session_path)
    if info is not None and _is_stale(info):
        await _retire_daemon(info)
        info = None
    if info is not None:
        try:
            return await DaemonClient.connect(info)
        except LinkError as exc:  # 旧 daemon 已连不上：记一笔后拉起新的
            print(f"[alex] reconnect failed, spawning daemon: {exc!r}", file=sys.stderr)

    _spawn_daemon(root)
    deadline = time.monotonic() + timeout
    last_spawn = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.2)
        info = read_session_file(session_path)
        if info is None:
            # 单实例锁可能被尚未退出的旧 daemon 占着，新实例会立即退出；
            # 一段时间仍没有会话文件就再拉一次，避免空等到超时
            if time.monotonic() - last_spawn >= 5.0:
                _spawn_daemon(root)
                last_spawn = time.monotonic()
            continue
        try:
            return await DaemonClient.connect(info)
        except LinkError:
            continue
    raise LinkError("daemon did not start; see data/daemon.log")
