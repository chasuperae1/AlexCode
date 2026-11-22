"""本地图片输入：从消息里解析路径并构造多模态 UserMsg。"""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from agentscope.message import UserMsg

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DEFAULT_IMAGE_PROMPT = "Describe this image."

_TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')
_WINDOWS_DRIVE = re.compile(r"^/[A-Za-z]:")


def as_image_path(token: str, base: Path) -> Path | None:
    """把单个 token 解析成存在的本地图片路径；否则返回 None。"""
    token = (token or "").strip().strip("\"'")
    if not token:
        return None
    if token.lower().startswith("file://"):
        path = unquote(urlparse(token).path)
        if _WINDOWS_DRIVE.match(path):
            path = path[1:]
        token = path
    path = Path(token).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        path = path.resolve()
    except OSError:
        return None
    if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
        return None
    return path


def split_images(text: str, base: Path) -> tuple[str, list[Path]]:
    """剥离消息里的图片路径，返回 (剩余文本, 图片列表)。"""
    # 拖入 / 粘贴的路径可能带空格，先整体判断一次
    whole = as_image_path(text.strip(), base)
    if whole is not None:
        return "", [whole]
    images: list[Path] = []
    spans: list[tuple[int, int]] = []
    for match in _TOKEN.finditer(text):
        token = match.group(1) or match.group(2) or match.group(3) or ""
        path = as_image_path(token, base)
        if path is None:
            continue
        images.append(path)
        spans.append(match.span())
    if not images:
        return text.strip(), []
    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        last = end
    parts.append(text[last:])
    prompt = " ".join("".join(parts).split())
    return prompt, images


def grab_clipboard_image(dest: Path) -> Path | None:
    """把剪贴板里的图片保存为 PNG；剪贴板没有图片时返回 None。

    剪贴板里如果是图片文件（资源管理器复制），直接返回该文件路径。
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    try:
        data = ImageGrab.grabclipboard()
    except Exception:
        return None
    if isinstance(data, list):
        for item in data:
            path = Path(str(item))
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                return path
        return None
    if data is None:
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data.save(dest, "PNG")
    except Exception:
        return None
    return dest


def display_text(prompt: str, images: list[Path]) -> str:
    """聊天区展示的文本：保留提示词并附加图片路径。"""
    if not images:
        return prompt
    markers = "\n".join(f"[image] {path}" for path in images)
    return f"{prompt}\n{markers}" if prompt else markers


def build_user_message(
    prompt: str, images: list[Path], *, vision: bool = True
) -> UserMsg:
    """把文本和本地图片组装成多模态用户消息。

    ``vision=False`` 时（模型不支持识图）只发文本，把图片作为本地文件路径
    附在消息里，避免触发接口报错，agent 仍能知道有附件、可用工具处理。
    """
    # 只有 daemon 侧会构造消息，延后导入可让 TUI 启动少加载约 0.2s
    from agentscope.message import Base64Source, DataBlock, TextBlock, UserMsg

    if not images:
        return UserMsg(name="user", content=prompt)
    if not vision:
        notes = "\n".join(
            f"[image] {path} (attached file; current model has no vision)"
            for path in images
        )
        return UserMsg(name="user", content=f"{prompt}\n{notes}".strip())
    blocks: list[TextBlock | DataBlock] = [
        TextBlock(text=prompt or DEFAULT_IMAGE_PROMPT)
    ]
    for path in images:
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        if not media_type.startswith("image/"):
            media_type = "image/png"
        blocks.append(
            DataBlock(
                source=Base64Source(
                    data=base64.b64encode(path.read_bytes()).decode("ascii"),
                    media_type=media_type,
                ),
                name=path.name,
            )
        )
    return UserMsg(name="user", content=blocks)
