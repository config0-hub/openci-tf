# openci-tf

Safe-path CI for Terraform/OpenTofu in GitHub pull requests. This repo is the
product: a Python service (Lambda + Step Functions) that runs authenticated
`tf plan`, `tf drift`, and `tf report` against registered repositories and AWS
accounts. Read-only work uses the readonly execution lane. Apply and destroy
use the separate gated flow in `docs/APPLY.md`.

Start with `docs/INSTALL.md` for deployment and `docs/API.md` for the core API.
Further maps: `docs/PIPELINES.md`, `docs/ACCOUNTS.md`, `docs/EXECUTOR_ROLES.md`,
`docs/GITHUB_WEBHOOK.md`. Docs are a map into the code, not a substitute for it —
confirm behaviour in `src/` when a wrong guess would matter.

## What this system is

- **CI for IaC**, especially Terraform/OpenTofu folders in customer/target
  repos. It is not a general GitHub Actions replacement and not an interactive
  `terraform apply` workstation.
- Target repos declare folders with `.openci_tf/config.yaml`. Optional pipelines
  live at `.openci_tf/pipelines/<name>.yaml` and only order folders.
- Hub account owns orchestration, registries, and artifacts. Target accounts
  assume `openci-tf-executor-readonly` (read lane) or `openci-tf-executor-poweruser` (mutation lane).

## Layout

- `src/core/` — shared primitives (logging, small types)
- `src/domain/` — rules with no AWS I/O (command grammar, gates, formatters)
- `src/platform/` — one place per external system (AWS, GitHub, git)
- `src/services/` — Lambda/entry orchestration (webhook, API, resolve, run-folder, render, intent)
- `infra/` — this product's own Terraform (hub, roles, console)
- `tests/unit/` — pytest; some cases exercise the real Terraform CLI in Docker
- `frontend/` — optional console
- `justfile` — operator and test entrypoint

## How to run work

`just` is the operator interface (`just --list`). Do not invent parallel install
or apply paths.

```bash
just test          # Docker pytest (Python 3.14 image; copies engine payload.py)
just install       # bootstrap → foundation → engine → deploy
just verify
```

Tests and image builds run in Docker. Do not run bare `pytest`/`python` as the
CI stand-in. Do not `terraform apply` this product's `infra/` by hand — use the
`just` recipes.

## Test AWS accounts

Local live checks and install/onboard dry-runs against AWS use these two
target accounts. Source the matching `exports.env`; never paste or commit
token values. Confirm `aws sts get-caller-identity` shows the expected
`account_id` before running `just` recipes that talk to AWS.

| account_id | exports.env |
|---|---|
| `998038917735` | `~/project/repos/hamburger/exclude_folder/aws/gear+leases-1@thytruth.com/exports.env` |
| `338510628751` | `~/project/repos/hamburger/exclude_folder/aws/gear+leases-2@thytruth.com/exports.env` |

Do not invent other account IDs. Do not treat these as hub-account credentials
unless `get-caller-identity` says they are; they are the **test targets**.

## GitHub control token

Local GitHub API / registration work for this repo uses:

`~/project/repos/hamburger/exclude_folder/github/williamwu/openci-tf.env`

Source that file; never paste or commit the token. Scope and SSM install
shape are in `docs/GITHUB_TOKEN.md`.

The GitHub admin token used to create and inspect the current test repositories
is stored at:

`~/project/repos/hamburger/exclude_folder/github/williamwu/admin.env`

Use this only for explicit human-approved GitHub mutations such as creating or
checking test repositories. Do not print the token.

## External test repositories

Live PR tests use three GitHub repos owned by `williaumwu`:
`openci-test-public-modules` (public modules), `openci-test-private-modules`
(private module, DynamoDB), and `openci-test-gitops` (21 Terraform roots,
PR-driven runs). Layout, module sources, backend account patching, and the
private-module SSM dotenv are in the `openci-install` skill. Do not paste
them here.

## Invariants

- Fail closed: unknown repos, bad signatures, forks, and unauthorized commenters
  are rejects, not best-effort continues.
- Pin the PR head SHA. Do not follow a moving branch tip.
- Do not widen apply/destroy: those stay behind `docs/APPLY.md` gates
  (`enable_apply`, per-folder `apply.allow` / `destroy.allow`, confirm tokens).
- Do not commit token values, extra account aliases, or live customer
  fixtures. Tracked files may name the two test accounts above and credential
  *paths* under `~/project/repos/hamburger/exclude_folder/`, never values.
  Use placeholders (`sample-target-repo`, `REPLACE_MAIN_ACCOUNT`) in tracked
  examples. Live evidence stays under `/tmp` or another local scratch path.
- Do not push, open, or update PRs unless the human explicitly owns the remote
  and authorizes that action.
- Keep apply, destroy, and any remote git mutation in the session the human is
  talking to. Do not hand those to a subagent or a hired worker.
