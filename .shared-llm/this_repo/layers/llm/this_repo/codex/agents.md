# Cross-Harness Orchestration

## UpAgent workers

A hired UpAgent worker is terminal. It returns `blocked` when it needs help.
It never spawns another worker.

## Skills — shared across harnesses

Skills in this repo are composed by `llm-config-setup` (`just update` from that
kit). Sources: `.shared-llm/public/` (kit) and `.shared-llm/this_repo/` (this
repo). Outputs land at `.claude/skills/<name>/SKILL.md`.

- **Claude Code** reads `.claude/skills/` in the project.
- **Codex** discovers the same files via `~/.codex/skills/` (kit `link` step).
- **Pi** discovers them via `~/.pi/agent/skills/`; Pi-only `do-*` skills live in
  `.pi-skills/` so Claude does not see them.

The Python skill is repo-local: common practices plus
`.shared-llm/this_repo/layers/skills/this_repo/python.md`.

## Hooks and MCP servers

No project MCP servers are wired. Claude hooks/settings, when present, come
from the kit compose/copy into `.claude/` — do not hand-edit generated copies.

## Session memories

Dated notes may exist under `.memsearch/memory/`. Read them when prior-session
context would change the work; do not treat them as source of truth over `src/`
and `docs/`.
