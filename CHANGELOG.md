# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 Semantic Versioning。

## [0.1.0] - 2026-11-23

首个公开版本。此前为个人本地开发，本版本补齐开源所需的结构与文档。

### Added

- 终端界面（Textual）：启动页、顶栏状态、内联 diff 卡、五套主题、粘贴截图。
- 会话内核：流式回复、上下文压缩、手动 `/compact`、`/clear`。
- 记忆体系：按日流水 → 后台合并 `MEMORY.md` → 压缩后反思沉淀 `LESSONS.md`。
- 权限体系：`accept` / `bypass` / `explore` 三档，危险命令正则拦截，确认卡支持「允许一次 / 总是允许 / 拒绝」。
- 目标模式 `/goal`：执行 + 独立验收循环。
- 任务队列 `/queue`：串行执行多个独立任务，中断后不重叠。
- 子代理派发：同一回复内并发，上下文互不共享。
- MCP 支持：stdio 与 http，并发连接，单服务失败隔离。
- 快照与回溯：按轮生成工作区快照，可从历史消息恢复文件与对话。
- 技能与规则：`skills/*/SKILL.md` 按需加载，`rules/*.md` 进入系统提示，设置页可开关。
- 多模型：deepseek / openai / openai-compatible / 自建网关，思考强度分档。
- 内置技能 `skills/alexcode`：只读功能手册，回答自身的命令、配置与扩展方式。

### Changed

- `config/config.json` 的 `workdir` 默认改为相对路径 `agent_workspace`，仓库不再包含任何本机绝对路径。
- MCP 配置、个人规则与个人技能只保留在本地：仓库不附带 `config/mcp_servers.json`，`rules/` 留空占位。
- 仓库只保留 `skills/alexcode` 一个内置技能，`skills/` 下其余目录不入库。
- `.gitignore` 重写：纳入自身，继续排除 `.env`、`data/`、`agent_workspace/`、`design/`、`tests/` 等本地内容。

### Notes

- 尚未提供 pip 安装包，按源码方式运行。
- 以 Windows 终端为主要目标平台。
- 测试用例由作者本地维护，不随仓库发布。
