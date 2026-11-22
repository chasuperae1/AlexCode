# 贡献指南

感谢你愿意改进 AlexCode。这个项目是一个偏个人向的终端 Agent，改动可能很快，
但下面的约定是稳定的。

## 开始之前

- Python 3.10+（推荐 3.11–3.13）。
- 需要 Node.js 才能用示例里的 `filesystem` / `playwright` MCP，纯跑核心功能可以不需要。
- 不要提交真实密钥。`.env` 已被忽略，`config/mcp_servers.json` 也不进仓库。

```powershell
git clone https://github.com/chasuperae1/AlexCode.git
cd AlexCode
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## 代码约定

- 中文注释与中文 docstring 是这个项目既有的风格，保持即可；标识符一律英文。
- 每个模块顶层写一句职责说明，复杂逻辑优先拆函数，不要堆长表达式。
- 不在 `python -c` / `py -3 -c` 里跑多行脚本；临时脚本写成文件再执行。
- 不修改 `site-packages/agentscope` 的源码，也不把框架源码复制进项目。
- 文件操作优先专用工具，不要用 shell 命令替代读写。

## 验证改动

仓库不包含测试用例（由作者本地维护），所以每个 PR 需要在描述里写清验证方式：

```powershell
python -m compileall -q alex termui     # 语法与导入层面自检
python -m alex.tui                      # 手动过一遍受影响的功能
```

- 修 bug 请在 PR 里给出最小复现步骤：输入了什么、期望什么、实际什么。
- 涉及会话、权限、记忆、快照的改动，请说明状态迁移是否会影响已有 `data/` 数据。
- 涉及 TUI 的改动，请附终端尺寸与操作系统（Windows Terminal / ConPTY 差异较大）。

## 提交信息

沿用 Conventional Commits 前缀 + 中文描述，一行说清做了什么：

```
feat: 支持 /queue 任务中断后续跑
fix: 修复压缩后 LESSONS 未注入的问题
refactor: 拆分 UI 卡片层
chore: 升级 agentscope 到 2.0.12
docs: 补充 MCP 配置示例
```

一次提交做一件事。格式化、重命名的噪声改动单独放一个 `chore:` / `refactor:`。

## Pull Request

1. 从 `main` 拉分支，命名 `feat/xxx`、`fix/xxx`。
2. PR 描述里写：解决了什么、怎么验证的、有没有兼容性影响（配置字段、数据目录、命令行为）。
3. 合并前需要在 Windows 上至少跑通一次启动，并手动验证受影响的功能。
4. 改动用户可见行为时，同步更新 `README.md` 与 `skills/alexcode/SKILL.md`。
5. 改动 `config/config.json` 字段时，保证老配置文件缺字段仍能启动。

## 新增技能或规则

本仓库只保留 `skills/alexcode` 一个内置技能。你自己的技能与规则写在本地即可，已被
`.gitignore` 排除，不会进入提交：

- `skills/<name>/SKILL.md`：frontmatter 必须有 `name` 与 `description`，`name` 与目录名一致。
- `rules/<topic>.md`：纯 Markdown、无 frontmatter、一条规则一个文件。
- 技能里不要写死本机路径、用户名或密钥。
- 确实需要把某个技能开源时，先在 issue 里说明用途与来源，再单独提 PR；第三方技能必须附带
  它的 `LICENSE`。

## 不要提交

`__pycache__`、`.pytest_cache`、`.venv`、`data/`、`agent_workspace/`、`design/`、
`tests/`、个人技能与规则、`config/mcp_servers.json`、任何 `.env`。
`.gitignore` 已覆盖这些，`git status` 里出现它们请先查原因。

## 行为准则

参与本项目即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
安全问题请走私密渠道，见 [SECURITY.md](SECURITY.md)。
