# termui

Claude Code 风格的终端 UI 组件库。基于 [Textual](https://textual.textualize.io/) 构建，提供虚拟化列表、多选框、输入框、进度条、层级菜单、状态面板六类核心组件，以及可切换的主题系统。

## 特性

- **流畅渲染**：列表采用虚拟化绘制（`ScrollView` + `render_line`），10 万条目下单帧渲染约 1ms，光标移动与滚动使用缓动动画
- **键盘优先**：全部组件支持方向键导航、快捷键操作，无需鼠标
- **响应式布局**：窗口宽度变化时自动在左右分栏 / 上下堆叠之间切换
- **主题系统**：内置 3 套主题，`Ctrl+T` 实时切换，组件样式全部引用主题变量
- **可扩展**：组件样式类（COMPONENT_CLASSES）+ `DEFAULT_CSS` 双层覆盖机制
- **可测试**：15 项自动化测试，基于 Textual Pilot 无头驱动

## 安装与运行

```bash
pip install textual>=8.2

# 运行演示
python -m termui.demo
# 或
python -m termui
```

演示程序快捷键：

| 按键 | 作用 |
|---|---|
| `↑` `↓` | 列表/菜单/多选框内移动 |
| `→` `←` | 展开 / 折叠层级菜单 |
| `空格` | 勾选/取消勾选；在进度条上暂停动画 |
| `A` | 多选框全选 / 全不选 |
| `Enter` | 确认选中 / 激活菜单项 / 提交输入 |
| `Tab` | 在组件间切换焦点 |
| `Ctrl+T` | 切换主题 |
| `Ctrl+R` | 进度条随机跳转 |
| `F1` | 显示快捷键帮助 |
| `Ctrl+Q` | 退出 |

输入框内支持 `↑`/`↓` 翻历史、`Ctrl+L` 清空；输入 `/theme`、`/expand`、`/collapse`、`/clear`、`/help` 可直接控制演示界面。

## 接入 Alex

项目内的 `alex/tui.py` 是完整的接入示例：`python -m alex.tui` 启动对话界面。

- 聊天区：`ChatLog`（滚动容器 + 增量挂载控件），AI 回复用 `Markdown` 控件渲染，用户消息带背景气泡（`.user-bubble`），流式时只更新当前消息控件（50ms 节流），滚动条已隐藏
- 消息结构：一轮对话 = 用户气泡 → 可折叠调试组（思考/工具日志，默认收起，`Ctrl+O` 展开）→ Markdown 回答；回答容器延迟到首个文本 delta 才挂载，避免出现在调试组上方
- 元信息行：当前模型 + 上下文 token（已用/总量、百分比、距压缩阈值），由 `Model.count_tokens` 实测
- 命令：`/model [名称]` 切换模型（读 `config/config.json` 的 `model_profiles` 并持久化）、`/reload`、`/clear`、`/theme`、`/tasks`、`/exit`
- 事件流：直接消费 AgentScope 原生事件——`ThinkingBlock*` 展示思考过程、`ToolCallStart/ToolResultEnd` 展示工具与耗时、`mcp__server__tool` 识别为 MCP 调用、`Skill` 工具展示读取的技能名；启动时列出已加载的规则与技能
- 任务摘要：单行显示「Tasks 2/3 · running: xxx」，无任务时自动隐藏
- 底部：`PromptInput` 输入 + 元信息行（模型/上下文）；当前动作显示在思考/工具折叠组的标题上，展开才看详细内容
- 只有 `ReplyEndEvent` 之后才保存会话，避免中断产生残缺上下文

## 组件

### ScrollList 可滚动列表

```python
from termui import ScrollList, ListItem

scroll_list = ScrollList([
    ListItem("构建项目", "编译 + 打包", value="build"),
    ListItem("部署", "暂未配置", value="deploy", disabled=True),
])
```

| 成员 | 说明 |
|---|---|
| `items` / `current` | 条目列表 / 当前光标条目 |
| `set_items(items)` | 替换数据并重置光标 |
| `cursor_index` | 当前光标索引 |
| `ScrollList.Highlighted` | 光标移动消息 |
| `ScrollList.Selected` | 回车选中消息 |

### CheckboxGroup 多选框

```python
from termui import CheckboxGroup

group = CheckboxGroup(
    ["打包产物", ("上传镜像", "upload"), ("通知团队", "notify", True)],
    title="附加步骤",
)
```

选项支持三种写法：`"文本"`、`(文本, 值)`、`(文本, 值, 是否禁用)`。消息：`SelectionChanged`、`Submitted`。

### PromptInput 输入框

```python
from termui import PromptInput

prompt = PromptInput(
    placeholder="输入命令",
    validator=lambda value: "不能为空" if not value else None,
    history=["上一条命令"],
)
```

校验失败时自动标红并发出 `Rejected(value, reason)`；通过后发出 `Submitted(value)` 并写入历史。

### ProgressBar 进度条

```python
from termui import ProgressBar

bar = ProgressBar(label="构建进度")
bar.advance_to(65)          # 平滑动画到 65%
bar.indeterminate = True    # 不确定进度模式
```

### HierarchyMenu 层级菜单

```python
from termui import HierarchyMenu, MenuNode

menu = HierarchyMenu([
    MenuNode("文件", expanded=True, children=[
        MenuNode("新建", value="Ctrl+N"),
        MenuNode("最近使用", children=[MenuNode("main.py")]),
    ]),
])
```

`Activated(path, node)` 在叶子节点回车时触发，`path` 是完整层级路径。

### StatusPanel 状态面板

```python
from termui import StatusPanel

status = StatusPanel(text="就绪")
status.notify("保存成功", level="success", timeout=3)
status.busy = True   # 显示旋转指示器
```

级别：`info` / `success` / `warning` / `error`，颜色取自主题。

## 主题

```python
from termui import TermUIApp, ThemeSpec, THEMES, register_themes

# 方式一：使用内置主题（slate / slate-light / high-contrast）
class MyApp(TermUIApp):
    ...

# 方式二：自定义
THEMES["mine"] = ThemeSpec(
    name="mine",
    primary="#7aa2f7", secondary="#565f89", accent="#bb9af7",
    success="#9ece6a", warning="#e0af68", error="#f7768e",
    foreground="#c0caf5", background="#1a1b26",
    surface="#16161e", panel="#1f2335",
)
```

组件 CSS 只引用 `$primary` / `$surface` / `$panel` / `$text-muted` 等主题变量，所以切换 `app.theme` 后所有已挂载组件立即重新着色。

## 架构

```
termui/
├── theme.py                 # ThemeSpec + 主题注册/切换
├── app.py                   # TermUIApp 基类（注册主题、Ctrl+T）
├── demo.py                  # 演示程序
└── widgets/
    ├── scroll_list.py       # 虚拟化列表
    ├── checkbox_group.py    # 多选框
    ├── prompt_input.py      # 输入框
    ├── progress.py          # 进度条
    ├── hierarchy_menu.py    # 层级菜单
    └── status_panel.py      # 状态面板
```

分层：主题层（设计变量）→ 组件层（状态 + 键盘 + 消息）→ 应用层（组装与交互）。组件之间不直接依赖，统一通过 Textual 消息通信。

## 自定义与扩展

1. **改单个组件外观**：继承后覆盖 `DEFAULT_CSS`
2. **细粒度改色**：覆盖 `COMPONENT_CLASSES` 对应的样式类
3. **新增组件**：实现 `render()`（普通组件）或 `render_line()`（滚动类组件），状态变化时 `refresh()`

```python
from termui import ScrollList

class CompactList(ScrollList):
    DEFAULT_CSS = """
    CompactList { border: none; background: $background; }
    """
```

## 测试

```bash
pip install pytest pytest-asyncio
python -m pytest tests -q
```

覆盖：列表导航/选中/禁用项、多选全选、输入校验与历史、进度动画与键盘微调、菜单展开折叠与激活、状态提示自动消失、主题循环、窄屏响应式、10 万条目渲染性能。

## 性能

- 列表只渲染可视行，`virtual_size` 与实际条目数解耦
- 本机实测：10 万条目、30 行可视区域，单帧渲染约 **1.08ms**（预算 16.7ms）
- 动画交给 Textual animator，无需自建帧循环
- 状态面板的旋转指示器仅在 `busy` 期间以 15Hz 刷新，空闲时零开销

## 终端兼容性

Textual 负责底层终端能力（备用屏幕、鼠标、真彩色、resize），组件层不直接输出 ANSI 序列。

| 终端 | 状态 |
|---|---|
| Windows Terminal | 已在 Windows 11 实测（演示程序 + 全部测试通过） |
| VS Code 集成终端 | 已在 Windows 11 实测 |
| iTerm2 / macOS Terminal | 依赖 Textual 的跨平台渲染，未在本机（Windows）实测 |
| 无真彩色终端 | Textual 自动降级为 256 色，主题仍可用 |

已知限制：Windows 传统 `conhost` 在窗口缩放时偶有重绘延迟，建议使用 Windows Terminal。
