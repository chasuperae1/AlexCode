"""权限卡共用的 Allow / Always / Deny 状态机。

diff 卡（文件修改）和 shell 卡（命令执行）的版式不同，但"点按钮 → 本地
状态变更 → 回传一次选择"的逻辑完全一致，放在这里避免两份实现漂移。
"""

from __future__ import annotations

from typing import Any, Callable

from alex.screens.confirm import ALLOW, ALWAYS, DENY


class PermissionChoiceMixin:
    """子类需提供 ``_state`` / ``_payload`` / ``_always`` / ``_on_choice``。"""

    _state: str
    _payload: dict
    _always: object | None
    _on_choice: Callable[[Any], None]

    def choose(self, action: str) -> None:
        if self._state != "pending":
            return
        if action == "always" and self._always is None:
            return
        choice = {"allow": ALLOW, "always": ALWAYS, "deny": DENY}.get(
            action, DENY
        )
        self.set_state("denied" if choice == DENY else "allowed")
        try:
            self._on_choice(choice)
        except Exception:
            self.set_state("failed")
            self._payload["fail_text"] = "Permission reply failed · Try again"

    def resolve_external(self, choice: str) -> None:
        """另一个窗口先回应时，只更新卡片，不再回传一次。"""
        if self._state != "pending":
            return
        self.set_state("denied" if choice == DENY else "allowed")
