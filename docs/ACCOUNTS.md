# Target accounts and cross-account planning

openci-tf uses a **hub** account for the service control plane and one or more
**target** accounts where repository Terraform/OpenTofu work runs. A folder's
`.openci_tf/config.yaml` selects a target with `account_alias`; the hub then
assumes the registered target role with `sts:AssumeRole` and an implementation-owned
ExternalId.

## Architecture

```text
GitHub PR comment / API run
        │
        ▼
Hub account
  API Gateway · Lambdas · Step Functions · settings table
        │
        │ sts:AssumeRole + derived ExternalId
        ├──────────────────────────────┐
        ▼                              ▼
Same-account target              Remote target account
readonly / poweruser roles       readonly / poweruser roles
state: openci-tf-state-<acct>    state: openci-tf-state-<acct>
```

Runtime planning does not use `AdministratorAccess`. Installation may require
administrator credentials, but executor roles and hub caller roles remain scoped
to the work they perform.

## Executor roles

Read-only PR work assumes the account row's `role_name`, normally
`openci-tf-executor-readonly`. Confirmed apply/destroy work assumes
`poweruser_role_name`, normally `openci-tf-executor-poweruser`, and only after the
account, repository, folder, token, and pinned-plan gates pass.

See [EXECUTOR_ROLES.md](EXECUTOR_ROLES.md) for role ownership and state-access
rules.

## Why read-only executors write state

Read-only executors attach AWS managed `ReadOnlyAccess` for infrastructure APIs,
plus scoped permissions for Terraform backend state:

- S3 object access under `targets/*` in the target state bucket.
- DynamoDB lock access for lock IDs matching `<bucket>/targets/*`.

Explicit deny rules block install/control-plane prefixes such as `bootstrap/`,
`deploy/`, `source/`, and `engine/`. Registered repositories should use backend
state keys under:

```text
targets/<repo>/<folder>.tfstate
```

## Onboard a target account

### Recommended journey

1. **Target account credentials**

   ```sh
   aws sts get-caller-identity   # must show the target account
   just target-onboard <hub-account-id>
   # optional custom bucket: just target-onboard <hub-account-id> <state-bucket-name>
   ```

   `target-onboard` checks the target state bucket and lock table, stores the hub
   trust inputs in target-account SSM, and creates the readonly executor role.

2. **Hub account credentials**

   ```sh
   aws sts get-caller-identity   # must show the hub account
   just register-target <alias> <target-account-id>
   ```

   `register-target` writes the account alias row, appends the target account ID
   to `target_account_ids`, and redeploys hub IAM so the hub may assume the target
   role.

The ExternalId is derived in both accounts as `openci-tf-` plus the first 16
lowercase hex characters of SHA-256 over:

```text
openci-tf:<hub-account-id>:<target-account-id>
```

Runtime recomputes that value before assuming the target role and fails loud if
configuration does not match.

### Manual recovery path

Use lower-level recipes only when the bundled journey cannot run.

**Target account**

1. Confirm caller identity with `aws sts get-caller-identity`.
2. Ensure `openci-tf-state-<target-account-id>` and `openci-tf-tf-locks` exist.
   `just bootstrap` creates both when needed.
3. Store target-account install SSM values:
   - `hub_lambda_exec_role_arn`
   - `target_state_bucket_arn`
4. Run `just target-create-aws-readonly <hub-account-id>`.
5. Optionally run `just target-create-aws-poweruser <hub-account-id>` for
   accounts that may execute confirmed apply/destroy jobs.

**Hub account**

1. Register or update the account row with `just register-account` or
   `just register-target`.
2. Ensure `target_account_ids` includes the target account ID.
3. Run `just deploy` to refresh the hub assume-role allow list.

## How `account_alias` selects the account

Each reportable folder has `.openci_tf/config.yaml` with `account_alias: <name>`.
At run time, `prepare-and-submit` loads the DynamoDB account row
(`pk=account`, `sk=<alias>`) for `account_id`, `role_name`, optional
`poweruser_role_name`, derived `external_id`, and `max_ttl`. It recomputes the
ExternalId from the current hub account and target account, then assumes:

```text
arn:aws:iam::<account_id>:role/<role_name>
```

For mutation lanes it uses `<poweruser_role_name>` instead.

## How `tf report` spans accounts

`validate-and-resolve` discovers every folder with `.openci_tf/config.yaml` when
`tf report` runs (or when API callers use `folder_mode: all`). Folders may
reference different aliases; each run-folder execution assumes the matching target
role and uses that folder's backend state configuration. The outer state machine
fans out per folder and posts the summary comment last.

Adding another account does not change the PR command surface: onboard the target,
register an alias, update hub IAM, and point folders at the new `account_alias`.

## Teardown and recovery

- Delete or disable account alias rows before removing target roles.
- Remove the account ID from `target_account_ids` and redeploy hub IAM when the
  target should no longer be reachable.
- Destroy target roles from the target account with the matching
  `target-delete-aws-readonly` / `target-delete-aws-poweruser` recipes.
- Never destroy a bucket unless it is owned by the current install and intended
  for removal.
