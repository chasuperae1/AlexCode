"""卡片公共部件：头部点击折叠与底部动作按钮。"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class HeadToggleMixin:
    """点击卡片头部展开/收起；子类提供 ``_head`` 与 ``action_toggle``。"""

    def on_click(self, event) -> None:
        target = event.widget
        while target is not None:
            if target is self._head:
                self.action_toggle()
                event.stop()
                return
            target = target.parent


class DiffAction(Static):
    """权限卡片底部的一个可点击动作。"""

    def __init__(self, label: Text, action: str, on_click, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._action = action
        self._on_click = on_click

    def on_click(self, event) -> None:
        self._on_click(self._action)
        event.stop()
