# sample-target-repo — multi-region openci-tf tracer

Disposable, multi-region Terraform sample for validating **openci-tf** plan-only workflows across **six** reportable Terraform roots: four in the TEST hub account (`REPLACE_MAIN_ACCOUNT`) and two VPC-only roots in the TEST2 target account (`REPLACE_SECONDARY_ACCOUNT`). Resources are intentionally minimal and tagged for teardown.

Canonical repository: `<REPO_ORG>/<REPO_NAME>` (matches backend state keys and openci-tf registration).

## Topology

```text
TEST hub account REPLACE_MAIN_ACCOUNT (alias REPLACE_MAIN_ALIAS)

terraform/eu-west-1/01-vpc       terraform/ap-northeast-1/01-vpc
├── VPC 10.40.0.0/16             ├── VPC 10.41.0.0/16
├── one public subnet (1 AZ)     ├── one public subnet (1 AZ)
├── Internet Gateway             ├── Internet Gateway
└── (no NAT)                     └── (no NAT)
         │                                │
         ▼                                ▼
terraform/eu-west-1/02-ec2       terraform/ap-northeast-1/02-ec2
├── t3.nano (AL2023, SSM)        ├── t3.nano (AL2023, SSM)
├── SNS → SQS + DLQ              ├── SNS → SQS + DLQ
└── DynamoDB pk/sk               └── DynamoDB pk/sk

TEST2 target account REPLACE_SECONDARY_ACCOUNT (alias REPLACE_SECONDARY_ALIAS)

terraform/test2-eu-west-1/01-vpc   terraform/test2-us-east-1/01-vpc
├── VPC 10.42.0.0/16               ├── VPC 10.43.0.0/16  (us-east-1; not ap-northeast-1)
├── one public subnet              ├── one public subnet
├── Internet Gateway               ├── Internet Gateway
└── state in TEST2 bucket          └── state in TEST2 bucket
```

Region note: informal requests for “US-East-11” are interpreted as **`us-east-1`** (`us-east-11` is not a valid AWS region). TEST2 VPC roots deploy only in `eu-west-1` and `us-east-1`.

### State grouping (six reportable roots)

| Root | Account | Owns | State bucket | State key |
|------|---------|------|--------------|-----------|
| `terraform/<region>/01-vpc` | TEST | VPC, subnets, routing, IGW | `openci-tf-state-REPLACE_MAIN_ACCOUNT` | `targets/.../terraform/<region>/01-vpc.tfstate` |
| `terraform/<region>/02-ec2` | TEST | EC2, SNS/SQS, DynamoDB | `openci-tf-state-REPLACE_MAIN_ACCOUNT` | `targets/.../terraform/<region>.tfstate` |
| `terraform/test2-eu-west-1/01-vpc` | TEST2 | VPC sample | `openci-tf-state-REPLACE_SECONDARY_ACCOUNT` | `targets/.../terraform/test2-eu-west-1/01-vpc.tfstate` |
| `terraform/test2-us-east-1/01-vpc` | TEST2 | VPC sample | `openci-tf-state-REPLACE_SECONDARY_ACCOUNT` | `targets/.../terraform/test2-us-east-1/01-vpc.tfstate` |

Workload roots in TEST depend on their regional VPC root via `terraform_remote_state`. Each VPC uses the lexicographically first available AZ in the region (no hard-coded AZ names).

## Creation vs openci-tf boundary

| Action | Who runs it | Notes |
|--------|-------------|-------|
| `plan`, `drift`, `validate`, `report` | **openci-tf** (PR comment) | Only supported CI verbs per current contract |
| `init`, `apply`, `destroy` | **Operator** (`just` recipes) | Requires explicit account confirmation; destroy also requires typing `DESTROY` |
| AWS credential sourcing | Operator environment | Never committed; source `/tmp/test.env` (TEST) or `/tmp/test2.env` (TEST2) before operator recipes |

openci-tf executors can read/write state only under `targets/<owner>/<repo>/<folder>.tfstate` in the **target account’s** state bucket. TEST folders use the hub bucket; TEST2 folders use the TEST2 bucket. Backend settings are checked into each root’s `versions.tf` so plain `tofu init` works without `-backend-config` injection.

### Operator guard bypass

Running `tofu init`, `tofu plan`, `tofu apply`, or `tofu destroy` directly inside a root folder **skips** the `just` account/destroy confirmations. Use `just` recipes for guarded workflows; direct OpenTofu is emergency fallback only.

### Preconditions (not Terraform)

Before the first credentialed openci-tf plan, operators must complete hub-side setup outside this repository:

1. Bootstrap hub state bucket `openci-tf-state-REPLACE_MAIN_ACCOUNT` and lock table `openci-tf-tf-locks` (see openci-tf `docs/INSTALL.md`).
2. Bootstrap TEST2 state bucket `openci-tf-state-REPLACE_SECONDARY_ACCOUNT` and lock table `openci-tf-tf-locks` in TEST2; deploy `openci-tf-executor-remote` via target-connect.
3. Register account aliases `REPLACE_MAIN_ALIAS` → `REPLACE_MAIN_ACCOUNT` and `REPLACE_SECONDARY_ALIAS` → `REPLACE_SECONDARY_ACCOUNT`.
4. Register this repository and create the GitHub webhook.
5. Ensure hub `target_account_ids` includes both `REPLACE_MAIN_ACCOUNT` and `REPLACE_SECONDARY_ACCOUNT`.

These steps are operational prerequisites; they are not provisioned by this tracer.

## Safe order

### Init backend (once per root)

Backend is committed; `just init-test` / `just init-test2` run plain `tofu init` after account confirmation. Apply VPC roots before workload roots in each TEST region:

```bash
source /tmp/test.env
just init-test eu-west-1/01-vpc
just init-test eu-west-1/02-ec2
just init-test ap-northeast-1/01-vpc
just init-test ap-northeast-1/02-ec2
```

TEST2 VPC roots (operator uses `/tmp/test2.env`):

```bash
source /tmp/test2.env
just init-test2 test2-eu-west-1/01-vpc
just init-test2 test2-us-east-1/01-vpc
```

### Create infrastructure (operator)

```bash
source /tmp/test.env
just apply-test eu-west-1/01-vpc
just apply-test eu-west-1/02-ec2
just apply-test ap-northeast-1/01-vpc
just apply-test ap-northeast-1/02-ec2
# or: just apply-test-all

source /tmp/test2.env
just apply-test2 test2-eu-west-1/01-vpc
just apply-test2 test2-us-east-1/01-vpc
# or: just apply-test2-all
```

### openci-tf verification (CI)

After hub registration (see Preconditions), on a PR comment:

```text
tf plan all
```

Supported verbs: `plan`, `drift`, `validate`, `report`. Apply and destroy are **not** available through openci-tf. `tf plan all` discovers exactly six reportable folders across both accounts.

### Teardown (reverse order)

```bash
source /tmp/test2.env
just destroy-test2 test2-us-east-1/01-vpc
just destroy-test2 test2-eu-west-1/01-vpc

source /tmp/test.env
just destroy-test ap-northeast-1/02-ec2
just destroy-test ap-northeast-1/01-vpc
just destroy-test eu-west-1/02-ec2
just destroy-test eu-west-1/01-vpc
```

## Local validation (no AWS credentials)

```bash
just ci-check
```

This runs `tofu fmt -check`, backend-disabled `init`/`validate` for all six roots, and repository contract tests.

### Disposable expiry

`approved_test_date` in each root's `locals.tf` marks the approved test window start. `ExpiresOn` tags are set to **seven days** after that date. Bump `approved_test_date` and recompute `ExpiresOn` when extending the window.
