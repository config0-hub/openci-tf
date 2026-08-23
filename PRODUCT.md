# Product

Formerly iac-ci; renamed to openci-tf for the new repository.

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack
Undecided. No dashboard code exists yet; framework/deploy target to be chosen when the surface is shaped/built.

## Users

Primary users are platform/DevOps engineers — the people who install and operate openci-tf across repositories and AWS accounts. They monitor run health, investigate failures, and manage installation-level configuration. App-team developers were considered but are not the primary audience; the dashboard is an internal operator tool first.

## Product Purpose

openci-tf itself is a safe-path GitHub PR automation service: authenticated `tf plan|drift|report` commands over Terraform/OpenTofu folders, with apply and destroy permanently disabled at the resolver level (see CLAUDE.md, docs/APPLY.md). The planned web dashboard is a companion surface, not a replacement for PR comments — it exists because GitHub PR comments are a poor place to see run history across repos/accounts, inspect artifacts, or check operator/installation state.

## Positioning

The dashboard's reason to exist is observability + administration over data the existing IAM-authenticated core API already exposes (`/runs`, `/runs/{id}`, `/runs/{id}/folders`, manifests, artifacts — see docs/API.md), plus operator-facing config/registration state (registered repos/accounts, locks, apply/destroy gate status per docs/INSTALL.md, docs/APPLY.md) that today only exists via `just` recipes and SSM. No other tool in this stack currently surfaces both at once.

## Operating Context

- Backing API: existing AWS IAM-authenticated HTTP routes on API Gateway (docs/API.md). The dashboard is expected to call these routes rather than a new API.
- Run artifacts live in S3 under `openci-tf-tmp-<account-id>` with a 1-day default retention (plans) and a 90-day run-registry TTL (DynamoDB) — the dashboard's historical views are bounded by these retentions, not infinite history.
- Underlying domain concepts: runs, folder executions, manifests, artifacts (plan output, drift.json, tfsec.json, infracost.json), locks, registered repos/accounts/targets, and the apply/destroy gate (`enable_apply`, per-folder `apply`/`destroy` opt-in in `.openci_tf/config.yaml`).
- openci-tf's own executable verbs remain `plan`, `drift`, and `report` only; apply/destroy are permanently disabled in the CI comment flow (CLAUDE.md). Whether the dashboard may trigger safe-verb runs (via the existing `POST /runs`) or is strictly view-only was not resolved during init — treat as undecided.

## Capabilities and Constraints

- Confirmed: internal-only access, authenticated via AWS IAM (matching the existing core API), not a separately-login-gated public tool.
- Confirmed: covers both run observability (history, folders, manifests, artifacts, drift/plan status across repos and accounts) and operator/admin visibility (config, registrations, locks, apply/destroy gate status).
- Undecided: whether the dashboard can trigger runs (plan/drift/report) or is strictly read-only. Do not assume either until confirmed.
- Undecided: whether app-team developers get any access/view, even a restricted one, in a later iteration.
- Constraint inherited from openci-tf: apply and destroy can never be initiated through this dashboard as a bypass of the two-step confirmation flow described in docs/APPLY.md — any future action surface must not weaken that gate.

## Evidence on Hand

No dashboard UI, mockups, or visual assets exist yet. Reference material for future work: docs/API.md (route contracts), docs/INSTALL.md (operator config/recipes), docs/APPLY.md (mutation gates), docs/ACCOUNTS.md (live cross-account evidence).

## Product Principles

- Observability and admin surfaces reflect the same safe-path guarantees as the CI flow itself — the dashboard must never appear to offer more control than openci-tf actually grants (no apply/destroy shortcuts).
- Built for operators first: prioritize scanability and fast troubleshooting (run status, failures, artifacts) over marketing polish.
- Treat the existing core API as the contract; the dashboard should not require new backend surface area beyond what's already documented unless explicitly decided later.
- Respect retention as real: historical views should make bounded retention (1-day plan artifacts, 90-day run registry) legible rather than implying permanence.
