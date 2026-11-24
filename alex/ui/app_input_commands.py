"""AlexApp 的输入与命令：斜杠命令注册表与各命令实现。"""

from __future__ import annotations

from alex.config import workspace_path
from alex.core.images import split_images
from alex.screens.dir_picker import DirectoryPicker


class InputCommandMixin:
    """斜杠命令处理：命令名 → 处理方法，逐个命令各自独立。"""

    _COMMANDS: dict[str, str] = {
        "/exit": "_cmd_exit",
        "/quit": "_cmd_exit",
        "/model": "_cmd_model",
        "/permission": "_cmd_permission",
        "/settings": "_cmd_settings",
        "/cd": "_cmd_cd",
        "/init": "_cmd_init",
        "/help": "_cmd_help",
        "/theme": "_cmd_theme",
        "/reload": "_cmd_reload",
        "/image": "_cmd_image",
        "/compact": "_cmd_compact",
        "/queue": "_cmd_queue",
        "/goal": "_cmd_goal",
        "/clear": "_cmd_clear",
    }

    def _handle_command(self, raw: str) -> None:
        command, _, arg = raw.partition(" ")
        handler = self._COMMANDS.get(command.lower())
        if handler is None:
            self.notify(
                f"Unknown command: {command.lower()}",
                severity="error",
                timeout=3,
            )
            return
        getattr(self, handler)(arg.strip())

    def _reject_when_busy(self, action: str) -> bool:
        """忙 / 回滚中时给统一提示；返回 True 表示本次动作应当中止。"""
        if self._busy or self._rewinding:
            self.notify(
                f"Busy — {action} after this reply",
                severity="warning",
                timeout=2,
            )
            return True
        return False

    def _cmd_exit(self, arg: str) -> None:
        self.exit()

    def _cmd_model(self, arg: str) -> None:
        if arg and self._rewinding:
            self.notify(
                "Busy — wait for the restore to finish",
                severity="warning",
                timeout=2,
            )
        elif arg:
            self.run_worker(self._switch_model(arg))
        else:
            self.run_worker(self._open_model_picker())

    def _cmd_permission(self, arg: str) -> None:
        from alex.core.permissions import MODE_NAMES

        if not arg:
            self.run_worker(self._choose_permission())
        elif arg.lower() not in MODE_NAMES:
            self.notify(
                f"Unknown mode {arg}; choose with /permission",
                severity="error",
                timeout=4,
            )
        else:
            self._set_permission_mode(arg.lower())

    def _cmd_settings(self, arg: str) -> None:
        self._open_settings()

    def _cmd_cd(self, arg: str) -> None:
        if self._reject_when_busy("change workspace"):
            return
        if arg:
            self.run_worker(self._change_workspace(arg), exclusive=True)
        else:
            self.push_screen(
                DirectoryPicker(workspace_path(create=True)),
                self._on_directory_picked,
            )

    def _cmd_init(self, arg: str) -> None:
        if arg.lower() != "memory":
            self.notify("Usage: /init memory", severity="warning", timeout=3)
        elif self._reject_when_busy("clear memory"):
            return
        else:
            self.run_worker(self._init_memory(), exclusive=True)

    def _cmd_help(self, arg: str) -> None:
        self.notify(
            "/model choose model & reasoning · /permission choose permission mode"
            " · /settings skills / rules / memory / tokens / MCP"
            " · /cd browse workspace · /init memory clear memory"
            " · paste an image path or /image <path> [prompt]"
            " · F6 / Alt+V paste clipboard screenshot"
            " · click a user message to restore it"
            " · click a change summary to view its diff"
            " · /reload reload config · /clear clear session · /theme choose theme"
            " · /compact compress context"
            " · /queue split the request into tasks, run them in order"
            " · /goal work until an independent verifier passes it"
            " · Shift+Enter newline · /exit quit",
            severity="warning",
            timeout=10,
        )

    def _cmd_theme(self, arg: str) -> None:
        self._open_theme_picker()

    def _cmd_reload(self, arg: str) -> None:
        if self._reject_when_busy("reload"):
            return
        self.run_worker(self._reload(), exclusive=True)

    def _cmd_image(self, arg: str) -> None:
        if not arg:
            self.notify(
                "Usage: /image <path> [prompt]",
                severity="warning",
                timeout=4,
            )
            return
        text, images = split_images(arg, workspace_path())
        if not images:
            self.notify(
                f"Image not found: {arg.split()[0]}",
                severity="error",
                timeout=5,
            )
            return
        self._start_exchange(text, images)

    def _cmd_compact(self, arg: str) -> None:
        if self._reject_when_busy("compact"):
            return
        self.run_worker(self._compact_context(), exclusive=True)

    def _cmd_queue(self, arg: str) -> None:
        if self._reject_when_busy("queue"):
            return
        if not arg:
            self.notify(
                "Usage: /queue task A; task B; task C",
                severity="warning",
                timeout=4,
            )
            return
        self.run_worker(self._send_queue(arg), exclusive=True)

    def _cmd_goal(self, arg: str) -> None:
        if self._reject_when_busy("goal"):
            return
        if not arg:
            self.notify(
                "Usage: /goal <what must be achieved>",
                severity="warning",
                timeout=4,
            )
            return
        self.run_worker(self._send_goal(arg), exclusive=True)

    def _cmd_clear(self, arg: str) -> None:
        if self._reject_when_busy("clear"):
            return
        self.run_worker(self._clear_session(), exclusive=True)
