# Apply and destroy

openci-tf supports token-based two-step `tf apply` and `tf destroy` when the target account has `enable_apply` set, folders declare `apply.allow: true` or `destroy.allow: true`, and other gates pass.

## Per-folder mutation config

Per-folder `apply` and `destroy` blocks use independent `allow` gates (default false) and optional `grace_seconds` (apply default 15, destroy default 60, max 3600). Boolean shorthand (`apply: true`) is not supported; use a mapping with `allow: true`.

```yaml
apply:
  allow: true
  grace_seconds: 15   # optional; default 15; max 3600

destroy:
  allow: true
  grace_seconds: 60   # optional; default 60; max 3600
```

`apply.allow` and `destroy.allow` are enforced independently from the account-level `enable_apply` DynamoDB gate.

## Grace period and pinned plan show

After confirmation, the mutation outer Step Functions waits `grace_seconds` (Step Functions Wait) before starting each folder's inner/CodeBuild execution. Stopping the outer execution during that wait aborts before CodeBuild starts.

CodeBuild runs `tofu show` against the exact pinned plan artifact downloaded from the source plan run, then `tofu apply` with `-no-color` on that file. A failed show stops the mutation.

## Submission acknowledgement and progress comments

Engine acceptance is authoritative. The inner folder service writes create-only run-registry acceptance fields containing the trigger/execution identity before attempting the CodeBuild progress comment. Later notification updates are conditionally bound to that accepted identity and cannot change its `status`. The returned envelope keeps `submission_status: accepted`; a GitHub/API failure is recorded separately as `notification_status: failed`, `notification_failed: true`, and a compact redacted `notification_error`. Notification failure never turns accepted mutation work into a preparation failure.

## Commands

| Command | Step | Behavior |
|---------|------|----------|
| `tf plan --destroy <folders>` | single | Runs `terraform plan -destroy`, writes `tf/destroy.plan.tfplan` under the run prefix |
| `tf apply <folders>` | 1 | Creates an apply intent and posts a confirmation token |
| `tf apply confirm <token>` | 2 | Executes the pinned plan for each folder sequentially |
| `tf destroy <folders>` | 1 | Creates a destroy intent against the newest successful `plan_destroy` run |
| `tf destroy confirm <token>` | 2 | Applies the pinned destroy plan sequentially |

## Ask-if tree (step 1)

Every failure posts a PR comment and ends the run in `IntentFailed`:

1. Account `enable_apply` flag on the folder's `account_alias` registration (DynamoDB; default false)
2. Folder `.openci_tf/config.yaml` declares `apply.allow: true` or `destroy.allow: true` (default false)
3. `require_approval` repo flag: PR must have an approved review when enabled at repository registration
4. Newest successful `plan` / `plan_destroy` run exists for this PR and folder
5. PR head SHA still matches the plan run `commit_hash`
6. On success: persist a single-use token (600s TTL) and post `tf <action> confirm <token>`

Set `enable_apply` when registering an account (`just register-account ... --enable-apply true`) or later with `just account-set-apply <alias> true`. Provision `openci-tf-executor-poweruser` separately with `just target-create-aws-poweruser` in the **target** account; confirmed apply/destroy runs on the dedicated mutation outer state machines (`openci-tf-apply`, `openci-tf-destroy`) and inner machines (`openci-tf-run-folder-apply`, `openci-tf-run-folder-destroy`), assuming only the registered poweruser role.

## State machines and roles

| Lane | Outer Step Function | Inner Step Function | Assumed target role |
|------|--------------------|--------------------|---------------------|
| Read (`plan`, `drift`, `report`, `plan_destroy`) | `openci-tf` | `openci-tf-run-folder` | `openci-tf-executor-readonly` (registered `role_name`) |
| Apply confirm | `openci-tf-apply` | `openci-tf-run-folder-apply` | `openci-tf-executor-poweruser` (`poweruser_role_name`) |
| Destroy confirm | `openci-tf-destroy` | `openci-tf-run-folder-destroy` | `openci-tf-executor-poweruser` (`poweruser_role_name`) |

Intent creation (`tf apply <folders>` / `tf destroy <folders>`) still enters the read outer machine only. Confirmation (`tf apply confirm <token>`) atomically revalidates token, head SHA, and pinned plan, then starts exactly one mutation outer machine.

## Token semantics

- 6–8 hex characters, crypto-random
- Stored in the run registry table under `intent#<token>`
- Single-use via conditional DynamoDB update
- Confirm requires write access and matching PR head SHA

## S3 layout

Apply consumes only `openci-tf/<repo>/<source_run_id>/<folder>/tf/plan.tfplan`.

Destroy consumes only `openci-tf/<repo>/<source_run_id>/<folder>/tf/destroy.plan.tfplan`.

Apply/destroy runs write `apply.out` / `destroy.out` and `manifest.json` under their own execution ID prefix. Manifests record `source_plan_run_id`.

## Execution ID

User-facing comments and API aliases use **Execution ID** for the outer run identifier in S3 paths and PR comments.
