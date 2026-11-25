---
name: alexcode
description: AlexCode 的功能说明书：TUI 命令与快捷键、模型与思考强度、权限模式、设置页（Tokens/Rules/Skills/MCP/Memory）、主题、队列与目标模式、记忆、消息回溯、子代理派发，以及新增/修改 MCP、Skill、Rule、模型的配置方法。用户问 AlexCode 有什么功能、怎么用、怎么配置或怎么扩展时使用。
---

# AlexCode 功能说明书

本技能是 AlexCode（本项目）的完整功能手册，同时说明如何通过文件扩展它自己。
所有路径都相对**项目根目录**（包含 `alex/`、`config/`、`skills/`、`rules/` 的那一层），
不要写死用户名、盘符或安装位置；需要绝对路径时先用 `Read`/`Glob` 确认再拼。

> **本技能只读**：`skills/alexcode/` 是内置技能，始终启用、不可在设置里关闭，
> 源文件被标记为只读，`Write`/`Edit`/删除与 shell 写入都会被拒绝。
> 不要尝试修改它；确实需要更新时，告知用户由用户手动解除只读后再改。

## 0. 使用约定

- 用户问“AlexCode 能做什么 / 某功能怎么用 / 怎么配置”时，依据本手册回答，不要凭空猜测。
- 用户要求改**其他**技能、规则、MCP 或模型配置时，按第 8 节操作；改完必须提醒 `/reload`。
- 不修改 `site-packages/agentscope` 框架源码，也不复制框架源码到项目里。

## 1. 界面与交互

- **启动页**：顶部 AlexCode 字符画（随主题上下渐变），下面是 Skills / Session /
  Daemon / Model / MCP 五个阶段的进度；加载完成前输入被锁定。
- **顶栏**：显示当前模型、思考强度、权限模式与队列状态；主题名显示在状态栏。
- **输入框**：`Enter` 发送，`Shift+Enter` 换行，最多显示 6 行；输入 `/` 打开命令面板。
- **快捷键**：`Ctrl+T` 循环主题，`Ctrl+L` 清屏，`Ctrl+O` 开关调试日志，
  `Ctrl+Q` 退出，`Ctrl+V` / `Alt+V` / `F6` 粘贴剪贴板截图。
- **点击交互**：点击用户消息可恢复到该条消息（可复制后重新编辑）；
  点击每轮回复下方的变更摘要可查看文件 diff。
- **Esc**：生成中按 Esc 停止回复；有弹窗时 Esc 关闭弹窗。
- 同一台机器只允许运行一个 AlexCode 实例；重复启动会提示并退出。

## 2. 斜杠命令

| 命令 | 作用 |
|---|---|
| `/model` | 选择模型，再选择该模型的思考强度 |
| `/permission` | 选择权限模式（accept / bypass / explore） |
| `/settings` | 打开设置页（Tokens / Rules / Skills / MCP / Memory） |
| `/cd` | 切换工作区（带目录选择器） |
| `/theme` | 切换主题（也可 `Ctrl+T`） |
| `/reload` | 重载配置、技能、规则、MCP 与模型 |
| `/compact` | 手动压缩上下文 |
| `/queue` | 把请求拆成多个任务并串行执行，如 `/queue 任务A; 任务B` |
| `/goal` | 目标模式：执行者与独立验收者循环，直到目标通过校验 |
| `/image` | 发送图片，如 `/image <路径> [说明]` |
| `/init memory` | 清空长期记忆 |
| `/clear` | 清空当前会话 |
| `/help` | 显示命令速查 |
| `/exit` | 退出 |

## 3. 模型与思考强度

- 支持三类 provider：`deepseek`、`openai`（及 `openai-compatible`）、`custom`（公司内网网关）。
- 思考强度档位按 provider 预设：
  - deepseek：`off` / `high` / `max`（映射 `thinking_enable` 与 `reasoning_effort`）
  - openai：`none` / `minimal` / `low` / `medium` / `high` / `xhigh`
  - custom：默认 `low` / `medium` / `high`，可在 profile 的 `reasoning.levels` 里覆盖
- 网关实际是 OpenAI 协议时，在 profile 里加 `"canonical": "openai"`，档位与参数就按 OpenAI 走。
- 顶栏模型名后直接显示当前档位（如 `deepseek-flash · high`）；`/model` 里选过的档位按模型名记忆。
- 模型配置写在 `config/config.json` 的 `model` 段，可选项在 `model_profiles` 中预置。

## 4. 权限模式

- `accept`：文件修改与危险命令需要确认，其余自动放行。
- `bypass`：全部自动放行。
- `explore`：只读，任何修改都被拒绝。
- 危险命令由 `config/config.json` 的 `dangerous_commands` 正则判断；确认框可选
  允许一次 / 总是允许 / 拒绝。

## 5. 设置页

`/settings` 打开，分类顺序固定为 **Tokens、Rules、Skills、MCP、Memory**。

- **Tokens**：上下文窗口、已用比例、会话累计输入/输出/缓存。
- **Rules**：`rules/*.md` 列表，可启用/排除、点开阅读全文。
- **Skills**：`skills/*/SKILL.md` 列表，可启用/排除、点开阅读全文；
  `alexcode` 为只读条目，开关锁定且始终启用。
- **MCP**：服务器连接状态与每个工具的开合。
- **Memory**：`MEMORY.md`、`LESSONS.md` 与每日日志，可点开阅读。

关闭设置页后，技能/规则的改动会重建 agent；其他改动走一次 `/reload`。

## 6. 主题

内置五套：Crimson（默认）、Dark、Light、Mono（白底黑线）、Ember（暖橙）。
`/theme` 弹窗选择或 `Ctrl+T` 循环，选择结果持久化在 `data/tui.json`，重启保持。
启动页字符画、阶段圆点、进度条都会跟随主题色。

## 7. 记忆、队列与回溯

- **记忆**：每轮任务流水写入 `data/Memory/memory/YYYY-MM-DD.md`，后台定期合并到
  `MEMORY.md`；上下文压缩时沉淀的长期经验写入 `LESSONS.md`。回答“最近做过什么”先读 `MEMORY.md`。
- **队列**：`/queue 任务A; 任务B; 任务C` 串行执行，规划完成后等用户确认开始；
  顶部任务栏显示进度，中断后已完成的任务不会叠加到下一轮。规划按"可独立验收的单元"
  拆：一个 test case / 一个文件 / 一个 endpoint 算一个单元，大致一单元一个任务
  （同文件同操作才合并，并在标题里写明覆盖的单元），所以 100 个 case 会给 20-100 个
  任务点，而不是 7-8 个大任务。
- **目标模式**：`/goal <目标>` 启用执行/验收循环，验收者只读检查工作区，不通过就继续修。
- **回溯**：点击任一用户消息，选择恢复到该条消息；会话快照保存在 `data/snapshots`，
  但不会改动框架源码。历史恢复后可以继续对话或重新编辑。
- **图片**：`/image <路径> [说明]` 或直接粘贴截图；输入框里的 `[image-N]` 占位符可整体退格删除。

## 8. 扩展 AlexCode

### 8.1 通用流程

1. **收集信息**：缺关键参数先问用户（MCP 启动命令/URL、模型的 provider/base_url/密钥变量名）。
2. **读现状**：先 `Read` 目标文件，避免覆盖已有配置。
3. **写文件**：用 `Write`/`Edit`，保持 JSON 合法、YAML frontmatter 完整。
4. **自检**：重新 `Read` 一次，并对 JSON 做格式校验（用平台自带解析器或临时脚本，不要内联表达式）。
5. **提醒 reload（硬性要求）**：动了 MCP / Skill / Rule / 模型配置，回复里必须明确要求
   `/reload` 或重启，见 8.5。
6. **验证**：重载后让用户发一句会触发新能力的请求；失败按第 9 节排查。

### 8.2 新增 MCP

文件：`config/mcp_servers.json`

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "<要授权的目录>"]
    },
    "remote-tools": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer xxx"}
    }
  }
}
```

- `type` 缺省为 `stdio`；`stdio` 必须有 `command`，可选 `args`、`env`；`http` 必须有 `url`，可选 `headers`。
- 名称用简短英文小写，它会成为工具名前缀 `mcp__<名称>__<工具名>`。
- 新增前确认 `command` 在本机可用（Windows：`where npx`；类 Unix：`command -v npx`）。
- 只做追加，不要删除用户已有的 server 配置；单个服务失败只跳过该服务，错误写在 `data/mcp.log`。

### 8.3 新增 Skill

目录：`skills/<name>/SKILL.md`

```markdown
---
name: my-skill
description: 一句话说明什么时候该用这个技能（模型判断是否调用的唯一依据）
---

# 正文：具体步骤、命令、注意事项
```

- `name` 与目录名一致（小写、连字符），frontmatter 必须有 `name` 和 `description`。
- 正文是给模型看的操作手册：步骤化、可执行、包含验证方式；不写死本机路径和密钥。
- 写完后提醒 `/reload`，再用 `Skill` 工具以 `{"skill": "my-skill"}` 读取验证。

### 8.4 新增 Rule 或切换模型

- 规则：`rules/<topic>.md`，纯 Markdown、无 frontmatter，每次请求都会拼进 system prompt；
  只写长期有效的约束，越短越好；一条规则一个文件。
- 模型：改 `config/config.json` 的 `model` 段（`provider`、`name`、`base_url`、`api_key_env`），
  密钥只写进 `.env`（应在 `.gitignore` 中），不要写进 `config.json`，也不要打印在对话里。
- 需要新增 provider 分支时，先在 `alex/agent.py` 的 `build_model()` 里用框架自带模型类接入，
  禁止复制框架源码；此类代码改动前必须先征得用户同意。

### 8.5 默认能力约定

- **联网搜索**：优先内置 `WebSearch`（无需 API Key），需要正文时用 `Cmd` 的 `curl.exe`；
  有反爬或必须执行 JS 时再用浏览器类 MCP。
- **命令执行**：Windows 优先 `Cmd`（cmd.exe，UTF-8），需要对象管道时用 `PowerShell`；类 Unix 用 `Bash`。
- **工作区**：默认 `agent_workspace/`，用 `/cd` 切换；文件操作优先专用工具，不要用 shell 代替。

## 9. 子代理派发

主代理拥有 `Subagent` 工具，可以派发独立子代理。没有预设角色：角色、职责和任务全部由
`prompt` 定义。子代理不能再派发（最多两层：主代理 + 一层子代理）。

**什么时候派发**
- 子任务独立、自包含且需要多步工具调用，例如探索陌生代码库、独立调研、批量检查。
- 中间产物（大量搜索结果、长文件、日志）不必留在主对话里，派发可以保护主上下文。
- 多个互不依赖的任务需要同时推进。

**什么时候不要派发**
- 一步就能完成的事：读一个文件、跑一条命令、回答一个概念问题。
- 需要和用户来回确认、或依赖用户偏好判断的任务。
- 子任务依赖主对话中大量隐含上下文，无法用一段完整 prompt 描述清楚。

**调用方式**：`Subagent(description, prompt)`

- `description`：3-6 个词的简短标签，只用于界面展示。
- `prompt`：必须自包含，至少写清：角色、目标与背景、涉及的路径或范围、验收标准、输出格式。

**并行与约定**
- 同一条回复里多次调用 `Subagent` 会并发执行，每次都是全新实例，上下文互不共享。
- 需要 N 个并行任务就发 N 个调用，不要逐个等待；串行派发会让总耗时叠加。
- 子代理拥有与主代理相同的 rules、skills 和 MCP 工具，可在 `prompt` 里直接要求它使用某个技能或工具。
- 子代理只返回最终文本；需要细节时在 `prompt` 里明确要求，不要要求它继续派发。
- 收到结果后由主代理核对、整合，不要把子代理结论直接当作已验证事实。

## 10. 生效方式与排查

| 改动 | 生效方式 |
|---|---|
| `config/mcp_servers.json` | `/reload` |
| `skills/*/SKILL.md` | `/reload` |
| `rules/*.md` | `/reload` |
| `config/config.json` 的 model / workdir | `/reload` |
| `config/config.json` 新增字段、`alex/` 代码 | 重启应用 |

**提醒 reload 的硬性要求**：以上任何改动都只写了磁盘，运行中的进程不会自动感知。
收尾时必须单独一段写出“请在对话框输入 `/reload`”（结构性改动写“重启应用”），
说明本会话还看不到新能力，并给出判断依据：

- 底部提示 `Reloaded: N MCP server(s) · <模型名>`，N 应等于配置里的 server 数量；
- 设置页 MCP 分类里新服务显示为已连接；
- 新增 skill 后可用 `Skill` 工具读取验证。

不要用“已生效 / 已经装好”描述只写入文件的状态，最多说“配置已写入，待 reload 生效”。

常见问题：
- MCP 未出现：先确认是否真的执行过 `/reload`；再查 JSON 合法性、`command` 是否存在、`data/mcp.log`。
- Skill 未出现：确认 `skills/<name>/SKILL.md` 存在且 frontmatter 同时有 `name`、`description`。
- 规则未生效：确认在 `rules/` 根目录、扩展名 `.md`，并且已 `/reload`。
- 模型 401/404：核对 `.env` 键名与 `api_key_env`、`base_url` 是否要 `/v1` 后缀、模型名是否真实存在。

## 11. 安全边界

- 只改项目目录内的配置和内容，不动框架源码。
- 未经用户确认不删除已有配置；写 `.env` 只追加/替换目标键。
- 新 MCP、新模型等会改变计费、权限或外部网络访问的改动，先向用户确认一次。
- `skills/alexcode/` 为只读：设置里不能关闭，`Write`/`Edit`/删除会被拒绝，
  shell 里针对该目录的写操作也会被拒绝；文件本身带只读属性。
  需要更新本手册时，请让用户先手动解除只读，再修改。
