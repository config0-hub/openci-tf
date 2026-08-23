# openci-tf Console — Design Brief

The openci-tf console is an operator dashboard for the documented core API. It
shows run status, artifacts, registered repositories/accounts, locks, and gates
without weakening the GitHub PR command model.

Product truth lives in [PRODUCT.md](../PRODUCT.md), API contracts in
[API.md](API.md), and mutation gates in [APPLY.md](APPLY.md).

## Audience

Platform and DevOps engineers operating openci-tf across repositories and AWS
accounts. They arrive mid-task to check a run, diagnose a failure, inspect
artifact state, or confirm registration and gate settings.

## Outcomes

The operator can, without leaving the console:

- See current and recent run status across authorized repositories/accounts,
  bounded by the run-registry retention window.
- Open one run and inspect folder executions, step timeline, artifacts,
  manifests, and plan expiry.
- See registered repositories, accounts/targets, active locks, and read-only
  apply/destroy gate state.
- Trigger safe read-only verbs (`plan`, `drift`, `report`) through the existing
  `POST /runs` API.

Hard boundary: the console does not initiate apply or destroy. Gate state is
displayed, not operated.

## Visual direction — Quick Reference Handbook

Infrastructure runs are presented as operational procedures: checklist lines,
section rules, boxed gates, dotted leaders, and stamped verdicts.

- Palette: paper `#f4f2ec`, ink `#141210`, caution amber `#e8a013`, warning red
  `#c22f21`, completed green `#2e7d43`.
- Type: one condensed grotesk family with weight and caps for hierarchy.
- State is readable without color: checks, crosses, stamps, and flags carry
  meaning; color reinforces it.
- Motion is procedural: leaders draw, verdicts stamp in, and in-progress states
  use a caret or leader animation rather than spinners.

## Screens

- **Runs index** — filter by trigger ID and repository; stable cursor pagination;
  empty state when no retained runs match.
- **Run detail** — timeline of folder executions and artifacts; explicit expired
  plan state; no binary plan download through the console.
- **New run** — trigger ID, pinned SHA, verb (`plan`, `drift`, `report`), folder
  mode, and idempotency key.
- **Pipelines** — placeholder page until pipeline data is available.
- **Repos** — registered repository settings.
- **Accounts/Targets** — account alias rows and role names.
- **Locks** — active locks with TTL.
- **Gates** — account and folder mutation-gate state as read-only boxed items.

## Stack and hosting

The console is a Vite + React SPA served by a Hono Node app packaged in one
Lambda behind a Function URL.

```text
Browser
   │ HTTPS
   ▼
Lambda Function URL
   │
   ▼
Hono app
   ├── /        → static Vite/React build
   └── /api/*   → SigV4 proxy to API Gateway using the Lambda execution role
```

- Static shell and assets are publicly reachable at the Function URL.
- Every `/api/*` request requires the shared console bearer token.
- The Lambda reads the token from the configured SSM SecureString at cold start.
- Browser credentials never include AWS credentials; the proxy signs API calls.
- Frontend uses React, TypeScript, TanStack Router, TanStack Query, and plain CSS.
- No component library, auth framework, CSS framework, or additional state library.

## Implementation constraints

- The Lambda execution role must be listed in `api_caller_policy_json` with the
  trigger IDs, actions, artifact classes, binary-plan permission, and admin read
  class it needs.
- Render retention and expiry from API data; do not imply artifacts are permanent.
- Do not invent drift, cost, security, or step outcomes from missing artifacts.
- Keep the console a thin view/proxy over documented API behavior.
