"""AlexApp 的界面外围：主题、任务与进度、目标卡、模型选择、命令面板。"""

from __future__ import annotations

from textual.widgets import OptionList
from textual.widgets.option_list import Option

from alex.ui.widgets import PromptInput


class ChromePaletteMixin:
    """命令面板动作。"""

    def _show_palette(self, prefix: str) -> None:
        matches = [
            (command, description)
            for command, description in self.COMMANDS
            if command.startswith(prefix.lower())
        ]
        if not matches:
            self._hide_palette()
            return
        palette = self.query_one("#commands", OptionList)
        if matches != self._palette_matches:
            self._palette_matches = matches
            palette.clear_options()
            palette.add_options(
                [
                    Option(f"{command:<10} {description}", id=command)
                    for command, description in matches
                ]
            )
        palette.highlighted = 0
        palette.add_class("-visible")
        self._palette_visible = True

    def _hide_palette(self) -> None:
        self._palette_visible = False
        self._palette_matches = []
        self.query_one("#commands", OptionList).remove_class("-visible")

    def _move_palette(self, delta: int) -> None:
        if not self._palette_matches:
            return
        palette = self.query_one("#commands", OptionList)
        current = palette.highlighted or 0
        palette.highlighted = (current + delta) % len(self._palette_matches)

    def _accept_palette(self) -> None:
        if not self._palette_matches:
            return
        palette = self.query_one("#commands", OptionList)
        index = palette.highlighted or 0
        if index >= len(self._palette_matches):
            return
        command = self._palette_matches[index][0]
        self._hide_palette()
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = ""
        self._handle_command(command)

    def _complete_palette(self) -> None:
        """Tab 只把高亮命令补进输入框，不直接执行。"""
        if not self._palette_matches:
            return
        palette = self.query_one("#commands", OptionList)
        index = palette.highlighted or 0
        if index >= len(self._palette_matches):
            return
        command = self._palette_matches[index][0]
        self._hide_palette()
        prompt = self.query_one("#prompt", PromptInput)
        prompt.value = f"{command} "
        prompt.focus()

    def action_palette_up(self) -> None:
        if self._subagent_key_move(-1):
            return
        self._move_palette(-1)

    def action_palette_down(self) -> None:
        if self._subagent_key_move(1):
            return
        self._move_palette(1)

    def action_palette_accept(self) -> None:
        if self._subagent_key_open():
            return
        self._accept_palette()

    def action_palette_complete(self) -> None:
        self._complete_palette()
