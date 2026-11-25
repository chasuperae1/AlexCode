"""会话级渲染上下文：宽度、主题与 epoch，外加 Markdown 渲染的 LRU。

以前每个 ``MarkdownStatic`` 自己订阅 ``theme_changed_signal``、自己在
``on_resize`` 里重排；换一次主题先是每个块各重排一遍，紧接着
``watch_theme`` 又把整个聊天区重建一遍——同一份文本被渲染两次甚至更多次。

现在宽度与主题只有一个来源：``ChatLog`` 布局变化写 ``width``，
``watch_theme`` 写 ``theme_id``（并递增 ``epoch``）。块只在 epoch 或宽度
变化时重排，渲染结果按 ``(源文本哈希, 长度, 宽度, 主题)`` 复用，
于是换回旧主题是零解析，窗口 resize 每个宽度也只解析一次。
"""

from __future__ import annotations

import weakref
from collections import OrderedDict
from typing import Any

from rich.text import Text


class RenderCache:
    """``(hash(source), len, width, theme)`` → 渲染好的 ``Text``，LRU 淘汰。

    键里带 64 位哈希和长度而不是正文本身：既不复制上万字符，也不会因为
    两条等长文本先后出现而互相顶掉（真要撞键，得哈希与长度同时相等）。
    """

    def __init__(self, capacity: int = 240) -> None:
        self._capacity = max(8, int(capacity))
        self._items: OrderedDict[tuple, Text] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(source: str, width: int, theme: str) -> tuple:
        return (hash(source), len(source), int(width), str(theme))

    def get(self, key: tuple) -> Text | None:
        text = self._items.get(key)
        if text is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return text

    def put(self, key: tuple, text: Text) -> None:
        self._items[key] = text
        self._items.move_to_end(key)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class RenderContext:
    """一整场会话共享的渲染参数；块通过 ``attach`` 订阅它的变化。"""

    def __init__(self, app: Any = None, *, capacity: int = 240) -> None:
        self._app = app
        self.width = 0
        self.theme_id = ""
        self.epoch = 0
        self.cache = RenderCache(capacity)
        self._blocks: list[weakref.ref] = []
        # (epoch, style_theme, syntax_theme)：主题 token 只在换主题时重建一次
        self._themes: tuple[int, Any, Any] | None = None

    # ---------------------------------------------------------- 订阅
    def attach(self, block) -> None:
        self._blocks = [
            ref for ref in self._blocks if ref() is not None and ref() is not block
        ]
        self._blocks.append(weakref.ref(block))

    def detach(self, block) -> None:
        self._blocks = [
            ref for ref in self._blocks if ref() is not None and ref() is not block
        ]

    def broadcast(self) -> None:
        for ref in list(self._blocks):
            block = ref()
            if block is None:
                continue
            try:
                block.on_render_context_changed()
            except Exception:  # 单个块重排失败不能拖垮整屏
                continue

    # ---------------------------------------------------------- 参数
    def set_width(self, width: int, *, broadcast: bool = True) -> bool:
        width = int(width or 0)
        if width <= 0 or width == self.width:
            return False
        self.width = width
        if broadcast:
            self.broadcast()
        return True

    def set_theme(self, theme_id: str, *, broadcast: bool = True) -> bool:
        theme_id = str(theme_id or "")
        if not theme_id or theme_id == self.theme_id:
            return False
        self.theme_id = theme_id
        self.epoch += 1
        self._themes = None
        if broadcast:
            self.broadcast()
        return True

    def invalidate(self) -> None:
        """主题变量 / 字号一类设置变了：丢弃渲染结果，所有块重排一次。"""
        self.epoch += 1
        self._themes = None
        self.cache.clear()
        self.broadcast()

    def themes(self) -> tuple[Any, Any]:
        """当前主题的 (Rich 样式表, 代码高亮配色)，按 epoch 缓存。"""
        if self._themes is not None and self._themes[0] == self.epoch:
            return self._themes[1], self._themes[2]
        from alex.ui.markdown import (  # 延迟导入：markdown 模块要引用本模块
            _markdown_style_theme,
            _markdown_syntax_theme,
        )

        style = syntax = None
        try:
            style = _markdown_style_theme(self._app)
        except Exception:  # 取不到主题样式就用默认
            pass
        try:
            syntax = _markdown_syntax_theme(self._app)
        except Exception:  # 取不到语法配色就用默认
            pass
        self._themes = (self.epoch, style, syntax)
        return style, syntax

    # ---------------------------------------------------------- 渲染
    def render(self, source: str, width: int) -> Text:
        """渲染一段 Markdown；命中缓存就只复制一份，解析与高亮都省了。"""
        from alex.ui.markdown import markdown_to_text

        key = RenderCache.key(source, width, self.theme_id)
        cached = self.cache.get(key)
        if cached is not None:
            # Text 交给控件后可能被 Rich 就地裁剪，缓存里必须留原件
            return cached.copy()
        style, syntax = self.themes()
        text = markdown_to_text(
            source, width, style_theme=style, syntax_theme=syntax
        )
        self.cache.put(key, text)
        return text.copy()
