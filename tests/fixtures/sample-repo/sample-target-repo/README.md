# openci-tf Terraform sample repository

Disposable Terraform sample for validating openci-tf folder discovery and
plan-only workflows across six reportable Terraform roots: four same-account
roots and two remote-target VPC roots. Resources are intentionally minimal and
tagged for teardown.

## Layout

```text
.openci_tf/config.yaml
terraform/
  <same-account-region>/
    01-vpc/
    02-ec2/
  <remote-target-region>/
    01-vpc/
```

Workload roots depend on their regional VPC root via `terraform_remote_state`.
Each VPC uses the lexicographically first available AZ in the region.

## openci-tf boundary

| Action | Who runs it | Notes |
|--------|-------------|-------|
| `plan`, `drift`, `report` | openci-tf PR command | Supported read-only verbs |
| `init`, `apply`, `destroy` | Operator tooling | Requires explicit account and destroy confirmations |
| AWS credential sourcing | Operator environment | Never committed to the repository |

openci-tf executors can read/write state only under
`targets/<owner>/<repo>/<folder>.tfstate` in the target account's state bucket.
Backend settings are checked into each root's `versions.tf` so plain `tofu init`
works without extra backend configuration.

## Preconditions

Before a credentialed openci-tf run, operators must complete setup outside this
sample repository:

1. Bootstrap the hub state bucket and lock table.
2. Bootstrap any remote target state bucket and lock table.
3. Deploy target executor roles.
4. Register account aliases.
5. Register this repository and create the GitHub webhook.
6. Ensure hub IAM allows the configured target accounts.

## Safe order

Initialize and apply VPC roots before workload roots in each region. Destroy in
reverse order: workloads first, then VPC roots. Direct `tofu apply` and
`tofu destroy` bypass openci-tf's guarded workflows and should be reserved for
operator-controlled maintenance.

## openci-tf verification

After registration, open a PR and comment:

```text
tf plan all
```

The command should discover all six reportable folders across the configured
account aliases.

## Local validation

```bash
just validate
```

This runs formatting, backend-disabled Terraform initialization/validation, and
fixture contract tests.
