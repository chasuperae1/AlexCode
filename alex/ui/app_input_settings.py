"""AlexApp 的输入与命令：快捷键动作、队列、粘贴、发送、权限、设置、斜杠命令。"""

from __future__ import annotations

import asyncio
import time

from rich.text import Text
from textual.widgets import Static

from alex.settings import SettingsData, SettingsScreen
from alex.ui.state import _save_ui_state


class InputSettingsMixin:
    """设置页打开、加载与显示模式切换。"""

    def _open_settings(self) -> None:
        self._start_settings_spinner()
        # 设置页单独占一个 worker group：默认组里可能正挂着权限确认
        # （_ask_confirm 在等用户点 Allow/Deny）。用 exclusive=True 放在默认组
        # 会把它一起取消，简洁模式的 finally 会直接摘掉权限卡；拆组后互不干扰。
        self.run_worker(
            self._open_settings_async(), exclusive=True, group="settings"
        )

    def _start_settings_spinner(self) -> None:
        """Settings 文字左侧显示加载指示，和对话中的 spinner 同款。"""
        spinner = self.query_one("#settings-spinner", Static)
        self._settings_spinner_frame = 0
        spinner.update(self._settings_spinner_text())
        spinner.display = True
        if self._settings_spinner_timer is None:
            self._settings_spinner_timer = self.set_interval(
                0.12, self._tick_settings_spinner
            )

    def _settings_spinner_text(self) -> Text:
        frame = self._spinner_frames[
            self._settings_spinner_frame % len(self._spinner_frames)
        ]
        return Text(frame, style=f"bold {self.current_theme.accent}")

    def _tick_settings_spinner(self) -> None:
        self._settings_spinner_frame += 1
        self.query_one("#settings-spinner", Static).update(
            self._settings_spinner_text()
        )

    def _stop_settings_spinner(self) -> None:
        if self._settings_spinner_timer is not None:
            self._settings_spinner_timer.stop()
            self._settings_spinner_timer = None
        self.query_one("#settings-spinner", Static).display = False

    async def _open_settings_async(self) -> None:
        started = time.monotonic()
        if self.link is None:
            self._stop_settings_spinner()
            return
        # 预取的 describe 直接复用，点击后只差一次弹窗挂载
        data = self._settings_cache
        if data is None:
            try:
                data = await self._rpc("describe")
            except Exception as exc:
                self._stop_settings_spinner()
                self.notify(
                    f"Cannot open settings: {exc}", severity="error", timeout=5
                )
                return
            self._settings_cache = data
        screen = SettingsScreen(
            SettingsData.from_payload(
                data, display_mode=getattr(self, "_display_mode", "developer")
            ),
            on_visibility_change=self._set_agent_visibility,
            on_display_mode_change=self._set_display_mode,
            theme=str(getattr(self, "theme", "")),
        )
        # 数据往往瞬间到手，保底展示一小段时间，让加载指示肉眼可见
        remain = 0.35 - (time.monotonic() - started)
        if remain > 0:
            await asyncio.sleep(remain)
        self._stop_settings_spinner()
        self._settings_open = True
        try:
            changed = await self.push_screen_wait(screen)
        finally:
            self._settings_open = False
        # 设置页开着时切过主题：聊天区被盖住，重建推迟到这里做一次
        themed = bool(getattr(self, "_theme_rebuild_pending", False))
        self._theme_rebuild_pending = False
        # 改动类设置（主题变量 / 字号 / 技能可见性）会让缓存里的渲染结果失效
        context = getattr(self, "render_context", None)
        if changed and context is not None:
            context.invalidate()
        # 没有改动时不重建聊天区。只开关设置页就全量重建，会把仅存在于
        # 当前界面的中间 assistant 文本、已作答 ask 行冲掉（history 里未必
        # 都有对应记录）。
        if changed or themed:
            self._render_history()
            if themed and getattr(self, "_subagent_view_open", False):
                self._retheme_subagent_board()
        elif self.view.thinking:
            # 不重建聊天区时，Textual 的 Collapsible 正文可能停留在弹层
            # 关闭前的一帧；主动把当前思考正文重绘一次。
            self._update_thinking()
        self._refresh_meta_line()
        # 设置弹层关闭后把键盘焦点交还主输入框，避免继续输入时落进
        # Settings 按钮或答题卡的自定义输入。
        self._restore_prompt_focus()
        # 关闭后数据可能已变：清缓存并后台刷新，下次打开依旧秒开
        self._settings_cache = None
        self.run_worker(self._prefetch_settings(), exclusive=False)
        if not changed:
            return
        kinds = screen.changed_kinds
        if kinds == {"preference"}:
            # 只改了显示模式：已即时写入 UI 偏好，无需触碰 daemon
            self.notify("Display mode saved", timeout=3)
        elif kinds and kinds <= {"skill", "rule"}:
            # 只改了技能/规则：重建 agent 即可，不必重启 MCP
            try:
                await self._rpc("rebuild_agent")
                self._refresh_sources()
                self.notify("Applied skill/rule changes", timeout=3)
            except Exception as exc:
                self.notify(
                    f"Apply failed: {exc}", severity="error", timeout=6
                )
        else:
            await self._reload()

    async def _prefetch_settings(self) -> None:
        """空闲时预先拉取设置页数据，点击 Settings 时无需等待。"""
        if self.link is None:
            return
        try:
            self._settings_cache = await self._rpc("describe")
        except Exception:
            self._settings_cache = None

    def _set_agent_visibility(self, kind: str, item: str, visible: bool) -> None:
        if self.link is None:
            return
        self.run_worker(
            self._rpc(
                "set_visibility", kind=kind, item=item, visible=visible
            )
        )

    def _set_display_mode(self, mode: str) -> None:
        """切换交互区显示模式：只存 TUI 偏好，下一轮渲染时生效。"""
        mode = "concise" if str(mode) == "concise" else "developer"
        if mode == getattr(self, "_display_mode", "developer"):
            return
        self._display_mode = mode
        _save_ui_state(display_mode=mode)
