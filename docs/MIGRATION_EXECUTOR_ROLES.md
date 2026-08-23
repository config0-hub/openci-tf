# Executor role migration (pre-split → readonly/poweruser)

## State ownership

| Role | State owner | Recipe |
|------|-------------|--------|
| `openci-tf-executor-readonly` (hub / same-account) | `hub-setup` in **deploy** state | `just deploy` |
| `openci-tf-executor-readonly` (remote target) | `target-connect` state | `just target-create-aws-readonly <hub_account_id>` |
| `openci-tf-executor-poweruser` (remote target, opt-in) | `target-connect-poweruser` state | `just target-create-aws-poweruser <hub_account_id>` |
| `openci-tf-executor-poweruser` (hub / same-account, opt-in) | `target-connect-poweruser` state | `just target-create-aws-poweruser <hub_account_id>` (same account id) |

`just target-create-aws-readonly` **refuses same-account mode**
(hub account id equals caller account id). Hub readonly is never double-owned.
`just target-create-aws-poweruser` accepts same-account mode for hub optional mutation lanes.

Legacy roles (`openci-tf-executor-local`, `openci-tf-executor-remote`) are gated by
**install SSM provisioning flags** separate from DynamoDB runtime `enable_apply`:

| Legacy role | SSM key (install namespace) | Default | Terraform variable |
|-------------|----------------------------|---------|-------------------|
| Hub `executor-local` | `provision_legacy_executor_local` | `true` | `provision_legacy_executor_local` in deploy |
| Target `executor-remote` | `provision_legacy_executor_remote` | `true` | `provision_legacy_executor_remote` in target-connect |

**Legacy IAM migration only:** install SSM `enable_apply` (default `false`) preserves
pre-split `PowerUserAccess` vs `ReadOnlyAccess` attachment on retained legacy roles
during `just deploy` and `just target-create-aws-readonly`. It does **not** gate runtime
apply/destroy intent — DynamoDB `enable_apply` on each account alias remains the runtime
gate (see [APPLY.md](APPLY.md)).

When the flag is `true`, normal `just deploy` or `just target-create-aws-readonly`
retains legacy roles for safe upgrades. When explicitly set to `false` and applied,
subsequent normal recipes read `false` from SSM and **do not recreate** retired legacy
roles. Terraform `moved` blocks map pre-split addresses to count-indexed resources.

`just uninstall` runs `deploy-destroy` and **does remove** hub legacy roles
(`executor-local`) when still provisioned — that is the normal teardown path. Do not
claim uninstall preserves legacy hub roles. Role-specific recipes (`just
target-delete-aws-readonly`) use targeted destroy so remote legacy `executor-remote`
survives when co-managed in `target-connect` state.

## Safe migration sequence

1. **Provision the new readonly role** without touching legacy:
   - Hub (same account): `just deploy` — creates `openci-tf-executor-readonly` alongside legacy roles.
   - Remote target: `just target-create-aws-readonly <hub_account_id> [state_bucket]` from target credentials.

2. **Update DynamoDB account alias rows** to the explicit readonly role name:
   ```sh
   just register-account --alias <alias> --account-id <12-digit-id> --role-name openci-tf-executor-readonly
   ```
   Legacy `role_name=openci-tf-executor-remote` registrations continue to work until you update them.

3. **Verify** the safe lane: `just verify` (present) and run a PR `tf plan` against a test folder.

4. **Optional poweruser** (mutation lane):
   - Remote target: `just target-create-aws-poweruser <hub_account_id>` in the target account.
   - Hub (same account): `just target-create-aws-poweruser <hub_account_id>` with hub account id.
   Set `enable_apply` and `poweruser_role_name` on the alias when ready.

5. **Durable legacy retirement** after steps 1–3 succeed:
   - Hub: `just retire-legacy-executor-local` (hub credentials). Persists
     `provision_legacy_executor_local=false` in install SSM **first**, then runs deploy apply
     to delete `openci-tf-executor-local`. Fails loud if apply fails.
   - Remote target: `just retire-legacy-executor-remote <hub_account_id>` from target
     credentials. Persists `provision_legacy_executor_remote=false` in the **target-account**
     install SSM namespace, then applies target-connect to delete `openci-tf-executor-remote`.
   - To recreate legacy after retirement: `just restore-legacy-executor-local` or
     `just restore-legacy-executor-remote <hub_account_id>` (explicit opt-in).
   - Do **not** rely on manual `terraform state rm` alone — without the SSM flag, the next
     normal deploy or target-create would plan to recreate legacy roles.

## Pre-split state with `target-connect` managing `executor-remote`

Accounts that applied the old `target-connect` module (managing `openci-tf-executor-remote`) have state at
`target-connect/terraform.tfstate`. Upgrading:

1. Run `just target-create-aws-readonly <hub_account_id>` — provisions **new** `openci-tf-executor-readonly`
   (no `moved` from `executor-remote`; `moved` blocks index legacy resources at `[0]`).
2. Complete steps 2–5 above. Use `just retire-legacy-executor-remote` for durable retirement instead of
   manual state rm when possible.
