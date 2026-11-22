# Contributing

Thanks for wanting to improve AlexCode. It is a personal terminal agent, so things can move
quickly, but the conventions below are stable.

## Before you start

- Python 3.10+ (3.11–3.13 recommended).
- Node.js is only needed for the `filesystem` / `playwright` MCP servers; the core runs without it.
- Never commit real credentials. `.env` is ignored, and so is `config/mcp_servers.json`.

```powershell
git clone https://github.com/chasuperae1/AlexCode.git
cd AlexCode
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

## Code conventions

- Comments and module docstrings in this codebase are mostly Chinese. That is the existing
  style; English is fine in new modules, but stay consistent within a file.
- Every module opens with a one-line statement of its responsibility. Prefer splitting
  functions over long expressions.
- Do not run multi-line Python through `python -c` / `py -3 -c`; write a temporary file and
  execute it instead.
- Do not modify `site-packages/agentscope`, and do not copy framework sources into the project.
- Prefer dedicated file tools over shell redirects for reading and writing files.

## Verifying changes

The repository does not ship test suites (they are maintained locally by the author), so every
pull request must describe how it was verified:

```powershell
python -m compileall -q alex termui     # syntax and import-level check
python -m alex.tui                      # manually exercise the affected feature
```

- For bug fixes, give the minimal reproduction: input, expected, actual.
- For changes to sessions, permissions, memory or snapshots, explain whether state migration
  affects existing `data/`.
- For TUI changes, include terminal size and OS (Windows Terminal / ConPTY behave differently).

## Commit messages

Use Conventional Commits prefixes with a short imperative English subject:

```
feat: add resume support for queued tasks
fix: inject LESSONS after context compaction
refactor: split the UI card layer
chore: bump agentscope to 2.0.12
docs: document MCP configuration
```

One commit does one thing. Formatting-only or rename-only noise belongs in its own
`chore:` / `refactor:` commit.

## Pull requests

1. Branch from `main`, named `feat/xxx` or `fix/xxx`.
2. Describe what changed, how you verified it, and any compatibility impact (config fields,
   data directories, command behaviour).
3. Launch the app on Windows at least once and manually check the affected feature before merging.
4. Update `README.md` and `skills/alexcode/SKILL.md` when user-visible behaviour changes.
5. When changing `config/config.json` fields, keep old config files working without those fields.

## Adding skills or rules

This repository ships exactly one bundled skill, `skills/alexcode`. Your own skills and rules
stay local and are excluded by `.gitignore`, so they never end up in a commit:

- `skills/<name>/SKILL.md`: the frontmatter must contain `name` and `description`, and `name`
  must match the directory.
- `rules/<topic>.md`: plain Markdown, no frontmatter, one rule per file.
- Never hardcode machine paths, usernames or secrets inside a skill.
- If a skill really should be published, open an issue first explaining its purpose and
  provenance, then send a separate PR; bundled third-party skills must include their `LICENSE`.

## Do not commit

`__pycache__`, `.pytest_cache`, `.venv`, `data/`, `agent_workspace/`, `design/`, `tests/`,
personal skills and rules, `config/mcp_servers.json`, or any `.env`.
`.gitignore` covers all of these; if something shows up in `git status`, find out why first.

## Code of conduct

By participating you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Report security issues privately, see [SECURITY.md](SECURITY.md).
