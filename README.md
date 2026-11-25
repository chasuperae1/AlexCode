# AlexCode

> A terminal AI assistant that gets smarter every session.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#quick-start)

AlexCode is an AI coding and office assistant that lives in your terminal: a Textual TUI,
tool calls guarded by inline permission prompts, concurrent sub-agents, session snapshots you
can rewind to, and a memory pipeline that **reflects after every context compaction**. Each
time the context is compressed, it distills what went wrong into reusable lessons and starts
the next turn with them.

Built on [AgentScope](https://github.com/agentscope-ai/agentscope) and
[Textual](https://github.com/Textualize/textual). Default model: DeepSeek.

## Features

| Capability | What it does |
|---|---|
| **Reflection after compaction** | When the context hits the compaction threshold (80% by default), AlexCode asynchronously reviews the turn and writes reusable experience to `LESSONS.md`, which is injected into the next system prompt |
| **Long-term memory** | Every turn is logged per day, then merged in the background into `MEMORY.md`; you can clear, trim, or browse memory by date |
| **Permission modes** | `accept` / `bypass` / `explore`; dangerous commands are matched against a regex table, and file edits are shown as an inline diff before confirmation |
| **Goal mode** | `/goal <goal>` starts an executor + independent verifier loop that keeps fixing until the verifier passes |
| **Task queue** | `/queue taskA; taskB` splits a request into fine-grained tasks (one per verifiable unit) and runs them one after another with progress in the top bar |
| **Sub-agents** | The main agent can dispatch self-contained sub-agents; multiple calls in one reply run concurrently with isolated context |
| **MCP** | Connects to stdio and http servers from `config/mcp_servers.json` concurrently; a failing server is skipped, not fatal |
| **Skills / rules** | `skills/*/SKILL.md` is loaded on demand, `rules/*.md` is injected into the system prompt; both can be toggled in Settings |
| **Snapshots** | A workspace snapshot per turn; click a past message to restore files and conversation to that moment |
| **Five themes** | Crimson / Dark / Light / Mono / Ember, cycle with `Ctrl+T`, choice is persisted |
| **Image input** | `/image <path>` or paste a screenshot from the clipboard |
| **Multiple models** | deepseek / openai / openai-compatible / custom gateway, with adjustable reasoning effort |

## A look at the UI

```
┌────────────────────────────────────────────────────────────────┐
│ deepseek-flash · high     accept      queue 0/0      Crimson   │
├────────────────────────────────────────────────────────────────┤
│ you  make the default workspace path relative, no drive letter │
│                                                                │
│ AlexCode  ├ Read alex/config.py                                │
│           ├ Edit alex/config.py  (+2 −2)                       │
│           └ Cmd py -3 -m compileall -q alex                    │
│           compile OK                                           │
├────────────────────────────────────────────────────────────────┤
│ > /                                                            │
└────────────────────────────────────────────────────────────────┘
```

## Quick start

Python 3.10+ is required (3.11–3.13 recommended). The default model is DeepSeek, so you need
an API key.

```powershell
git clone https://github.com/chasuperae1/AlexCode.git
cd AlexCode

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env     # then put your API_KEY in it
```

On Windows, double-click `start.bat` (it locates the interpreter itself). Or run directly:

```powershell
python -m alex.tui    # TUI
python -m alex.cli    # plain REPL
```

On Linux / macOS use `python -m alex.tui`; swap the interpreter detection in `start.bat`
for your own shell script.

The first launch creates `agent_workspace/` and `data/` for runtime state and never writes
into the source tree.

## Configuration

### `.env`

```ini
API_KEY=sk-xxx          # provider key — never commit this
MODEL_NAME=deepseek-flash
```

Secrets live only in `.env` (already git-ignored). `config/config.json` stores just the
**variable name** in `api_key_env`.

### `config/config.json`

| Field | Purpose |
|---|---|
| `model.provider` / `name` / `base_url` | Active model; `model_profiles` holds presets you can switch to |
| `model.context_size` / `context_trigger_ratio` | Context window and compaction threshold (0.8 by default) |
| `workdir` | Workspace; relative paths resolve against the project root, defaults to `agent_workspace` |
| `permission_mode` | `accept` / `bypass` / `explore` |
| `dangerous_commands` | Regex table for dangerous commands; a match forces a confirmation (except in `bypass`) |
| `memory_dir` / `snapshot_dir` | Where memory and snapshots live |
| `memory_consolidation_minutes` | Background merge interval (30 minutes by default) |
| `skills_dir` / `rules_dir` / `mcp_config` | Extension points |

Editing an existing field takes effect after `/reload`; adding new fields or changing code
under `alex/` requires a restart.

### MCP

The repository ships no MCP server configuration. Create `config/mcp_servers.json` yourself —
that file is git-ignored, so absolute paths and tokens stay out of commits:

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "<absolute workspace path>"]
    }
  }
}
```

- `stdio` requires `command`, optionally `args` and `env`; `http` requires `url`, optionally `headers`.
- Use short lowercase names; they become the tool prefix `mcp__<name>__<tool>`.
- Save, then `/reload`. Connection status shows up under MCP in Settings; a server that fails
  to start is skipped without blocking the app.

## Why it gets smarter

```
one turn ──► data/Memory/memory/YYYY-MM-DD.md    per-turn log
                  │
                  ├─ periodic merge ──► MEMORY.md        factual long-term memory
                  │
                  └─ context compaction ──► reflection ──► LESSONS.md   reusable lessons
                                                            │
next system prompt ◄────────────────────────────────────────┘
```

- **Log** — what was done and changed each turn, archived per day; answering "what did we do
  recently" reads from here.
- **Merge** — the background job compresses the log into `MEMORY.md` every
  `memory_consolidation_minutes` to keep it bounded.
- **Reflect** — compaction is where information is most likely to be lost, so AlexCode asks
  the model to review that turn asynchronously and keeps only "what to do differently next
  time", written to `LESSONS.md`.
- **Act** — `MEMORY.md` and `LESSONS.md` both go into the next system prompt, so behaviour
  is calibrated by actual usage.

`/init memory` clears long-term memory and starts over.

## Slash commands

| Command | Action | Command | Action |
|---|---|---|---|
| `/model` | Pick model and reasoning effort | `/queue` | Run several tasks sequentially |
| `/permission` | Switch permission mode | `/goal` | Goal mode (execute + verify loop) |
| `/settings` | Settings (Tokens/Rules/Skills/MCP/Memory) | `/image` | Send an image |
| `/cd` | Change workspace | `/compact` | Compact context manually |
| `/theme` | Switch theme | `/init memory` | Clear long-term memory |
| `/reload` | Reload config, skills, rules, MCP | `/clear` | Clear the current session |
| `/help` | Command cheat sheet | `/exit` | Quit |

Keys: `Enter` sends, `Shift+Enter` newline, `Ctrl+T` cycles themes, `Ctrl+L` clears,
`Ctrl+O` toggles debug logs, `Ctrl+Q` quits, `Esc` interrupts generation or closes a modal.

`skills/alexcode` is a read-only manual bundled with the project — you can simply ask it
"how do I configure an MCP server".

## Repository layout

```
alex/            session core, tools, memory, permissions, TUI assembly
termui/          terminal widget library (panels, progress, status bar, spinner)
config/          config.json (create your own MCP config; it stays local)
rules/           local long-term rules, empty by default (.gitkeep placeholder)
skills/alexcode/ read-only manual, the only bundled skill
start.bat        one-click launcher for Windows
```

## Permissions and safety

- `accept` (default): file edits and dangerous commands need confirmation, everything else runs.
- `explore`: read-only; every write is rejected. Good for unfamiliar repositories.
- `bypass`: everything runs — **only when you fully trust the task**.

Confirmation cards offer allow once / always allow / deny. Commands matching
`dangerous_commands` (deletes, formatting, force pushes, registry writes, …) never run
silently. File edits made through tools are shown as an inline diff first.

Please report vulnerabilities privately per [SECURITY.md](SECURITY.md) instead of opening
a public issue.

## Extending

- **Skills**: create `skills/<name>/SKILL.md` with `name` and `description` in the
  frontmatter, then `/reload`.
- **Rules**: add `rules/<topic>.md` as plain Markdown; keep it short, one rule per file.
- **MCP**: edit `config/mcp_servers.json` and `/reload`; status appears in Settings.
- **Models**: change the `model` section of `config/config.json`; keys still live only in `.env`.

## Local development

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m alex.tui
```

- Code changes or new `config/config.json` fields require a restart; skills, rules and MCP
  only need `/reload`.
- Commit messages use `feat:` / `fix:` / `refactor:` / `chore:` prefixes, see
  [CONTRIBUTING.md](CONTRIBUTING.md).
- Test suites are maintained locally by the author and are not published. When changing core
  logic, describe how you verified it in the pull request.

## Known limitations

- The TUI targets Windows terminals first; Linux/macOS share the same code, but key bindings
  and clipboard behaviour may differ slightly.
- Only one instance may run per machine (a lock file enforces this); a second launch exits.
- The `filesystem` / `playwright` MCP examples need Node.js installed locally.
- No pip package yet — run from source with `requirements.txt`.

## License

Released under the [Apache License 2.0](LICENSE), Copyright 2026 Alex CHA.

The repository contains exactly one bundled skill, `skills/alexcode`, and no third-party
code. Runtime dependencies (agentscope / textual / python-dotenv) are installed by `pip`
and remain under their own upstream licenses.
