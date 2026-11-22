# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue. Report privately through either channel:

- Email: `chasuperae1@gmail.com`, with `[SECURITY] AlexCode` in the subject.
- GitHub Security Advisories (repository Security tab → Report a vulnerability) — preferred.

Include the affected version or commit, reproduction steps or a proof of concept, the impact
(can it read files, execute commands, does it need user interaction), and how you would like
to be credited.

## Response timeline

| Stage | Target |
|---|---|
| Acknowledgement | within 3 business days |
| Initial assessment | within 7 business days |
| Fix or mitigation | within 30 days; complex issues get progress updates |

Once a fix is released it is credited in `CHANGELOG.md` and the GitHub Security Advisory,
unless you ask to stay anonymous.

## Scope

In scope:

- Permission bypass: writes or dangerous commands that should be blocked in `accept` / `explore`
  mode but execute anyway.
- Trivial evasions of the dangerous-command regex (equivalent spellings of `rm`, pipe and
  redirect escapes).
- Secret leakage: `.env` contents appearing in logs, snapshots, memory files or error messages.
- Path escape: tools reading or writing outside the workspace and explicitly authorized directories.
- Prompt injection leading the agent to run unauthorized actions or exfiltrate local files.
- Command injection through MCP configuration parsing.
- Snapshot / rewind bugs that overwrite user files.

Out of scope for this project (still welcome to discuss):

- A user deliberately approving dangerous actions in `bypass` mode.
- Incorrect model output — content risk is governed by the model provider and its policies.
- Vulnerabilities in third-party dependencies (agentscope / textual / python-dotenv); please
  also report upstream.
- Local environment issues such as weak passwords or leaking your screen.

## Notes for users

- `bypass` skips every confirmation; use it only when you fully trust the task.
- Keep `.env` and `config/mcp_servers.json` out of commits, chats and screenshots.
- Run in `explore` mode for a first pass in an unfamiliar repository.
- Memory and snapshots live under `data/` and contain your conversations; clean them before
  sharing the project directory.
