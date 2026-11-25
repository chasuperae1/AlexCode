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

from alex.config import CONFIG, ROOT
from alex.daemon.protocol import (
    EVENT_BUFFER_SIZE,
    MAX_LINE_BYTES,
    MSG_BYE,
    MSG_EVENT,
    MSG_HELLO,
    MSG_HELLO_OK,
    MSG_PING,
    MSG_PONG,
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
from alex.session import AgentSession, SessionError

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
# 事件环形缓冲：断链重连后 resync 能补上的条数上限（协议两侧共用同一常量）
BUFFER_SIZE = EVENT_BUFFER_SIZE
EXIT_GRACE = 300.0  # 空闲后保活 5 分钟：再次打开窗口无需重启 daemon
IDLE_NO_CLIENT = 60.0
# 事件日志：单写协程用的队列深度，按天轮转并保留这么多天
LOG_QUEUE_SIZE = 4096
LOG_RETENTION_DAYS = 7
# 日志文件打不开之后隔多久再试一次（目录被外部删掉时靠它自愈）
LOG_RETRY_SECONDS = 5.0


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
        except Exception:  # 连接已断开，关闭异常可忽略
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
        self._log_dir: Path | None = None
        self._log_day = ""
        # 事件日志只由一个协程写：多线程并发写同一文件不保证行序
        self._log_queue: asyncio.Queue[str | None] | None = None
        self._log_task: asyncio.Task | None = None
        # 上一次成功落盘之后丢掉的事件数，以及最后一次丢的原因：
        # 恢复写盘时的第一条日志会先补一条 log_gap，缺口不会无声无息
        self._log_dropped = 0
        self._log_gap_reason = ""
        # 目录被外部删掉后 open 会一直失败：记失败的那天和下次重试时间，
        # 避免每条事件都重试 + 往 stderr 刷一行
        self._log_failed_day = ""
        self._log_retry_at = 0.0

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
            except Exception:  # transport 状态异常时跳过 drain
                pass

    @staticmethod
    async def _drain_client(writer: asyncio.StreamWriter) -> None:
        try:
            await writer.drain()
        except (OSError, ConnectionError):  # 客户端已断开，drain 失败可忽略
            pass

    def _write_log(self, message: dict[str, Any]) -> None:
        """事件日志入队即返回；真正的落盘由唯一的写协程按序完成。"""
        if self._log_queue is None:
            return
        try:
            line = json.dumps(message, ensure_ascii=False) + "\n"
        except (ValueError, TypeError):
            return
        try:
            self._log_queue.put_nowait(line)
        except asyncio.QueueFull:
            # 宁可丢日志也不拖慢事件广播；丢了多少由下一条成功落盘的日志交代
            self._log_dropped += 1
            self._log_gap_reason = self._log_gap_reason or "queue full"

    def _flush_log_gap(self) -> None:
        """把“上次落盘之后丢了多少条”补成一条记录，缺口必须留痕。"""
        dropped = self._log_dropped
        if not dropped or self._log_file is None:
            return
        self._log_dropped = 0
        gap = {
            "type": "log_gap",
            "ts": time.time(),
            "dropped": dropped,
            "reason": self._log_gap_reason or "unknown",
        }
        try:
            self._log_file.write(json.dumps(gap, ensure_ascii=False) + "\n")
            self._log_file.flush()
            self._log_gap_reason = ""
        except OSError:  # 仍然写不进去：计数还回去，下一次成功时再交代
            self._log_dropped = dropped

    async def _log_writer(self, queue: asyncio.Queue[str | None]) -> None:
        """唯一的事件日志写协程：同一文件的行严格按 emit 顺序落盘。

        队列通过参数传进来而不是读 self：收尾时 self._log_queue 会先被清空，
        协程真正跑起来时再去读就已经丢了引用（整批日志直接蒸发）。
        """
        while True:
            line = await queue.get()
            try:
                if line is None:
                    return
                self._rotate_log_if_needed()
                if self._log_file is None:
                    # 没有落点：这条丢了，记账，等能写的时候补一条 gap
                    self._log_dropped += 1
                    self._log_gap_reason = self._log_gap_reason or "log unavailable"
                    continue
                self._flush_log_gap()
                self._log_file.write(line)
                self._log_file.flush()
            except Exception as exc:  # 写失败不阻断会话，记到 daemon 日志
                self._log_dropped += 1
                self._log_gap_reason = "write failed"
                print(f"[alex] event log write failed: {exc!r}", file=sys.stderr)
            finally:
                queue.task_done()

    def _rotate_log_if_needed(self) -> None:
        day = time.strftime("%Y%m%d")
        if self._log_file is not None and day == self._log_day:
            return
        if self._log_file is None and time.monotonic() < self._log_retry_at:
            # 刚失败过：别为了每一条事件反复 mkdir + open
            return
        self._close_log_file()
        self._log_day = day
        if self._log_dir is None:
            return
        try:
            # 目录可能被外部清掉（清理脚本 / 手工删除）：每次轮转都补建。
            # 少了这一步，open 失败后这个进程当天的事件会全部静默丢失。
            self._log_dir.mkdir(parents=True, exist_ok=True)
            # 一天一个文件、同一天重启就追加：文件数从"每次启动一个"收敛到按天
            self._log_file = open(
                self._log_dir / f"{day}.jsonl", "a", encoding="utf-8"
            )
        except OSError as exc:
            self._log_retry_at = time.monotonic() + LOG_RETRY_SECONDS
            self._log_gap_reason = self._log_gap_reason or "log open failed"
            # 一天只报一次，剩下交给 log_gap 记录，别把 stderr 刷满
            if self._log_failed_day != day:
                self._log_failed_day = day
                print(f"[alex] event log open failed: {exc!r}", file=sys.stderr)
            self._log_file = None
            return
        self._log_retry_at = 0.0
        self._log_failed_day = ""
        self._prune_logs(day)

    def _close_log_file(self) -> None:
        if self._log_file is None:
            return
        try:
            self._log_file.close()
        except Exception:  # 关闭日志文件失败不影响退出
            pass
        self._log_file = None

    @staticmethod
    def _log_file_day(path: Path) -> str:
        """从 ``YYYYMMDD-pid.jsonl`` 里取日期前缀，取不到返回空串。"""
        prefix = path.name.split("-", 1)[0]
        if len(prefix) != 8 or not prefix.isdigit():
            return ""
        return prefix

    def _prune_logs(self, today: str) -> None:
        """保留最近 N 天的事件日志，更早的删掉（N 可在 config.json 调）。"""
        if self._log_dir is None:
            return
        try:
            day = time.mktime(time.strptime(today, "%Y%m%d"))
        except ValueError:
            return
        days = self._retention_days()
        cutoff = day - (days - 1) * 86400
        for path in self._log_dir.glob("*.jsonl"):
            older = self._log_file_day(path)
            if not older:
                continue
            try:
                stamp = time.mktime(time.strptime(older, "%Y%m%d"))
            except ValueError:
                continue
            if stamp < cutoff:
                try:
                    path.unlink()
                except OSError:  # 正被别的进程打开时跳过，下次启动再清
                    continue

    @staticmethod
    def _retention_days() -> int:
        raw = CONFIG.get("log_retention_days")
        if raw is None or raw == "":
            raw = LOG_RETENTION_DAYS
        try:
            days = int(raw)
        except (TypeError, ValueError):
            days = LOG_RETENTION_DAYS
        return max(1, days)

    async def _close_log(self) -> None:
        """收尾：把队列里剩下的事件写完再关文件（幂等，可重复调用）。"""
        queue = self._log_queue
        task = self._log_task
        self._log_queue = None
        self._log_task = None
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                # 队列满时直接取消：留下的半截日志也比卡住退出强
                if task is not None:
                    task.cancel()
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=3)
            except Exception:
                task.cancel()
        # 退出前把最后一批缺口也写进去，否则只有日志断了、没人知道断了多少
        self._flush_log_gap()
        self._close_log_file()

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
        except Exception as exc:  # 清理超时不能让 daemon 残留，记日志后继续退出
            print(f"[alex] session close on shutdown failed: {exc!r}", file=sys.stderr)
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3)
            except Exception as exc:  # 关监听超时同样继续退出
                print(
                    f"[alex] server close on shutdown failed: {exc!r}",
                    file=sys.stderr,
                )
        if self._log_file is not None:
            self._close_log_file()
        if self.info is not None:
            clear_session_file(self.root / "data" / "daemon.json", pid=os.getpid())
        # 队列里剩下的事件写完再退出，避免最后几条日志丢掉
        await self._close_log()

    # ------------------------------------------------------------ 客户端处理
    @staticmethod
    def _fit_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """兜底：快照行仍要留出协议余量，超限时退回“无历史”，由客户端按页取。

        历史本身已经按页发送；真顶到上限只可能是 checkpoints 之类越攒越大，
        与其让客户端在 read_line 上撞 ValueError 掉线，不如少发一页历史。
        """
        if len(encode(snapshot)) <= MAX_LINE_BYTES:
            return snapshot
        total = int(snapshot.get("history_total", 0) or 0)
        skipped = snapshot.get("history") or []
        snapshot["history"] = []
        snapshot["history_start"] = (
            int(snapshot.get("history_start", 0) or 0) + len(skipped)
        )
        snapshot["history_total"] = total or len(skipped)
        if len(encode(snapshot)) <= MAX_LINE_BYTES:
            return snapshot
        # 退到最后一步：检查点（含每轮的文件清单）单独就超限。本次先发空表，
        # 下一次 checkpoints 事件再补齐，总比整条链路被打断强。
        snapshot["checkpoints"] = []
        return snapshot

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
        client.send(self._fit_snapshot(snapshot))

        try:
            while True:
                message = await read_line(reader)
                if message is None:
                    break
                await self._handle_message(client, message)
        except (OSError, ValueError, asyncio.CancelledError):  # 客户端断开或协议错误：结束该连接即可
            pass
        finally:
            try:
                if client in self._clients:
                    self._clients.remove(client)
            except Exception:  # 列表里已没有该客户端
                pass
            client.close()
            self._maybe_exit()

    async def _handle_message(
        self, client: _Client, message: dict[str, Any]
    ) -> None:
        kind = message.get("type")
        if kind == MSG_PING:
            client.send({"type": MSG_PONG})
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
        self._log_dir = self.root / "data" / "sessions" / "events"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_queue = asyncio.Queue(maxsize=LOG_QUEUE_SIZE)
        self._log_task = asyncio.create_task(
            self._log_writer(self._log_queue), name="event-log"
        )
        # 启动先清一遍过期日志：一天一个文件，重启不再堆新的
        self._prune_logs(time.strftime("%Y%m%d"))

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
            await self._close_log()
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
    except KeyboardInterrupt:  # Ctrl+C 退出属正常
        pass


if __name__ == "__main__":
    main()
