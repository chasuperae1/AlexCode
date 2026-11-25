"""TUI 与后台 daemon 之间的本地 IPC 协议。

传输格式：长度受限的 JSON Lines（一行一条消息）。
会话文件记录 daemon 的端口与令牌，供客户端发现与重连。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 8 * 1024 * 1024
# 服务端事件环形缓冲的容量：断链重连后能补上的事件条数上限。
# 客户端据此判断 resync 是否够覆盖断开期间的缺口。
EVENT_BUFFER_SIZE = 2000
# 客户端心跳间隔：超过两个周期没有 pong 就按断链处理。
CLIENT_PING_SECONDS = 20.0

# 客户端 → 服务端
MSG_HELLO = "hello"
MSG_RPC = "rpc"
MSG_RESYNC = "resync"
MSG_PING = "ping"

# 服务端 → 客户端
MSG_HELLO_OK = "hello_ok"
MSG_SNAPSHOT = "snapshot"
MSG_EVENT = "event"
MSG_RPC_RESULT = "rpc_result"
MSG_PONG = "pong"
MSG_BYE = "bye"

# RPC 错误码
ERR_BUSY = "busy"
ERR_STALE_CONFIRM = "stale_confirm"
ERR_NOT_READY = "not_ready"
ERR_INTERNAL = "internal"


def source_stamp() -> str:
    """源码指纹：任一 alex/**/*.py 改动后，旧 daemon 会被识别为过期。"""
    root = Path(__file__).resolve().parent.parent
    latest = 0.0
    count = 0
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            latest = max(latest, path.stat().st_mtime)
            count += 1
        except OSError:
            continue
    return f"{count}-{int(latest)}"


@dataclass(frozen=True)
class SessionInfo:
    """daemon 的发现信息。"""

    port: int
    token: str
    pid: int
    started_at: float
    version: int = PROTOCOL_VERSION
    stamp: str = ""

    @classmethod
    def new(cls, port: int) -> "SessionInfo":
        return cls(
            port=port,
            token=secrets.token_hex(16),
            pid=os.getpid(),
            started_at=time.time(),
            stamp=source_stamp(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionInfo":
        known = {item.name for item in fields(cls)}
        return cls(**{key: data[key] for key in data if key in known})


def read_session_file(path: Path) -> SessionInfo | None:
    """读取会话文件；不存在或内容损坏时返回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        info = SessionInfo.from_dict(data)
    except (OSError, ValueError, TypeError):
        return None
    return info if info.port > 0 and info.token else None


def write_session_file(path: Path, info: SessionInfo) -> None:
    """原子写入会话文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(info.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    tmp.replace(path)


def clear_session_file(path: Path, pid: int | None = None) -> None:
    """删除会话文件；给出 pid 时只删除属于该进程的记录。"""
    info = read_session_file(path)
    if info is not None and pid is not None and info.pid != pid:
        return
    try:
        path.unlink()
    except FileNotFoundError:  # 记录已被其他进程清理
        pass


def encode(message: dict[str, Any]) -> bytes:
    """序列化一条消息（带换行）。"""
    return (
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


async def read_line(
    reader: asyncio.StreamReader,
    limit: int = MAX_LINE_BYTES,
) -> dict[str, Any] | None:
    """读取一条消息；EOF 返回 None，超限或格式错误抛 ValueError。"""
    try:
        raw = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError as exc:
        raise ValueError("message exceeds line limit") from exc
    if len(raw) > limit:
        raise ValueError("message exceeds line limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid message: {exc}") from exc


def rpc_ok(request_id: Any, result: Any = None) -> dict[str, Any]:
    return {"type": MSG_RPC_RESULT, "id": request_id, "ok": True, "result": result}


def rpc_error(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "type": MSG_RPC_RESULT,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }
