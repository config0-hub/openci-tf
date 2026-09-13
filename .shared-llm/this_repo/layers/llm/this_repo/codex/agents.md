# Cross-Harness Orchestration

## UpAgent workers

The "Subagents" rule further down this page, with its Sonnet, Opus, and
GPT-5.5 exceptions, applies to the session the human runs. It does not apply
to a hired UpAgent worker. A hired UpAgent worker is terminal. It returns
`blocked` when it needs help. It never spawns another worker or subagent.

## Session memories

Dated notes may exist under `.memsearch/memory/`. Read them when prior-session
context would change the work; do not treat them as source of truth over `src/`
and `docs/`.
