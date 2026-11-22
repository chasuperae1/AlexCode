"""回答正文里的文件附件卡：类型色圆角徽标、属性行、底部三操作。

卡片只服务“已存在、非空、位于允许目录”的最终文件。回答里的文件链接由
``split_file_links`` 识别，时间线在链接位置挂一张卡，而不是在回答末尾堆附件栏。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from alex.config import ROOT, workspace_path
from alex.ui.sources import format_bytes


# 扩展名 -> (徽标文字, 属性名, 背景色, 前景色)
TYPE_SPECS: dict[str, tuple[str, str, str, str]] = {
    ".xlsx": ("XLSX", "Excel workbook", "#217346", "#ffffff"),
    ".xlsm": ("XLSM", "Excel workbook", "#217346", "#ffffff"),
    ".xls": ("XLS", "Excel workbook", "#217346", "#ffffff"),
    ".docx": ("DOCX", "Word document", "#2b579a", "#ffffff"),
    ".doc": ("DOC", "Word document", "#2b579a", "#ffffff"),
    ".pdf": ("PDF", "PDF document", "#b30b00", "#ffffff"),
    ".pptx": ("PPTX", "PowerPoint", "#b7472a", "#ffffff"),
    ".ppt": ("PPT", "PowerPoint", "#b7472a", "#ffffff"),
    ".md": ("MD", "Markdown", "#4b8bbe", "#ffffff"),
    ".markdown": ("MD", "Markdown", "#4b8bbe", "#ffffff"),
    ".csv": ("CSV", "CSV data", "#5f6b76", "#ffffff"),
    ".json": ("JSON", "JSON data", "#5f6b76", "#ffffff"),
    ".txt": ("TXT", "Text file", "#5f6b76", "#ffffff"),
    ".html": ("HTML", "HTML page", "#1e88c7", "#ffffff"),
    ".htm": ("HTM", "HTML page", "#1e88c7", "#ffffff"),
    ".png": ("PNG", "Image", "#1e88c7", "#ffffff"),
    ".jpg": ("JPG", "Image", "#1e88c7", "#ffffff"),
    ".jpeg": ("JPG", "Image", "#1e88c7", "#ffffff"),
    ".webp": ("WEBP", "Image", "#1e88c7", "#ffffff"),
    ".svg": ("SVG", "Vector image", "#1e88c7", "#ffffff"),
    ".zip": ("ZIP", "Archive", "#e3a857", "#221605"),
    ".mp3": ("MP3", "Audio", "#5f6b76", "#ffffff"),
    ".wav": ("WAV", "Audio", "#5f6b76", "#ffffff"),
    ".mp4": ("MP4", "Video", "#5f6b76", "#ffffff"),
}

# 不把源码、配置、日志、规则文件当成交付文件挂卡
BLOCKED_DIRS = {
    ROOT / "alex",
    ROOT / "data",
    ROOT / "skills",
    ROOT / "rules",
    ROOT / "tests",
    ROOT / ".sessions",
    ROOT / ".git",
}

_LINK_LINE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?\[([^\]\n]+)\]\((.*)\)[ \t]*$"
)
_MARKER_LINE = re.compile(r"^[ \t]*\[\[file:(.+?)\]\][ \t]*$")


@dataclass(frozen=True)
class Attachment:
    """一张文件附件卡需要的全部信息。"""

    path: Path
    name: str
    badge: str
    kind: str
    color: str
    foreground: str
    size: int
    modified: str

    @classmethod
    def from_path(cls, path: Path) -> "Attachment | None":
        try:
            target = path.expanduser()
            if not target.is_absolute():
                target = workspace_path() / target
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not _is_allowed(resolved) or not resolved.is_file():
            return None
        try:
            stat = resolved.stat()
        except OSError:
            return None
        if stat.st_size <= 0:
            return None
        spec = TYPE_SPECS.get(resolved.suffix.lower())
        if spec is None:
            return None
        badge, kind, color, foreground = spec
        return cls(
            path=resolved,
            name=resolved.name,
            badge=badge,
            kind=kind,
            color=color,
            foreground=foreground,
            size=stat.st_size,
            modified=time.strftime("%H:%M", time.localtime(stat.st_mtime)),
        )

    @property
    def meta(self) -> str:
        return f"{self.kind} · {format_bytes(self.size)} · {self.modified}"


def _is_allowed(path: Path) -> bool:
    roots: list[Path] = []
    for root in (ROOT, workspace_path()):
        try:
            roots.append(root.resolve())
        except OSError:
            continue
    if not any(path == root or path.is_relative_to(root) for root in roots):
        return False
    if any(path == blocked or path.is_relative_to(blocked) for blocked in BLOCKED_DIRS):
        return False
    if path.name.startswith("."):
        return False
    return True


def _attachment_for_line(line: str) -> Attachment | None:
    match = _LINK_LINE.match(line)
    raw = ""
    if match is not None:
        raw = match.group(2).strip()
    else:
        match = _MARKER_LINE.match(line)
        if match is not None:
            raw = match.group(1).strip()
    if not raw:
        return None
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        raw = unquote(parsed.path.lstrip("/")) if sys.platform == "win32" else unquote(parsed.path)
    raw = raw.strip().strip("<>")
    raw = unquote(raw)
    return Attachment.from_path(Path(raw))


def split_file_links(source: str) -> list[str | Attachment]:
    """把正文按“独占一行的文件链接”切开，链接位置替换成 Attachment。"""
    parts: list[str | Attachment] = []
    buffer: list[str] = []
    in_code = False

    def flush() -> None:
        if not buffer:
            return
        chunk = "\n".join(buffer).strip("\n")
        buffer.clear()
        if chunk.strip():
            parts.append(chunk)

    for line in source.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            buffer.append(line)
            continue
        attachment = None if in_code else _attachment_for_line(line)
        if attachment is None:
            buffer.append(line)
            continue
        flush()
        parts.append(attachment)
    flush()
    return parts or [source]


def _open_windows(path: Path) -> None:
    """交给 Explorer 打开文件，避免 Electron 应用继承 TUI 的控制台。

    ``os.startfile`` / ``ShellExecuteEx`` 走的是调用进程的控制台句柄；
    Typora、Notion 这类 Electron 应用会把 Chromium 日志直接写到当前终端，
    把 Textual 的整屏画面刷掉。Explorer 是独立的 GUI 进程，由它做文件
    关联启动时不会再继承 TUI 的 stdout/stderr。

    注意：如果目标应用（例如 Notion）已经在运行，Explorer 只会把路径转交给
    现有实例；该实例的控制台句柄在它自己启动时就已固定，必须让它完全退出再
    重开，才能摆脱旧终端。
    """
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        ["explorer.exe", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def open_file(path: Path) -> None:
    """用系统默认程序打开文件。"""
    if sys.platform == "win32":
        _open_windows(path)
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [opener, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def copy_file_to_clipboard(path: Path) -> None:
    """把文件本身放进系统剪贴板（Windows 资源管理器的文件复制语义）。"""
    if sys.platform == "win32":
        env = dict(os.environ)
        env["ALEX_FILE_TO_COPY"] = str(path)
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Set-Clipboard -LiteralPath $env:ALEX_FILE_TO_COPY",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=8,
        )
        return
    if sys.platform == "darwin":
        subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "set the clipboard to POSIX file (item 1 of argv)",
                "-e",
                "end run",
                str(path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        return
    subprocess.run(
        ["xclip", "-selection", "clipboard", "-t", "text/uri-list"],
        input=path.as_uri().encode("utf-8"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
    )


class FileAction(Static):
    """卡片底部的一枚文字按钮。"""

    can_focus = True

    class Pressed(Message):
        def __init__(self, action: str, attachment: Attachment) -> None:
            super().__init__()
            self.action = action
            self.attachment = attachment

    def __init__(self, label: str, action: str, attachment: Attachment, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._action = action
        self._attachment = attachment

    def on_click(self, event) -> None:
        event.stop()
        self.post_message(self.Pressed(self._action, self._attachment))

    def on_key(self, event) -> None:
        if event.key in {"enter", "space"}:
            event.stop()
            event.prevent_default()
            self.post_message(self.Pressed(self._action, self._attachment))


class FileAttachmentCard(Vertical):
    """B 版附件卡：信息区在上，Open / Copy / Copy path 在底部操作条。"""

    DEFAULT_CSS = """
    FileAttachmentCard {
        width: 50;
        max-width: 100%;
        height: auto;
        margin: 0 0 1 0;
        background: transparent;
        border: round $primary 35%;
    }
    FileAttachmentCard .fa-main {
        height: 3;
        padding: 0 1;
    }
    FileAttachmentCard .fa-icon {
        width: 6;
        height: 3;
        margin-right: 1;
        content-align: center middle;
        text-align: center;
    }
    FileAttachmentCard .fa-info {
        width: 1fr;
        height: 3;
        padding-top: 1;
    }
    FileAttachmentCard .fa-name {
        height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    FileAttachmentCard .fa-meta {
        height: 1;
        color: $text-muted;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    FileAttachmentCard .fa-ops {
        height: 2;
        padding: 0 1;
        border-top: solid $primary 28%;
    }
    FileAttachmentCard .fa-gap {
        width: 1fr;
        height: 1;
    }
    FileAttachmentCard .fa-act {
        width: auto;
        height: 1;
    }
    FileAttachmentCard .fa-open {
        color: $primary;
        text-style: bold;
    }
    FileAttachmentCard .fa-copy {
        color: $text-muted;
    }
    FileAttachmentCard .fa-copy-path {
        color: $text-muted;
        text-style: dim;
    }
    FileAttachmentCard .fa-space {
        width: 2;
        height: 1;
    }
    FileAttachmentCard .fa-act:hover,
    FileAttachmentCard .fa-act:focus {
        color: $accent;
        text-style: underline;
    }
    """

    def __init__(self, attachment: Attachment, theme, **kwargs) -> None:
        super().__init__(classes="file-attachment", **kwargs)
        self.attachment = attachment
        self._theme = theme
        self._icon = Static(
            self._icon_text(attachment, theme.foreground),
            classes="fa-icon",
        )
        # 实心背景会盖掉圆角轮廓，终端里无法同时做到“实色 + 圆角”；
        # 这里用类型色 round border 做圆角徽标，文字仍单行居中。
        self._icon.styles.border = ("round", attachment.color)
        self._name = Static(
            Text(attachment.name, style=theme.foreground),
            classes="fa-name",
        )
        self._meta = Static(Text(attachment.meta, style=theme.secondary), classes="fa-meta")
        self._open = FileAction(
            "Open", "open", attachment, classes="fa-act fa-open"
        )
        self._copy = FileAction(
            "Copy", "copy", attachment, classes="fa-act fa-copy"
        )
        self._copy_path = FileAction(
            "Copy path", "copy-path", attachment, classes="fa-act fa-copy-path"
        )

    @staticmethod
    def _icon_text(attachment: Attachment, foreground: str) -> Text:
        return Text(attachment.badge, justify="center", style=f"bold {foreground}")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="fa-main"):
            yield self._icon
            with Vertical(classes="fa-info"):
                yield self._name
                yield self._meta
        with Horizontal(classes="fa-ops"):
            yield Static("", classes="fa-gap")
            yield self._open
            yield Static("", classes="fa-space")
            yield self._copy
            yield Static("", classes="fa-space")
            yield self._copy_path

    def on_file_action_pressed(self, message: FileAction.Pressed) -> None:
        message.stop()
        handler = getattr(self.app, "file_attachment_action", None)
        if handler is not None:
            handler(message.action, message.attachment)
