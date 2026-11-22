# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
Semantic Versioning.

## [0.1.0] - 2026-11-23

First public release. Development had been local and private until now; this version adds the
structure and documentation needed to open source it.

### Added

- Terminal UI (Textual): startup screen, status bar, inline diff cards, five themes, clipboard screenshots.
- Session core: streaming replies, context compaction, manual `/compact`, `/clear`.
- Memory pipeline: per-day turn log → background merge into `MEMORY.md` → post-compaction reflection into `LESSONS.md`.
- Permissions: `accept` / `bypass` / `explore` modes, dangerous-command regex guard, confirmation cards with allow once / always allow / deny.
- Goal mode `/goal`: executor plus independent verifier loop.
- Task queue `/queue`: sequential independent tasks that do not overlap after an interruption.
- Sub-agent dispatch: concurrent within one reply, isolated context.
- MCP support: stdio and http, concurrent connections, per-server failure isolation.
- Snapshots and rewind: per-turn workspace snapshot, restore files and conversation from a past message.
- Skills and rules: `skills/*/SKILL.md` loaded on demand, `rules/*.md` injected into the system prompt, toggleable in Settings.
- Multiple models: deepseek / openai / openai-compatible / custom gateway, with reasoning effort levels.
- Built-in `skills/alexcode`: read-only manual covering the app's commands, configuration and extension points.

### Changed

- `workdir` in `config/config.json` now defaults to the relative path `agent_workspace`; the repository no longer contains any machine-specific absolute paths.
- MCP configuration, personal rules and personal skills stay local only: no `config/mcp_servers.json` is shipped and `rules/` is an empty placeholder.
- The repository keeps only `skills/alexcode`; other directories under `skills/` are not tracked.
- `.gitignore` rewritten: it tracks itself and keeps excluding `.env`, `data/`, `agent_workspace/`, `design/`, `tests/` and other local state.

### Notes

- No pip package yet; run from source.
- Windows terminals are the primary target platform.
- Test suites are maintained locally by the author and are not published.
