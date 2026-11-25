"""AlexApp 的启动流程：连接 daemon、初始化、加载遮罩、断线重连。"""

from __future__ import annotations

import asyncio

from alex.config import ROOT
from alex.daemon.client import DaemonClient, LinkError, LocalLink
from alex.ui.boot import LoadingOverlay
from alex.ui.constants import _BOOT_LABELS, _BOOT_ORDER
from alex.ui.facade import _facade
from alex.ui.widgets import PromptInput


class StartupMixin:
    """启动流程：连接 daemon、初始化、加载遮罩、断线重连。"""

    def _boot_set(self, stage: str, status: str, detail: str = "") -> None:
        entry = self._boot.get(stage)
        if entry is None:
            entry = {"label": stage, "status": "idle", "detail": ""}
            self._boot[stage] = entry
        entry["status"] = status
        entry["detail"] = detail
        self._refresh_loading()

    def _refresh_loading(self) -> None:
        if not self._loading_visible:
            return
        try:
            # 阶段未上报的先用占位项补齐，加载页从第一帧到结束都是同一套布局
            stages = []
            for key in _BOOT_ORDER:
                entry = self._boot.get(key) or {
                    "label": _BOOT_LABELS[key],
                    "status": "idle",
                    "detail": "",
                }
                stages.append({"stage": key, **entry})
            self.query_one("#loading", LoadingOverlay).update_stages(
                stages, self._boot_frame
            )
        except Exception:
            pass

    def _hide_loading(self) -> None:
        if not self._loading_visible:
            return
        self._loading_visible = False
        try:
            self.query_one("#loading", LoadingOverlay).remove()
        except Exception:
            pass

    async def _initialize(self) -> None:
        """连接会话（后台 daemon 或同进程），之后全部由事件驱动渲染。"""
        self._boot_set("daemon", "active", "connecting…")
        try:
            if self._no_daemon:
                self.link = LocalLink.create()
            else:
                self.link = await _facade().ensure_daemon(ROOT)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            self._init_failed(str(exc))
            return
        self._boot_set(
            "daemon",
            "done",
            "in-process" if isinstance(self.link, LocalLink) else "connected",
        )
        self.link.on_event(self._on_session_event)
        if isinstance(self.link, DaemonClient):
            self.link.on_close(self._on_link_closed)
            self.link.on_lost(self._on_link_lost)
        try:
            await self.link.startup()
        except Exception as exc:
            self._init_failed(str(exc))

    def _init_failed(self, message: str) -> None:
        """初始化失败：必须解除遮罩，否则界面永远停在连接中。"""
        self._boot_set("daemon", "failed", message)
        self._hide_loading()
        try:
            self.query_one("#prompt", PromptInput).disabled = False
        except Exception:
            pass
        self.notify(
            f"Init failed: {message}", title="Init failed", severity="error", timeout=10
        )

    def _on_link_closed(self) -> None:
        if self._healing:
            # 断链后正在后台重连：先不提示断开，接回去会发 “Reconnected”
            return
        self.notify(
            "Disconnected from the background session",
            title="Disconnected",
            severity="warning",
            timeout=8,
        )

    def _on_link_lost(self) -> None:
        """传输层断开（超限行、心跳超时）：后台重连，用 resync 补事件。"""
        if self._healing:
            return
        self._healing = True
        self.run_worker(self._heal_link(), group="link", exclusive=True)

    async def _heal_link(self) -> None:
        try:
            await self._reconnect(resume=True)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            self.notify(
                f"Reconnect failed: {exc}",
                title="Disconnected",
                severity="error",
                timeout=10,
            )
        finally:
            self._healing = False

    async def _rpc(self, method: str, **params):
        """调用会话；连接失效时自动重连并重试一次。"""
        if method != "describe":
            self._settings_cache = None
        if self.link is None:
            await self._reconnect(resume=True)
        try:
            return await self.link.rpc(method, **params)
        except LinkError:
            await self._reconnect(resume=True)
            return await self.link.rpc(method, **params)

    async def _reconnect(self, resume: bool = False) -> None:
        """重建连接；resume 让同一个 daemon 用 resync 补发断开期间的事件。

        断链与 RPC 失败可能同时触发重连，用锁串行化，避免两条连接互相顶掉。
        """
        async with self._link_lock:
            if isinstance(self.link, DaemonClient) and self.link.is_open:
                return  # 并发的另一次重连已经建好连接
            old = self.link
            token = (
                old.resume_token()
                if resume and isinstance(old, DaemonClient)
                else None
            )
            if isinstance(old, DaemonClient):
                try:
                    await old.close()
                except Exception:
                    pass
            try:
                if token is not None:
                    self.link = await _facade().ensure_daemon(ROOT, resume=token)
                else:
                    self.link = await _facade().ensure_daemon(ROOT)
            except (LinkError, OSError, asyncio.TimeoutError) as exc:
                self.link = None
                raise LinkError(f"reconnect failed: {exc}") from exc
            self.link.on_event(self._on_session_event)
            if isinstance(self.link, DaemonClient):
                self.link.on_close(self._on_link_closed)
                self.link.on_lost(self._on_link_lost)
            await self.link.startup()
        self.notify("Reconnected to the background session", timeout=2)

    def _on_session_event(self, kind: str, data: dict) -> None:
        try:
            self.call_later(self._handle_event, kind, data)
        except Exception:
            self._handle_event(kind, data)
