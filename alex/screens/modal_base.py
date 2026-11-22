"""单选弹窗的公共基类：统一「选中即关闭」与快捷键提示。"""

from __future__ import annotations

from textual.screen import ModalScreen
from textual.widgets import OptionList

OPTION_HINT = "↑/↓ move · Enter select · Esc cancel"


class OptionListModal(ModalScreen[str | None]):
    """标题 + OptionList 的弹窗：选中任意选项即用其 id 关闭。"""

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(str(event.option.id))

    @staticmethod
    def _focus_list(options: OptionList, highlight: int = 0) -> None:
        options.highlighted = highlight
        options.focus()
