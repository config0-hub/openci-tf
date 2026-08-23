# openci-tf

openci-tf accepts GitHub PR comments only from collaborators with write/admin access,
pins the PR head SHA, and refuses forks. Executable verbs are `plan`, `plan_destroy`,
`drift`, and `report`; `apply` and `destroy` use a two-step intent/confirm flow gated
by `enable_apply`, per-folder config, optional PR approval, and fresh plan runs.
Pipelines run ordered folder sets from one PR comment; see `docs/PIPELINES.md`.

The outer state machine validates and resolves a safe command, locks folders, and
renders PR results. The inner run-folder state machine mints short-lived target
credentials, packages an encrypted payload, invokes the engine, polls its terminal
result, and returns bounded summaries plus S3 pointers.

The unmodified execution engine receives exactly eight fields: `trigger_id`,
`s3_package_uri`, `sops_type`, `sops_path`, `commands_b64`, `done_endpoint`,
`execution_target`, and `timeout_seconds` (derived from the run's `deadline_at`). Credentials exist only in encrypted `secrets.enc.json`.

Use `just --list` for operator recipes. Foundation is deployed before the engine,
which is deployed before openci-tf. Future real-AWS installation and two-account smoke
tests remain human-gated; see docs/INSTALL.md and completed live evidence in
docs/ACCOUNTS.md.

Agents must not push, create/update PRs, or otherwise mutate remote Git/GitHub state
unless the human explicitly confirms repository ownership and authorizes that specific
remote action. Default work is local commits on dev.

Do not commit organization names, repository names, account aliases, or other
environment-specific identifiers into this repository. Use placeholders such as
`<REPO_ORG>/<REPO_NAME>`, `REPLACE_MAIN_ACCOUNT`, and `REPLACE_MAIN_ALIAS`.
Live or customer-specific fixtures belong under `/tmp` (or another local scratch
path) and are never checked in. Tracked fixtures must use generic names like
`sample-target-repo`, not real repository slugs.
