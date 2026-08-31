# Install openci-tf

`just` recipes are the canonical operator interface. There are two install
modes:

- **standalone** (default, `just install`) — openci-tf provisions its own
  state bucket and execution engine in the hub account. The standard journey is
  `just install` → `just verify` → `just uninstall` → `just verify-clean`.
- **config0-addon** (`just install --mode config0-addon`) — openci-tf is
  installed into a tenant account that already runs the AWS execution engine
  and owns a Terraform state bucket. The install reuses both, copies the
  released GHCR Lambda image into tenant ECR, and registers the GitOps
  repository and webhook. See
  [Install as a config0 add-on](#install-as-a-config0-add-on).

Both modes lock Terraform state with the S3 native lock file
(`use_lockfile=true` at init, tofu/terraform >= 1.10); no DynamoDB lock table
exists in either mode.

## Prerequisites

- AWS credentials in the environment (e.g. `source /tmp/test.env`); admin in the
  hub account. Only AWS credentials are needed at runtime — no `TF_VAR_` exports.
- `terraform`, `just`, `jq`, `docker`, and the `aws` CLI.
- Node 20+, npm, and `zip` when deploying the optional console component.
- The engine repository checked out next door (default `../aws-execution-engine`,
  override with `ENGINE_REPO_PATH`).
- Region defaults to `us-east-1`; override with `AWS_REGION`.
- Behind a TLS-intercepting proxy, export `EXTRA_CA_CERT=docker/certs/extra-ca.crt`
  (gitignored) containing the proxy CA for the Docker image builds.

## Configuration

All install-time configuration lives in SSM Parameter Store (SecureString)
under `/openci-tf/install/<project>/…` (`openci-tf` for this repo, `engine` for the
engine repo). Manage it with:

```sh
just config set target_account_ids '["<hub-account-id>"]'   # REQUIRED before deploy
just config get target_account_ids
```

The openci-tf Lambda image tag is the fixed release version in the checked-in
`IMAGE_VERSION` file. Installation does not derive it from Git or read it from SSM.
ECR keeps this tag mutable so rebuilding the release replaces it; Terraform resolves
that known tag to its current immutable digest so Lambda reliably receives the update.
Optional key: `run_history_retention_days` (defaults to `90`; controls run-registry TTL).
Optional key: `run_folder_max_concurrency` (defaults to `40`; caps read-lane folder Map fan-out in the outer state machine; lower it when the hub account Lambda concurrency quota is tight).
Intent gating uses the per-account `enable_apply` flag on each DynamoDB account registration;
provision the optional `openci-tf-executor-poweruser` role separately when mutations are desired.
See [APPLY.md](APPLY.md).
Optional key: `api_caller_policy_json` (JSON map of IAM role ARNs to explicit caller
policies; required when API routes should be usable — each entry must include
`trigger_ids`, `actions`, `artifact_classes`, and `binary_plan`). The optional
`read_classes` list defaults to no admin access; set it to `["admin"]` for the
four read-only console reference routes (`/repos`, `/accounts`, `/locks`, and
`/gates`). Grant callers `execute-api:Invoke` only on the core API stage routes
they need, as documented in [API.md](API.md).
The console additionally requires a `console_token` SecureString. Set it only
through stdin with `just config set-stdin console_token`; the secret is passed to
the AWS CLI through a mode-0600 input file, never its argument vector. Its
deterministic path is `/openci-tf/install/<project>/console_token`, where `<project>`
follows `OPENCI_TF_PROJECT`.
Target-role ExternalIds are implementation-owned and derived automatically from
hub and target account IDs; do not store or supply them as operator config.

Core API routes (AWS IAM) are documented in [API.md](API.md), including the
90-day run-registry TTL behavior.

Each component recipe follows the same pattern: read SSM → write
`terraform.tfvars` into the root (gitignored) → `terraform init/apply` → upload
a complete copy of the applied Terraform source to
`s3://openci-tf-state-<account-id>/source/<root>/` (single authoritative copy,
overwritten each time, SSE-S3; bucket versioning provides history) plus a
`manifest.json` with root, timestamp, terraform version, and variable NAMES
only — never values.

## Install

```sh
just config set target_account_ids '["<hub-account-id>"]'
just install     # bootstrap -> foundation -> engine -> deploy (hub readonly via hub-setup)
just verify      # aws CLI checks: buckets, KMS, lambdas, SFNs, roles, SSM, source copies
```

`just install` and `just uninstall` print per-phase elapsed times to stderr as each
component runs (for example `>> bootstrap start` and `<< terraform-apply done in 2m 15s`).
The same timing lines appear when you run individual component recipes such as
`just bootstrap` or `just deploy`. Read them from the terminal or capture stderr in
install logs (for example `/tmp/openci-round2/install/phase1-install.log`).

`just install` provisions `openci-tf-executor-readonly` in the hub account via `just deploy`
(hub-setup module). Opt in to mutation IAM separately with `just target-create-aws-poweruser`
in each account that should allow apply/destroy. Deploy creates three outer state machines
(`openci-tf`, `openci-tf-apply`, `openci-tf-destroy`) and three inner run-folder machines
(`openci-tf-run-folder`, `openci-tf-run-folder-apply`, `openci-tf-run-folder-destroy`).
`just account-set-apply` writes to hub DynamoDB (`openci-tf-settings`) and must run
with hub credentials — not target credentials. Use it (or
`just account-set-apply <alias> true` later) so apply/destroy intents are
allowed for each registered account alias.

What each component does:

1. **bootstrap** — state bucket `openci-tf-state-<account-id>`. State locking
   is the S3 native lock file (`use_lockfile=true` at init; tofu/terraform
   >= 1.10); no DynamoDB lock table exists. Chicken-and-egg: the first apply uses LOCAL state
   (the backend bucket does not exist yet), then the recipe generates
   `backend.tf` and migrates the state into the bucket it just created. Every
   other root starts on the S3 backend directly.
2. **foundation** — KMS key (`alias/openci-tf-foundation`) and the
   `openci-tf-{tmp,package,done}-<account-id>` buckets. These names are a hard
   cross-stack contract (deploy and the engine discover them by name via data
   sources) and are deliberately NOT overridable.
3. **engine** — runs `just install` in the engine repo: adopts the shared state
   bucket (or creates its own when standalone), builds `engine.zip` +
   `sops-age-layer.zip`, uploads them to `s3://…/engine/artifacts/`, applies
   `infra/01-ecr`, mirrors the published GHCR engine image into tenant ECR, and
   applies `infra/02-deploy` with `project_prefix=openci-tf` and
   `engine_image_uri` set to that mirrored tag.
4. **deploy** — the hub stack including same-account `openci-tf-executor-readonly`
   (hub-setup module). Cross-stack values are discovered with data-source lookups
   on deterministic names (foundation KMS alias and buckets, engine `openci-tf-init-job`
   Lambda); only true config remains in tfvars. The recipe applies
   `module.ecr` first, builds and pushes the Lambda container image at the fixed
   version in `IMAGE_VERSION` (`just docker-push`), then applies the rest.
5. **target-create-aws-readonly** — creates `openci-tf-executor-readonly` in a
   remote target account and records the trust relationship back to the hub.
   Run `just target-onboard` or `just target-create-aws-readonly <hub_account_id>`
   from target credentials. Terraform derives the required `sts:ExternalId` as
   `openci-tf-` plus the first 16 lowercase hex chars of SHA-256 over
   `openci-tf:<hub-account-id>:<target-account-id>`. Target onboarding requires
   only an existing S3 bucket for the role stack's own backend (pass
   `--state-bucket` to use a shared bucket); openci-tf creates no per-target
   state bucket and no lock table exists.
6. **target-create-aws-poweruser** — optional mutation IAM for accounts that can
   run confirmed apply/destroy jobs. The role is separate from the readonly role
   and is assumed only by the apply/destroy lanes.
7. **console** (optional) — one Node 20 Lambda and a Function URL. It discovers
   the existing `<project>-webhook` HTTP API by name, serves the built SPA, and
   SigV4-proxies `/api/*`. The Function URL uses `NONE` authorization so a
   browser can load the static login shell and assets; the app checks the shared
   bearer token on every `/api/*` request.

## Install as a config0 add-on

`just install --mode config0-addon` installs openci-tf into a tenant account
that already runs the AWS execution engine and owns a Terraform state bucket.
It does not run bootstrap or the engine component; state for every infra root
lives in the tenant bucket and the deploy reuses the tenant engine by name
(`install_mode = "config0-addon"` on the deploy root). Requires `tofu` >= 1.10
on PATH (the installers fail loud below that).

The Lambda image is not built locally. A GitHub release publishes it to GHCR at
the checked-in `IMAGE_VERSION` tag and records the pushed digest in the release
notes (`.github/workflows/release.yml`); the install copies that digest-pinned
image into the tenant ECR repository.

Required SSM install config before running (same `just config set` namespace as
standalone):

```sh
just config set state_bucket_name <tenant-state-bucket>
just config set engine_name <tenant-engine-prefix>
just config set ghcr_image ghcr.io/<owner>/openci-tf@sha256:<digest>
just config set gitops_repo <owner/repo>
just config set trigger_id <trigger-id>
just config set upstream_urls_json '{...}'          # pinned runtime download URLs
just config set api_caller_role_arn <role-arn>      # optional: tenant executor role for POST /runs
just install --mode config0-addon
```

The journey composes four phases:

1. **ecr** (`install/config0_addon.py --stage ecr`) — targeted `module.ecr`
   apply on `infra/deploy` with the same backend and tfvars as the full apply,
   so the repository exists before the image copy.
2. **image copy** (`scripts/copy_ghcr_image.sh`) — pulls the digest-pinned
   GHCR image and pushes it to tenant ECR at the `IMAGE_VERSION` tag.
3. **deploy** (`install/config0_addon.py --stage deploy`) — applies
   `infra/foundation`, waits for the copied image tag to exist in ECR, then
   applies `infra/deploy` fully. When `api_caller_role_arn` is set, the stage
   writes an `api_caller_policy_json` entry for that role with actions
   `plan|drift|report` only.
4. **registration** (`install/register_repo.py`) — generates or reuses the
   webhook HMAC secret in SSM, writes the repository settings row, creates or
   reconciles the GitHub webhook (the hook id is recorded at
   `/openci-tf/install/<project>/webhook_hook_id` for clean removal), and
   proves comment access with a probe on a throwaway branch and PR. Re-runs
   converge. See [docs/GITHUB_WEBHOOK.md](GITHUB_WEBHOOK.md).

In config0-addon mode the hub Lambda exec role trusts
`arn:aws:iam::*:role/<project>-executor-*` by name pattern instead of an
enumerated account list; each target role's own trust policy remains the gate
(see [docs/ACCOUNTS.md](ACCOUNTS.md)).

After apply, the deploy root exports the values an embedding platform records:
`project_name`, `ecr_repository_url`, `settings_table_name`,
`run_registry_table_name`, `api_url`, and `webhook_url`
(`tofu -chdir=infra/deploy output`).

## Deploy the console

The console is separate from `just install` so existing headless installations
do not acquire a required shared token or a public URL. Deploy the core stack
first. Then configure the token, authorize the deterministic console execution
role in the core API's caller policy, redeploy the core stack, and deploy the
console:

```sh
# Secret input is read from stdin and stored as a SecureString.
just config set-stdin console_token

# Merge this role entry with any existing caller entries; do not overwrite them.
# Replace the account and trigger IDs with real installation values.
just config set api_caller_policy_json '{"arn:aws:iam::<hub-account-id>:role/openci-tf-console":{"trigger_ids":["<registered-trigger-id>"],"actions":["plan","drift","report"],"artifact_classes":["manifest","text","json"],"binary_plan":false,"read_classes":["admin"]}}'
just deploy
just console
```

For a non-default `OPENCI_TF_PROJECT`, the role name is `<project>-console` and the
SSM path uses that same project namespace. `binary_plan` should remain `false`
unless console operators are explicitly allowed to receive presigned binary
plan URLs. The `console` recipe reads SSM config, writes gitignored tfvars,
runs `npm ci` plus the deterministic Lambda packaging step, initializes and
applies `infra/console`, and uploads the Terraform source copy (including the
pinned provider lock). It prints the Function URL when complete. Terraform puts
only the token parameter name in the Lambda environment, so decrypted token
material is not persisted in Terraform state. At cold start the Lambda fetches
and decrypts that exact parameter, then caches the value for the execution
environment. The execution role can write its own CloudWatch logs, call
`ssm:GetParameter` on that one parameter, and invoke the existing `$default`
`/runs` routes plus the four read-only admin reference routes. The installer
uses the default AWS-managed SSM key, which needs no customer-key KMS grant. The
role must be present in `api_caller_policy_json` with `read_classes:["admin"]` or
the core API rejects the signed admin requests even though IAM allows them.

Because the console is outside the default install journey, destroy it before
running `just uninstall` or deleting its token:

```sh
just console-destroy
just uninstall       # when removing the rest of the installation too
```

Open the printed Function URL in a browser. The public static shell prompts for
the shared token and keeps it in `sessionStorage`; subsequent `/api/*` requests
carry it as a bearer token. Static files never receive or require the token.

## Updating an existing install

Code or infrastructure updates reuse the same recipes in the same relative
order as `just install`; only the components that changed need to be applied,
but when both change, foundation MUST go before deploy (deploy reads
foundation's buckets/KMS via data sources):

```sh
just foundation # bucket lifecycle/KMS changes (e.g. openci-tf/ retention)
just deploy     # build/push IMAGE_VERSION, then deploy IAM, state machines, and lambdas
just verify     # post-update checks
```

The engine (`just engine`) and target role recipes only need re-applying when
their own inputs change (engine release zip, target account list or IAM).
Changing per-account intent permission uses `just account-set-apply` and does not
require redeploy.

## Enable apply and destroy

Apply and destroy require account-level, folder-level, and (when configured)
repository-level opt-in:

```sh
# Hub account: readonly role is installed by just install; add poweruser when needed.
just target-create-aws-poweruser <hub-account-id>

# Allow intents for this account alias (registration or later update).
just register-account --alias hub --account-id <hub-account-id> --enable-apply true
# or: just account-set-apply hub true
```

For each cross-account target, split work by credential scope:

1. **Target account** — target credentials (executor IAM / Terraform only):

   ```sh
   just target-onboard <hub-account-id>
   # or just target-create-aws-readonly <hub-account-id> when install SSM is already populated
   just target-create-aws-poweruser <hub-account-id>   # optional mutation IAM
   ```

2. **Hub account** — hub credentials (DynamoDB registration and intent flags):

   ```sh
   just register-target prod <target-account-id>
   # or: just register-account --alias prod --account-id <target-account-id> --enable-apply true
   # or later: just account-set-apply prod true
   ```

Then opt in each eligible folder in `.openci_tf/config.yaml`:

```yaml
apply:
  allow: true

destroy:
  allow: true
```

Set either flag independently when a folder should support only one mutation.
To require an approved PR review before an intent is created, register the
repository with `just register-repo ... --require-approval` (per-repo DynamoDB flag).

Mutation is always a two-step, single-use-token flow against a fresh plan at
the unchanged PR head SHA. Run `tf plan <folder-or-csv>` before apply or
`tf plan --destroy <folder-or-csv>` before destroy, then follow the posted
`tf apply confirm <token>` or `tf destroy confirm <token>` command. See
[APPLY.md](APPLY.md) for all gates,
commands, token lifetime, and artifact paths.

## Run artifact layout (tmp bucket)

All run artifacts live under one prefix in `openci-tf-tmp-<account-id>`, keyed by
the outer run id shown in the PR status comment:

```
openci-tf/<repo_name>/<run_id>/<folder_path>/
  tf/plan.tfplan          # binary plan consumed by a confirmed apply
  tf/destroy.plan.tfplan  # binary destroy plan consumed by a confirmed destroy
  tf/plan.tfplan.sha256   # integrity sidecar
  tf/plan-metadata.json   # integrity sidecar
  tf/plan.out             # human-readable plan output
  init.out
  validate.out
  drift.json              # drift verb only
  tfsec.json
  tfsec.output
  infracost.json
  infracost.output
  manifest.json           # names, checksums, sizes, action, run_id
```

Each run writes artifacts only under its immutable run-scoped prefix above. The
API and UI resolve plans through PR-scoped pointers, exact run-scoped keys, or
newest-first run-registry queries — not a mutable alias prefix. Each manifest
records checksums and `expires_at`; the API rejects expired artifacts. The
`openci-tf/` prefix is deleted asynchronously by S3 lifecycle after
`plan_retention_days` (default **1 day**); stale plans disappear — re-run the
plan. Repo and folder names are used literally; the path builder rejects
traversal segments.

## Uninstall

```sh
just uninstall      # exact reverse: deploy -> engine -> foundation -> bootstrap (removes hub readonly via deploy-destroy)
just verify-clean   # asserts no openci-tf footprint remains
```

`uninstall` prompts whether to keep the state bucket + source copies as the
surviving record (set `OPENCI_TF_KEEP_STATE=yes|no` for non-interactive runs).
When not kept, the bootstrap destroy first migrates its own state back to
local, empties the bucket (all versions), then destroys the bucket. SSM install parameters are deleted in both namespaces.

After Terraform teardown, both `just uninstall` and `just bootstrap-destroy` run
`scripts/cleanup_operator_footprint.sh`, which removes operator-managed resources
that survive `terraform destroy`:

- CloudWatch log groups for product Lambdas, CodeBuild, and Step Functions
  (the full list is in `scripts/product_log_groups.sh`)
- SSM parameters outside `/openci-tf/install/` under `/openci-tf/clone-token`,
  `/openci-tf/env`, `/openci-tf/infracost`, and `/openci-tf/webhook`
- IAM roles `${OPENCI_TF_PROJECT}-executor-local` and
  `${OPENCI_TF_PROJECT}-executor-remote` when present

`just verify-clean` fails if any of those log groups, operator SSM parameters,
or executor-local/executor-remote roles remain.

`just deploy-destroy` (used during `uninstall`) calls
`scripts/terraform_unlock_stale_lock.sh` before `terraform destroy`. If an S3
native lock file exists beside the deploy state object, the script stops with
exit code 1, prints the lock holder and age, and shows the exact
`terraform -chdir=infra/deploy force-unlock <lock-id>` command. It never
unlocks automatically. Confirm no deploy is running, run that command, then
retry `just uninstall` or `just deploy-destroy`.

Every component also has an individual `<name>-destroy` recipe.

## After install

Store the webhook secret, the repository-scoped GitHub control PAT, and any
private-module token as separate SSM/KMS SecureStrings, then run
`just register-repo`, `just register-account`, and `just create-webhook`;
register only private repositories (see [docs/GITHUB_WEBHOOK.md](GITHUB_WEBHOOK.md#private-repositories-only));
never pass secret values on the command line. The control PAT must be a
fine-grained PAT scoped to **Only selected repositories** for the registered
repo, with Metadata read, Contents read, Pull requests read, and Issues
read/write; see [docs/GITHUB_TOKEN.md](GITHUB_TOKEN.md). `just register-repo`
runs a read-only GitHub capability verifier before it writes the registration
row and fails loud on missing access. Initialize the repository/default branch
before registration so the Contents check can pass; pass
`--github-capability-collaborator` if the token owner is not a direct
collaborator.

### Same-account target

`just deploy` (hub-setup in deploy state) provisions `openci-tf-executor-readonly` in the
hub account. `just target-create-aws-readonly` **refuses same-account mode** — do
not use it on the hub. Add `just target-create-aws-poweruser` when mutation IAM
is required.

### Cross-account target (two-command journey)

1. **Target account** — with credentials for the target account:

   ```sh
   just target-onboard <12-digit-hub-account-id> [state-bucket-name]
   ```

   Verifies the caller is the target account, confirms the backend state bucket
   (default `openci-tf-state-<target-account-id>`; pass a name to use an
   existing shared bucket), stores the hub role ARN and target bucket ARN
   in install SSM, then runs `just target-create-aws-readonly`. Terraform derives the
   target-role ExternalId; onboarding creates no bucket. No lock table exists;
   state locking is the S3 native lock file.

2. **Hub account** — with credentials for the hub account:

   ```sh
   just register-target <alias> <12-digit-target-account-id>
   ```

   Resolves the hub account from the authenticated AWS identity, stores the same
   derived ExternalId in the DynamoDB alias row, appends the account id to
   `target_account_ids` (without duplicates), and runs `just deploy` to refresh
   hub IAM. Runtime recomputes the value and fails loud if the stored row does
   not match.

Lower-level recipes (`just register-account`, `just config set target_account_ids …`,
`just target-create-aws-readonly`, `just target-create-aws-poweruser`) remain
available for manual or recovery flows.

**Executor roles:** See [EXECUTOR_ROLES.md](EXECUTOR_ROLES.md) for role ownership,
lane binding, and state access rules.

**Executor state contract:** PR-plan execution roles can only read/write Terraform
state under the `targets/` prefix of the state bucket, plus the S3 native lock
file (`<key>.tflock`) beside each state object. Registered repositories keep
their committed backend to bucket/key/region only (any terraform/tofu version
can init it); openci-tf's own runs pass `-backend-config=use_lockfile=true` at
init and pin tofu/terraform >= 1.10, so platform runs always lock. A human on
an older version runs unlocked (warned, never blocked). By default backend
state keys are `targets/<repo>/<folder>.tfstate`; a folder config may instead
pin `state_bucket`/`state_key` to an exact registered state object (see the
allowed state pairs below).
The install control-plane state, the `source/` record, and `engine/` artifacts
in the same bucket are explicitly denied to executors.

Open a same-repository PR and comment `tf plan <folder-or-csv>` or `tf report`
for the first safe run. With the account and folder gates enabled, use `tf apply`
or create a destroy plan with `tf plan --destroy <folder-or-csv>`; both mutation
paths require the confirmation command posted by openci-tf. See [APPLY.md](APPLY.md).
(`just uninstall` destroying the installation itself remains operator tooling,
not a CI verb.)

For cross-account operation, repeat the target onboarding and registration flow
for each target account; see [docs/ACCOUNTS.md](ACCOUNTS.md).
