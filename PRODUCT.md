# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

openci-tf is an AWS-backed GitHub PR automation service with an optional web
console. The console uses Vite/React for the browser app and a small Hono Node
server packaged as a Lambda.

## Users

Primary users are platform and DevOps engineers who install and operate
openci-tf across repositories and AWS accounts. They monitor run health,
investigate failures, and manage installation-level configuration. Application
team developers interact with openci-tf mainly through GitHub PR comments, which
are the primary user interface for pull request results.

## Product Purpose

openci-tf provides authenticated `tf plan` and `tf report` commands
over Terraform/OpenTofu folders in GitHub pull requests. Apply and destroy are
blocked by default and require the explicit gated flow documented in
[docs/APPLY.md](docs/APPLY.md).

GitHub PR comments are the primary result view. They summarize folder status,
drift, security findings, and cost, with detailed output kept behind collapsed
sections. Plan, drift, destroy-plan, apply, and destroy terminal comments share
one visual system: a folder summary line, blockquoted Setup/Plan/Security/Cost/
Execution/Artifacts children for read-only work, and collapsed Metadata at the
bottom of non-report comments. The web console is an optional operator view for run history, folder
status, manifests, artifacts, registered repositories/accounts, locks, and gate
visibility.

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
- For `tf report`, any successfully parsed nonzero Terraform/OpenTofu plan delta
  is presented as drift. Zero changes are clean. Missing or invalid evidence
  remains unknown, and execution failure still takes precedence.
- `tf report` comments keep every non-clean folder visible, ordered by the worst
  execution, security, or drift condition. Clean folders are collapsed.
- One expansion of a `tf report` folder comment shows the human-readable plan
  output inline. Low-frequency setup, cost, security detail, and download
  pointers can stay collapsed.
- Managed PR comment identity and replacement semantics remain stable so repeated
  runs update the intended folder and report summary comments.
- PR comment status indicators pair an icon with a text label so meaning does not
  depend on color alone.
- Apply and destroy must not bypass the two-step confirmation flow in
  [docs/APPLY.md](docs/APPLY.md).

## Product Principles

- Keep read-only plan work and mutation work structurally isolated.
- Make failure details actionable without exposing secrets or unbounded output.
- Make drift and security findings scannable in GitHub PR comments before showing
  detailed artifacts.
- Treat retention as real: views should show when artifacts are bounded or
  expired instead of implying permanence.
- Keep the console a thin operator view over the documented core API.
