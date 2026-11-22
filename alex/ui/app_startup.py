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
        except Exception:  # 遮罩已卸载时停止更新
            pass

    def _hide_loading(self) -> None:
        if not self._loading_visible:
            return
        self._loading_visible = False
        try:
            self.query_one("#loading", LoadingOverlay).remove()
        except Exception:  # 遮罩已卸载
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
        except Exception:  # 输入框未挂载时忽略
            pass
        self.notify(
            f"Init failed: {message}", title="Init failed", severity="error", timeout=10
        )

    def _on_link_closed(self) -> None:
        self.notify(
            "Disconnected from the background session",
            title="Disconnected",
            severity="warning",
            timeout=8,
        )

    async def _rpc(self, method: str, **params):
        """调用会话；连接失效时自动重连并重试一次。"""
        if method != "describe":
            self._settings_cache = None
        if self.link is None:
            await self._reconnect()
        try:
            return await self.link.rpc(method, **params)
        except LinkError:
            await self._reconnect()
            return await self.link.rpc(method, **params)

    async def _reconnect(self) -> None:
        old = self.link
        if isinstance(old, DaemonClient):
            try:
                await old.close()
            except Exception:  # 旧连接关闭失败不影响重连
                pass
        try:
            self.link = await _facade().ensure_daemon(ROOT)
        except (LinkError, OSError, asyncio.TimeoutError) as exc:
            self.link = None
            raise LinkError(f"reconnect failed: {exc}") from exc
        self.link.on_event(self._on_session_event)
        if isinstance(self.link, DaemonClient):
            self.link.on_close(self._on_link_closed)
        await self.link.startup()
        self.notify("Reconnected to the background session", timeout=2)

    def _on_session_event(self, kind: str, data: dict) -> None:
        try:
            self.call_later(self._handle_event, kind, data)
        except Exception:
            self._handle_event(kind, data)
