# Target accounts and cross-account planning

openci-tf keeps a **hub** account (where Lambdas, Step Functions, API Gateway, and the
settings table live) and one or more **target** accounts where Terraform plans run.
Folder `.openci_tf/config.yaml` selects the target via `account_alias`; the hub mints
short-lived credentials with `sts:AssumeRole` into the registered executor role.

## Architecture (TEST hub + TEST2 target live test)

```text
GitHub PR comment "tf plan all"
        │
        ▼
Hub REPLACE_MAIN_ACCOUNT — outer Step Functions + run-folder SFN
        │  prepare-and-submit Lambda (openci-tf-run-folder-prepare-and-submit)
        │  sts:AssumeRole + external ID
        ├──────────────────────────────┐
        ▼                              ▼
Same-account executor-remote      Cross-account executor-remote
REPLACE_MAIN_ACCOUNT                      REPLACE_SECONDARY_ACCOUNT (TEST2)
state: openci-tf-state-REPLACE_MAIN_ACCOUNT  state: openci-tf-state-REPLACE_SECONDARY_ACCOUNT
ReadOnlyAccess + scoped state      ReadOnlyAccess + scoped state
```

Runtime planning never uses `AdministratorAccess`. Installation may use admin
credentials; executor roles and hub caller roles remain least-privilege.

## Executor naming

Deploy `infra/modules/target-connect` in each target account with the exact hub role
principal and a required external ID.

<!-- # ref 4353245 - openci-tf remote executor consistency naming -->
The remote executor role name is derived from one prefix as
`${role_prefix}-executor-remote`; the checked-in default is `openci-tf-executor-remote`.
Keep `OPENCI_TF_PROJECT` (Terraform) and `OPENCI_TF_ROLE_NAME` (registration override) in
sync when installing under a non-default prefix.

## Why “read-only” still writes state

Executors attach AWS managed `ReadOnlyAccess` for infrastructure APIs, plus inline
**Allow** statements scoped to:

- `s3:GetObject` / `PutObject` / `DeleteObject` on `…/targets/*` in the target state bucket

Explicit **Deny** statements block control-plane state prefixes (`bootstrap/`, `deploy/`,
`source/`, `engine/`, …) and infrastructure mutation outside the state bucket ARN.
Wildcard **Deny** actions are not permissions — audit Effect carefully.

Hub and target-account executors use each account's install lock table for
`targets/*` state locking. Target onboarding requires both the existing S3 state
bucket and ACTIVE `<project>-tf-locks` DynamoDB table. The target-connect
installer's own backend remains S3-only; the lock table is for repository runs.

## Onboarding a new target account (two accounts today)

Current count after the live cross-account test: **hub `REPLACE_MAIN_ACCOUNT`** plus **target
`REPLACE_SECONDARY_ACCOUNT`**, with aliases `REPLACE_MAIN_ALIAS` and `REPLACE_SECONDARY_ALIAS`.

### Recommended: two-command journey

**A. Target account** (example TEST2 `REPLACE_SECONDARY_ACCOUNT`):

```sh
aws sts get-caller-identity   # must show the target account
just target-onboard REPLACE_MAIN_ACCOUNT
# optional custom bucket: just target-onboard REPLACE_MAIN_ACCOUNT my-existing-state-bucket
```

**B. Hub account** (example TEST `REPLACE_MAIN_ACCOUNT`):

```sh
aws sts get-caller-identity   # must show the hub account
just register-target REPLACE_SECONDARY_ALIAS REPLACE_SECONDARY_ACCOUNT
```

`target-onboard` and `register-target` use the same implementation-owned
ExternalId algorithm: `openci-tf-` plus the first 16 lowercase hex chars of SHA-256
over `openci-tf:<hub-account-id>:<target-account-id>`. The executor role name is
`${OPENCI_TF_PROJECT}-executor-remote` (default `openci-tf-executor-remote`). Target
onboarding requires an existing S3 state bucket and ACTIVE
`openci-tf-tf-locks` DynamoDB table in the target account. `just bootstrap` creates
both when needed; `target-onboard` creates neither and fails before SSM/IAM
mutation if either prerequisite is absent.

```text
target acct: hub id + current target id ──derive──> target role trust ExternalId
hub acct:    current hub id + target id ──derive──> alias row + STS AssumeRole
```

### Manual steps (recovery / lower-level recipes)

When the bundled recipes cannot run, use the steps below. Each step needs the
correct account credentials (`aws sts get-caller-identity`) before mutating IAM,
SSM, or state.

#### A. Target account (example TEST2 `REPLACE_SECONDARY_ACCOUNT`)

1. Verify caller: `aws sts get-caller-identity` must show the target account ID.
2. Ensure the target account has an S3 state bucket and lock table (e.g.
   `just bootstrap` in the target creates `openci-tf-state-<account>` and
   `openci-tf-tf-locks`). The table must be ACTIVE; the executor can access only lock
   items whose keys match the repository `targets/*` state prefix.
3. Store cross-account tfvars in target SSM (`/openci-tf/install/openci-tf/`):
   - `hub_lambda_exec_role_arn` — hub `openci-tf-hub-lambda-exec` ARN
   - `target_state_bucket_arn` — this account’s state bucket ARN
4. `just target-connect` — deploys `openci-tf-executor-remote` only (not hub Lambdas/engine)
   and derives the ExternalId in Terraform.

#### B. Hub account (example TEST `REPLACE_MAIN_ACCOUNT`)

1. `just register-account` — alias, account id, role `openci-tf-executor-remote`, optional
   `max_ttl` (e.g. 3600). It derives and stores the alias row ExternalId from the
   authenticated hub account plus the target account.
2. `just config set target_account_ids '["REPLACE_MAIN_ACCOUNT","REPLACE_SECONDARY_ACCOUNT"]'` — **append**; do
   not remove the hub id.
3. `just deploy` (or `terraform apply` on `infra/deploy` if image push is blocked) — updates
   hub `sts:AssumeRole` allow-list on `openci-tf-hub-lambda-exec` / prepare-and-submit.

Raw Terraform is fallback when recipes cannot run; tfvars come from SSM via `just config`.

## How `account_alias` selects the account

Each reportable folder has `.openci_tf/config.yaml` with `account_alias: <name>`. At run time
`prepare-and-submit` loads the DynamoDB row (`pk=account`, `sk=<alias>`) for `account_id`,
`role_name`, derived `external_id`, and `max_ttl`, recomputes the ExternalId from the
current hub account plus `account_id`, fails loud on mismatch, then assumes
`arn:aws:iam::<account_id>:role/<role_name>`.

## How `tf plan all` spans accounts

`validate-and-resolve` discovers every folder with `.openci_tf/config.yaml`. Folders may
reference different aliases; each run-folder execution assumes the matching target role and
uses that account’s state bucket (from the backend block in the repo). The outer state
machine fans out per folder; the **summary comment is always last**.

Adding another account later: repeat target bootstrap + target-connect, register a new alias,
append the 12-digit id to `target_account_ids`, redeploy hub IAM, add repo folders with the
new alias — no change to CI verbs.

## Region interpretation

Informal “US-East-11” requests are treated as **`us-east-1`** (`us-east-11` is not a valid
AWS region). Document this when onboarding operators.

## Teardown and recovery (ordering)

**Target VPC/samples (operator, not openci-tf):** destroy workload before VPC; TEST2 before hub
if both are being removed.

**openci-tf target plane (TEST2):** `target-connect-destroy` → optional `bootstrap-destroy`
(empties target state bucket if policy allows).

**Hub registration:** delete account row from `openci-tf-settings`; remove account id from
`target_account_ids` and redeploy hub IAM.

**Never** destroy foreign-owned buckets; verify `ManagedBy=openci-tf-bootstrap` tags before
bootstrap destroy.

## Live test evidence (2026-08-09)

- PR `<REPO_ORG>/<REPO_NAME>` #5 at `2f772fca6dfab7c92c2444e77ce9efc08118c32d`;
  the complete tested repository snapshot is retained in
  `tests/fixtures/live-smoke/sample-target-repo/`, with per-file provenance in
  `tests/fixtures/live-smoke/sample-target-repo.snapshot.json`
- Trigger: `tf plan all` — six folder reports + summary; all drift ; TEST2 plans show
  `data.aws_caller_identity.current: … id=REPLACE_SECONDARY_ACCOUNT` and no-change plans
- Outer SFN: `arn:aws:states:us-east-1:REPLACE_MAIN_ACCOUNT:execution:openci-tf:02b682ef-dba6-491e-9a02-4923f668364a` — SUCCEEDED
- IAM simulation: `openci-tf-run-folder-prepare-and-submit` → AssumeRole to TEST2 allowed;
  TEST2 executor `ec2:RunInstances` explicitDeny; `targets/*` PutObject allowed;
  `bootstrap/*` PutObject explicitDeny

## Phase B mutation-lane live evidence (2026-08-14)

- PR `<REPO_ORG>/<REPO_NAME>` #9 at
  `6679ba3f28aea5f68d264ce68cd755facad3af88`; hub `REPLACE_MAIN_ACCOUNT`, TEST2
  `REPLACE_SECONDARY_ACCOUNT`, region `us-east-1` only.
- Deployed code `79f4d67` at image digest
  `sha256:3d44132e702704085d35cff6ddb3045d1a59da74b6a3f52c7ab69f3dc4a02eeb`.
- VPC no-change plan `7e9c393820944a2d9d52aa6c3a22b58e`; apply outer
  `77ca3872d4b6496fa83e466f4a38880b`, inner
  `cf73d9ae-2a0e-4a9c-a161-bfacb2ff3398`, CodeBuild
  `fa0e2967-571a-4174-8064-c95c54c64f36` — all succeeded.
- EC2 create plan `3a307479891d475aa68e75f182473a0a` contained only the account guard,
  dedicated egress-only security group, and `t3.nano` probe. Apply outer
  `98416262aa354edc863b92f42c3afe6f`, inner
  `8b035207-2c86-4b29-ba15-39c17842f555`, CodeBuild
  `d0be747a-fc95-4b89-aaf7-f42537204c67` — all succeeded. The live instance had
  no public IP, required IMDSv2, and used an encrypted `gp3` root volume.
- Pinned destroy plan `b2eab991bf50439f9dad15e71ebe367e` was exactly `0 add / 0 change /
  3 destroy`. Destroy outer `a77852a9a53146bd94b00bfc16b14fd0`, inner
  `8d28a686-330c-497d-8969-b4513c9663bf`, CodeBuild
  `67ef4af5-5b99-4553-9e90-96924cdbc6a5` — all succeeded.
- Runtime waits measured `15.092s` for apply and `60.099s` for destroy. The final
  empty-state destroy verification used outer `b917991abe2c458c8ab3748a273d59a5`,
  inner `b16677af-9126-4581-9783-e82e09913f1c`, and CodeBuild
  `e66dca03-8dc3-4677-a30a-27b4f0beaac2`; all succeeded.
- Mutation engine executions used `openci-tf-codebuild` and exactly the eight fields
  `trigger_id`, `s3_package_uri`, `sops_type`, `sops_path`, `commands_b64`,
  `done_endpoint`, `execution_target`, and `timeout_seconds`.
- Final state: EC2 probe and dedicated security group absent; EC2 state empty;
  restored subnet `subnet-0aa56a24e33235ac3` available; VPC, hub, TEST2 readonly,
  and TEST2 poweruser Terraform plans clean; lock table empty.

Raw Terraform is fallback when recipes cannot run; tfvars come from SSM via `just config`.

## Simplification note

`just target-onboard` and `just register-target` wrap the manual steps with
account-verified guards, deterministic ExternalId derivation, existing-bucket
verification, and hub IAM redeploy. Use the manual path only for recovery.
