# Executor roles

openci-tf separates read-only plan work from mutation work with distinct IAM roles
and distinct Step Functions lanes.

## Roles and state ownership

| Role | Where it is created | Recipe |
|------|---------------------|--------|
| `openci-tf-executor-readonly` in the hub account | `hub-setup` in deploy state | `just deploy` |
| `openci-tf-executor-readonly` in a remote target account | target readonly state | `just target-create-aws-readonly <hub_account_id>` |
| `openci-tf-executor-poweruser` in the hub account | target poweruser state | `just target-create-aws-poweruser <hub_account_id>` |
| `openci-tf-executor-poweruser` in a remote target account | target poweruser state | `just target-create-aws-poweruser <hub_account_id>` |

`just target-create-aws-readonly` is for remote target accounts only and refuses
same-account hub mode. The hub readonly role is owned by `just deploy` so a single
Terraform state owns it.

`just target-create-aws-poweruser` is an explicit opt-in for accounts that may run
confirmed apply or destroy jobs. It works for both same-account and cross-account
targets.

## Lane binding

- The read lane assumes the registered account row's `role_name`, normally
  `openci-tf-executor-readonly`.
- The apply and destroy lanes assume `poweruser_role_name` only. If that field is
  missing or the role is absent, mutation jobs fail loud before Terraform can run.
- `enable_apply` on the account row gates mutation intent creation, but it does
  not grant credentials by itself. The poweruser role must also exist.
- Repository `require_approval` and folder-level `apply` / `destroy` gates remain
  independent checks before a confirmation token can start a mutation lane.

## State access contract

Executor roles can read and write Terraform backend state only under the
`targets/` prefix of the state bucket. State locking is the S3 native lock file
(`<key>.tflock`, written beside the state object); no DynamoDB lock table
exists. A folder config with `state_bucket`/`state_key` narrows the run session
policy to that exact registered state object instead.

Registered repositories must configure backend keys as:

```text
targets/<repo>/<folder>.tfstate
```

The install control-plane state, source snapshots, and engine artifacts in the
same bucket are denied to executor roles.
