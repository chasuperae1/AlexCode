# AlexCode

> 一个会自动反思、越用越聪明的终端 AI 助手。

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#快速开始)

AlexCode 是一个跑在终端里的 AI 编程/办公助手：Textual 构建的 TUI、带权限确认的工具调用、
子代理并发、会话快照回溯，以及一套**压缩后自动反思**的记忆机制——每压缩一次上下文，
它就把这一轮踩过的坑提炼成长期经验，下一轮直接带着这些经验开工。

*AlexCode is a terminal AI assistant built on [AgentScope](https://github.com/agentscope-ai/agentscope).
It keeps getting better because it reflects after every context compaction and turns the
experience into reusable lessons.*

## 特性

| 能力 | 说明 |
|---|---|
| **压缩后反思** | 上下文触发压缩（默认 80%）后异步提炼经验写入 `LESSONS.md`，下一轮注入 system prompt，越用越准 |
| **长期记忆** | 每轮任务流水按日归档，后台定期合并成 `MEMORY.md`；支持清空、裁剪、按日期回溯 |
| **权限模式** | `accept` / `bypass` / `explore` 三档；危险命令由正则表拦截，改动文件前弹内联 diff 卡确认 |
| **目标模式** | `/goal <目标>` 启动「执行者 + 独立验收者」循环，验收不通过就继续修，直到达标 |
| **任务队列** | `/queue 任务A; 任务B` 串行推进多个独立任务，顶部任务栏显示进度 |
| **子代理** | 主代理可派发独立子代理，同一条回复里多次派发即并发执行，互不污染上下文 |
| **MCP** | 按 `config/mcp_servers.json` 并发连接 stdio / http 服务器，单个失败只跳过该服务 |
| **Skills / Rules** | `skills/*/SKILL.md` 按需加载，`rules/*.md` 拼进系统提示；设置页可逐个开关 |
| **快照回溯** | 每轮生成工作区快照，点击历史消息即可恢复文件与对话到那一刻 |
| **五套主题** | Crimson / Dark / Light / Mono / Ember，`Ctrl+T` 循环，选择持久化 |
| **图片输入** | `/image <路径>` 或直接粘贴剪贴板截图 |
| **多模型** | deepseek / openai / openai-compatible / 自建网关，思考强度分档可调 |

## 界面速览

```
┌──────────────────────────────────────────────────────────────┐
│ deepseek-flash · high      accept      队列 0/0      Crimson │
├──────────────────────────────────────────────────────────────┤
│ 你 把工作区的默认路径改成相对路径，别写死盘符                    │
│                                                              │
│ AlexCode  ├ Read alex/config.py                              │
│           ├ Edit alex/config.py  (+2 −2)                     │
│           └ Cmd py -3 -m compileall -q alex                  │
│           编译通过                                            │
├──────────────────────────────────────────────────────────────┤
│ > /                                                          │
└──────────────────────────────────────────────────────────────┘
```

## 快速开始

需要 Python 3.10+（推荐 3.11–3.13）。默认模型走 DeepSeek，需要一个 API Key。

```powershell
git clone https://github.com/chasuperae1/AlexCode.git
cd AlexCode

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env     # 然后填入 API_KEY
```

Windows 下双击 `start.bat` 即可（它会自己找解释器）；也可以直接：

```powershell
python -m alex.tui    # TUI
python -m alex.cli    # 纯 REPL
```

Linux / macOS：`python -m alex.tui`，把 `start.bat` 里的解释器探测换成你自己的 shell 脚本即可。

首次启动会自动创建 `agent_workspace/`、`data/` 等运行时目录，不会写入项目源码。

## 配置

### `.env`

```ini
API_KEY=sk-xxx          # 模型服务商密钥，不要提交
MODEL_NAME=deepseek-flash
```

密钥只放 `.env`（已在 `.gitignore` 中），`config/config.json` 只记录**变量名** `api_key_env`。

### `config/config.json`

| 字段 | 作用 |
|---|---|
| `model.provider` / `name` / `base_url` | 当前模型；`model_profiles` 里预置了可切换的档位 |
| `model.context_size` / `context_trigger_ratio` | 上下文窗口与压缩触发比例（默认 0.8） |
| `workdir` | 工作区，相对路径按项目根解析，默认 `agent_workspace` |
| `permission_mode` | `accept` / `bypass` / `explore` |
| `dangerous_commands` | 危险命令正则表，命中即弹确认（`bypass` 除外） |
| `memory_dir` / `snapshot_dir` | 记忆与快照目录 |
| `memory_consolidation_minutes` | 后台合并记忆的间隔（默认 30 分钟） |
| `skills_dir` / `rules_dir` / `mcp_config` | 扩展点所在路径 |

改完 `config.json` 里已有的字段，输入 `/reload` 生效；新增字段或改 `alex/` 代码需重启。

### MCP

仓库不附带任何 MCP 服务器配置，需要自己新建 `config/mcp_servers.json`（该文件已被 `.gitignore`
排除，写绝对路径或令牌都不会进提交）：

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "<工作区绝对路径>"]
    }
  }
}
```

- `stdio` 必须有 `command`，可选 `args`、`env`；`http` 必须有 `url`，可选 `headers`。
- 名称用小写英文，会成为工具名前缀 `mcp__<名称>__<工具名>`。
- 保存后输入 `/reload`，设置页 MCP 分类里能看到连接状态；单个服务启动失败只跳过它，不影响启动。

## 它为什么越用越聪明

```
一轮任务 ──► data/Memory/memory/YYYY-MM-DD.md   逐轮流水
                     │
                     ├─ 后台定时合并 ──► MEMORY.md        事实性长期记忆
                     │
                     └─ 上下文压缩触发 ──► 反思 ──► LESSONS.md   可复用经验
                                                    │
下一轮 system prompt ◄──────────────────────────────┘
```

- **流水**：每轮做了什么、改了什么，按日归档，回答「最近做过什么」时直接读它。
- **合并**：后台按 `memory_consolidation_minutes` 把流水压成 `MEMORY.md`，控制体积。
- **反思**：上下文压缩是最容易丢信息的时刻，AlexCode 在这时异步让模型复盘这一轮，
  只保留「下次遇到同类问题该怎么做」的经验，写进 `LESSONS.md`。
- **生效**：`MEMORY.md` 与 `LESSONS.md` 一起进下一轮 system prompt，行为随使用逐步校准。

`/init memory` 可以清空长期记忆重新开始。

## 斜杠命令

| 命令 | 作用 | 命令 | 作用 |
|---|---|---|---|
| `/model` | 选模型与思考强度 | `/queue` | 串行执行多个任务 |
| `/permission` | 切换权限模式 | `/goal` | 目标模式（执行 + 验收循环） |
| `/settings` | 设置页（Tokens/Rules/Skills/MCP/Memory） | `/image` | 发送图片 |
| `/cd` | 切换工作区 | `/compact` | 手动压缩上下文 |
| `/theme` | 切换主题 | `/init memory` | 清空长期记忆 |
| `/reload` | 重载配置/技能/规则/MCP | `/clear` | 清空当前会话 |
| `/help` | 命令速查 | `/exit` | 退出 |

快捷键：`Enter` 发送，`Shift+Enter` 换行，`Ctrl+T` 循环主题，`Ctrl+L` 清屏，
`Ctrl+O` 调试日志，`Ctrl+Q` 退出，`Esc` 中断生成或关闭弹窗。

内置的 `skills/alexcode` 是只读功能手册，可以直接问它「AlexCode 怎么配置 MCP」。

## 目录结构

```
alex/            会话内核、工具、记忆、权限、TUI 组装
termui/          终端组件库（面板、进度、状态栏、spinner）
config/          config.json（本机 MCP 配置自行创建，不入库）
rules/           本机长期规则，默认留空（.gitkeep 占位）
skills/alexcode/ 只读功能手册，仓库内唯一内置技能
start.bat        Windows 一键启动
```

## 权限与安全

- `accept`（默认）：文件改动与危险命令需要确认，其余放行。
- `explore`：只读，任何写操作都被拒绝，适合陌生仓库。
- `bypass`：全放行，**只在你完全信任当前任务时使用**。

确认卡支持「允许一次 / 总是允许 / 拒绝」，命中 `dangerous_commands` 的命令（删除、格式化、
强制推送、注册表改写等）永远不会静默执行。工具调用的文件改动会先给出内联 diff。

发现安全问题请按 [SECURITY.md](SECURITY.md) 私下上报，不要开公开 issue。

## 扩展

- **加技能**：新建 `skills/<name>/SKILL.md`，frontmatter 写 `name` 和 `description`，`/reload` 生效。
- **加规则**：`rules/<topic>.md`，纯 Markdown，越短越好，一条规则一个文件。
- **加 MCP**：编辑 `config/mcp_servers.json`，`/reload` 后设置页可见连接状态。
- **换模型**：改 `config/config.json` 的 `model` 段，密钥仍只写 `.env`。

## 本地开发

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m alex.tui
```

- 改 `alex/` 代码或新增 `config/config.json` 字段需要重启；只改技能、规则、MCP 用 `/reload`。
- 提交信息沿用 `feat:` / `fix:` / `refactor:` / `chore:` 前缀加中文描述，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 测试用例由作者本地维护，不随仓库发布；改动核心逻辑时请在 PR 里说明你如何验证。

## 已知限制

- TUI 以 Windows 终端为主要目标平台，Linux/macOS 走同一套代码但快捷键与粘贴行为可能略有差异。
- 同一台机器只允许跑一个实例（有锁文件），重复启动会提示退出。
- MCP 的 `filesystem` / `playwright` 依赖本机 Node.js。
- 尚未提供 pip 安装包，当前通过源码 + `requirements.txt` 运行。

## 许可证

代码以 [Apache License 2.0](LICENSE) 发布，Copyright 2026 Alex CHA。

仓库内只包含 `skills/alexcode` 这一个自建技能，不打包任何第三方代码；
运行依赖（agentscope / textual / python-dotenv）由 `pip` 安装，各自遵循其上游许可证。
