# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

openci-tf is an AWS-backed GitHub PR automation service with an optional web
console. The console uses Vite/React for the browser app and a small Hono Node
server packaged as a Lambda.

## Users

Primary users are platform and DevOps engineers — the people who install and
operate openci-tf across repositories and AWS accounts. They monitor run health,
investigate failures, and manage installation-level configuration. Application
team developers interact with openci-tf mainly through GitHub PR comments.

## Product Purpose

openci-tf provides authenticated `tf plan`, `tf drift`, and `tf report` commands
over Terraform/OpenTofu folders in GitHub pull requests. Apply and destroy are
blocked by default and require the explicit gated flow documented in
[docs/APPLY.md](docs/APPLY.md).

The web console is an operator surface for the same core API: run history,
folder status, manifests, artifacts, registered repositories/accounts, locks,
and gate visibility.

## Positioning

The service is designed as a safe path for infrastructure review in pull
requests. It keeps the high-risk mutation path separate from read-only plan work,
pins every run to the PR head SHA, records bounded artifacts, and exposes enough
operator state to troubleshoot installations without reaching directly into AWS
resources first.

## Operating Context

- Backing API: AWS IAM-authenticated HTTP routes on API Gateway
  ([docs/API.md](docs/API.md)).
- Run artifacts live in S3 under `openci-tf-tmp-<account-id>` with bounded
  retention; run registry rows use a 90-day default DynamoDB TTL.
- Core concepts: runs, folder executions, manifests, artifacts, locks,
  registered repos/accounts/targets, and apply/destroy gates.
- The supported PR verbs are `plan`, `drift`, and `report`.

## Capabilities and Constraints

- Internal operator access through AWS IAM and, for the console, a shared console
  bearer token in front of SigV4-proxied API calls.
- Run observability across authorized trigger partitions with server-side filters
  and cursor pagination.
- Operator visibility for registered repositories, accounts, locks, and mutation
  gates.
- Apply and destroy must not bypass the two-step confirmation flow in
  [docs/APPLY.md](docs/APPLY.md).

## Product Principles

- Keep read-only plan work and mutation work structurally isolated.
- Make failure details actionable without exposing secrets or unbounded output.
- Treat retention as real: views should show when artifacts are bounded or
  expired instead of implying permanence.
- Keep the console a thin operator view over the documented core API.
