# Cross-Harness Orchestration

## UpAgent workers

A hired UpAgent worker is terminal. It returns `blocked` when it needs help.
It never spawns another worker.

## Session memories

Dated notes may exist under `.memsearch/memory/`. Read them when prior-session
context would change the work; do not treat them as source of truth over `src/`
and `docs/`.
