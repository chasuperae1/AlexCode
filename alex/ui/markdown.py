"""Markdown 渲染的小工具：框线表格转管道表、纯文本化。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pygments.token import Token
from rich import box
from rich.console import Console
from rich.markdown import CodeBlock
from rich.markdown import Markdown as RichMarkdown
from rich.markdown import TableElement
from rich.padding import Padding
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
from rich.table import Table
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.widgets import Static

from alex.ui.constants import _BOX_END, _BOX_MID, _BOX_START, _BOX_V


class _FullBorderTable(TableElement):
    """Rich Markdown 默认用 SIMPLE 框线（只有横线），这里统一成全边框网格。"""

    def __rich_console__(self, console, options):
        table = Table(
            box=box.SQUARE,
            show_lines=True,
            pad_edge=False,
            style="markdown.table.border",
            show_edge=True,
            collapse_padding=True,
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading, overflow="fold")
        if self.body is not None:
            for row in self.body.rows:
                row_content = [element.content for element in row.cells]
                table.add_row(*row_content)
        yield table


class _ThemedCodeBlock(CodeBlock):
    """代码块底色贴合代码宽度，不拉满整个正文宽度。"""

    def __rich_console__(self, console, options):
        code = str(self.text).rstrip()
        syntax = Syntax(
            code,
            self.lexer_name,
            theme=self.theme,
            word_wrap=True,
            padding=0,
        )
        # Rich 默认的 Syntax padding 会 expand 到整行；评审稿里的代码块
        # 是一张跟代码等宽的小卡片，所以这里用 expand=False 的 Padding。
        yield Padding(
            syntax,
            pad=(1, 1),
            style=syntax._get_base_style(),
            expand=False,
        )


class _FullBorderMarkdown(RichMarkdown):
    """所有管道表格都走全边框渲染。"""

    elements = dict(RichMarkdown.elements)
    elements["table_open"] = _FullBorderTable
    elements["fence"] = _ThemedCodeBlock
    elements["code_block"] = _ThemedCodeBlock


@dataclass(frozen=True)
class SyntaxPalette:
    """代码块高亮用到的 8 个颜色 token。"""

    background: str
    text: str
    key: str
    string: str
    number: str
    comment: str
    punct: str
    error: str


class _MarkdownSyntaxTheme(SyntaxTheme):
    """把主题 token 映射到 Rich/Pygments 的语法高亮，保持代码块底色一致。"""

    def __init__(self, palette: SyntaxPalette) -> None:
        background = palette.background
        text = palette.text
        key = palette.key
        string = palette.string
        number = palette.number
        comment = palette.comment
        punct = palette.punct
        error = palette.error
        self.background = background

        def tone(color: str, *, bold: bool = False, italic: bool = False) -> Style:
            return Style(
                color=color,
                bgcolor=background,
                bold=bold,
                italic=italic,
                meta={"alex-markdown-code-block": True},
            )

        self._default = tone(text)
        self._styles = {
            Token.Text: self._default,
            Token.Text.Whitespace: Style(
                bgcolor=background,
                meta={"alex-markdown-code-block": True},
            ),
            Token.Name: self._default,
            Token.Name.Tag: tone(key),
            Token.Name.Attribute: tone(key),
            Token.Name.Builtin: tone(key),
            Token.Name.Class: tone(key),
            Token.Name.Function: tone(key),
            Token.Keyword: tone(key, bold=True),
            Token.Literal.String: tone(string),
            Token.Literal.Number: tone(number),
            Token.Comment: tone(comment, italic=True),
            Token.Operator: tone(punct),
            Token.Punctuation: tone(punct),
            Token.Error: tone(error),
        }

    def get_style_for_token(self, token_type) -> Style:
        while token_type is not None:
            style = self._styles.get(token_type)
            if style is not None:
                return style
            token_type = token_type.parent
        return self._default

    def get_background_style(self) -> Style:
        return Style(
            bgcolor=self.background,
            meta={"alex-markdown-code-block": True},
        )


def _box_rows_to_markdown(block: list[str]) -> str | None:
    """把一段框线表格转成 Markdown 管道表格；结构不合法时返回 None。"""
    groups: list[list[list[str]]] = [[]]
    for raw in block:
        stripped = raw.strip()
        if not stripped or stripped[0] in _BOX_START | _BOX_END:
            continue
        if stripped[0] in _BOX_MID:
            groups.append([])
            continue
        if stripped[0] not in _BOX_V:
            return None
        cells = [cell.strip() for cell in re.split(r"[│║]", stripped.strip("│║"))]
        rows = groups[-1]
        if rows and not cells[0] and any(cells[1:]):
            # 单元格内换行：首列为空、其余列有内容的行并入上一行
            cells.extend("" for _ in range(len(rows[-1]) - len(cells)))
            rows[-1] = [
                f"{old} {new}".strip() if new else old
                for old, new in zip(rows[-1], cells)
            ]
        else:
            rows.append(cells)
    groups = [rows for rows in groups if rows]
    if not groups:
        return None
    header = groups[0][0]
    body = groups[0][1:]
    for rows in groups[1:]:
        body.extend(rows)
    if not any(header) or not body:
        return None
    columns = len(header)

    def line(cells: list[str]) -> str:
        cells = (cells + [""] * columns)[:columns]
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"

    divider = "| " + " | ".join("---" for _ in range(columns)) + " |"
    return "\n".join([line(header), divider] + [line(row) for row in body])


def normalize_tables(source: str) -> str:
    """把手绘框线表格转成 Markdown 管道表格。

    Rich 的 Markdown 渲染会把连续的框线行当成普通段落、按空格合并，
    终端里就变成错位的长行；管道表格才能正常排版。
    """
    if not any(char in source for char in _BOX_START):
        return source
    lines = source.splitlines()
    out: list[str] = []
    index = 0
    fenced = False
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
        elif not fenced and stripped[:1] in _BOX_START:
            end = index + 1
            while end < len(lines) and lines[end].lstrip()[:1] not in _BOX_END:
                end += 1
            if end < len(lines):
                table = _box_rows_to_markdown(lines[index : end + 1])
                if table:
                    if out and out[-1].strip():
                        out.append("")
                    out.extend([table, ""])
                    index = end + 1
                    continue
        out.append(line)
        index += 1
    return "\n".join(out)


def _markdown_style_theme(app) -> RichTheme | None:
    """把主题自定义的 Markdown token 交给 Rich，避免继承默认样式。"""
    if app is None:
        return None
    try:
        current = app.current_theme
        variables = app.theme_variables
    except Exception:
        return None
    if current is None:
        return None
    code_bg = variables.get("markdown-code-bg")
    code_fg = variables.get("markdown-code-fg")
    table_border = variables.get("markdown-table-border")
    if not (code_bg and code_fg and table_border):
        return None
    styles = {
        "markdown.code": f"{code_fg} on {code_bg}",
        "markdown.code_block": f"{code_fg} on {code_bg}",
        "markdown.table.border": table_border,
    }
    foreground = variables.get("foreground") or getattr(current, "foreground", None)
    if foreground:
        styles["markdown.table.header"] = f"bold {foreground}"
    return RichTheme(styles)


def _markdown_syntax_theme(app) -> _MarkdownSyntaxTheme | None:
    """Build the fenced-code syntax theme from the active Alex theme."""
    if app is None:
        return None
    try:
        current = app.current_theme
        variables = app.theme_variables
    except Exception:
        return None
    if current is None:
        return None
    background = variables.get("markdown-code-bg")
    text = variables.get("markdown-code-fg") or variables.get("foreground")
    key = variables.get("markdown-code-key") or variables.get("accent") or text
    string = variables.get("markdown-code-string") or variables.get("success") or text
    number = variables.get("markdown-code-number") or variables.get("warning") or text
    comment = variables.get("markdown-code-comment") or variables.get("secondary") or text
    punct = variables.get("markdown-code-punct") or variables.get("secondary") or text
    error = variables.get("markdown-code-error") or variables.get("error") or text
    if not all((background, text, key, string, number, comment, punct, error)):
        return None
    return _MarkdownSyntaxTheme(
        SyntaxPalette(
            background=background,
            text=text,
            key=key,
            string=string,
            number=number,
            comment=comment,
            punct=punct,
            error=error,
        )
    )


def markdown_to_text(
    source: str,
    width: int,
    style_theme: RichTheme | None = None,
    syntax_theme: SyntaxTheme | None = None,
) -> Text:
    """把 Markdown 渲染成等宽排版后的 ``Text``。

    Textual 会把 Rich 渲染对象包成 ``RichVisual``，既不画选区高亮、也拿不到
    选区文本；转成 ``Text``（``Content`` 视觉）后由框架原生支持选中与复制。
    """
    source = normalize_tables(source)

    console = Console(
        width=width,
        color_system="truecolor",
        force_terminal=False,
        legacy_windows=False,
        highlight=False,
        theme=style_theme,
    )
    markdown = (
        _FullBorderMarkdown(source)
        if syntax_theme is None
        else _FullBorderMarkdown(source, code_theme=syntax_theme)
    )
    try:
        lines = console.render_lines(
            markdown,
            console.options.update(width=width),
            pad=False,
        )
    except Exception:
        return Text(source)
    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        source_line = "".join(segment.text for segment in line)
        # 代码块的每个 segment 都带自定义 meta；行内代码没有这个标记，
        # 因此即使整行只有行内代码，也不会被误铺成整行底色。
        text_segments = [segment for segment in line if segment.text]
        is_code_line = bool(text_segments) and all(
            segment.style is not None
            and (segment.style.meta or {}).get("alex-markdown-code-block")
            for segment in text_segments
        )
        keep = len(source_line) if is_code_line else len(source_line.rstrip())
        if keep <= 0:
            continue
        for segment in line:
            if keep <= 0:
                break
            part = segment.text[:keep]
            text.append(part, segment.style)
            keep -= len(part)
    return text


class MarkdownStatic(Static):
    """Markdown 文本块：渲染为 ``Text``，可直接选中复制；流式阶段接收 ``Text``。

    ``width`` 用于历史重建：此时控件还没挂载、拿不到 ``content_region``，
    但聊天区宽度已经确定，提前按这个宽度排好版，挂上去的第一帧就有正文，
    不会出现“整块空白再慢慢填字”的闪烁。

    宽度与主题都不再由这个块自己维护：归属的 ``RenderContext`` 统一广播
    变化，渲染结果走它的 LRU，换主题 / 拖窗口不再让每个块各解析一遍。
    """

    def __init__(
        self,
        renderable="",
        *,
        app=None,
        width: int = 0,
        context=None,
        **kwargs,
    ) -> None:
        super().__init__(Text(""), **kwargs)
        self._markdown: str | None = None
        self._render_width = -1
        self._render_epoch = -1
        self._host_app = app
        self._hint_width = int(width or 0)
        # 注意：不能叫 ``_context``——``MessagePump._context`` 是消息泵自己
        # 的上下文管理器，同名实例属性会把它覆盖掉，挂载时直接 TypeError。
        self._render_ctx = context
        self.update(renderable)

    @property
    def render_context(self):
        """优先用构造时给的上下文，其次是宿主 app 上的会话级上下文。"""
        if self._render_ctx is not None:
            return self._render_ctx
        ctx = getattr(self._host_app, "render_context", None)
        if ctx is not None:
            return ctx
        try:
            return getattr(self.app, "render_context", None)
        except Exception:  # 还没挂到 app 上
            return None

    def update(self, renderable="", **kwargs) -> None:
        if isinstance(renderable, str):
            self._markdown = renderable
            self._render_width = -1
            self._render_markdown()
            return
        self._markdown = None
        super().update(renderable, **kwargs)

    def on_mount(self) -> None:
        context = self.render_context
        if context is not None:
            context.attach(self)
        # 已经按历史重建时的宽度排好版就不要重排一遍，宽度不变时渲染结果相同。
        self._render_markdown()
        # 刚挂载时 content_region 还是 0，拿不到宽度就只画出一个空行；
        # 挂在滚动容器末尾的块（新消息、翻回最新）随后尺寸恰好不变，
        # Textual 不会再发 Resize，这一块就永远空白。布局完成后再补一次。
        self.call_after_refresh(self._render_after_layout)

    def on_unmount(self) -> None:
        context = self.render_context
        if context is not None:
            context.detach(self)

    def on_render_context_changed(self) -> None:
        """上下文换了宽度或主题：按缓存重排一次。"""
        self._render_markdown()

    def _render_after_layout(self) -> None:
        """首帧布局后再排一次：mount 时宽度未知的块靠这里补渲染。"""
        if not self.is_mounted:
            return
        self._render_markdown()

    def on_resize(self, event) -> None:
        # 块自己的宽度跟聊天区不一致时（缩进、卡片内嵌）以实测宽度为准，
        # 否则长行折行位置会错；与上下文宽度一致时这次调用是空转。
        if self._markdown is not None:
            self._render_markdown(event.size.width)

    def _render_markdown(self, width: int | None = None) -> None:
        if self._markdown is None:
            return
        context = self.render_context
        if width is None:
            measured = 0
            try:
                if self.is_mounted:
                    measured = int(
                        self.content_region.width or self.size.width or 0
                    )
            except Exception:  # 布局信息还没就绪
                measured = 0
            width = measured or (context.width if context is not None else 0)
        if width <= 0:
            # 还没布局：历史重建时用已知的聊天区宽度先排好，别的场景保持原行为。
            width = self._hint_width
        if width <= 0:
            return
        epoch = context.epoch if context is not None else -1
        if width == self._render_width and epoch == self._render_epoch:
            return
        self._render_width = width
        self._render_epoch = epoch
        if context is not None:
            super().update(context.render(self._markdown, width))
            return
        host = self._host_app
        if host is None:
            try:
                host = self.app
            except Exception:
                host = None
        style_theme = None
        syntax_theme = None
        try:
            style_theme = _markdown_style_theme(host)
        except Exception:  # 取不到主题样式就用默认
            pass
        try:
            syntax_theme = _markdown_syntax_theme(host)
        except Exception:  # 取不到语法配色就用默认
            pass
        super().update(
            markdown_to_text(
                self._markdown,
                width,
                style_theme=style_theme,
                syntax_theme=syntax_theme,
            )
        )
