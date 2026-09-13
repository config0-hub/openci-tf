# openci-install - fresh install into the two test accounts

Precondition: `openci-reset` finished clean (both `verify-clean` passed). Same
account table, credential files, and identity check as in that skill: name the
accounts leases-1 (hub + `primary`) and leases-2 (`secondary`) by credential
file, confirm `aws sts get-caller-identity` before each phase, never print
secrets. All work goes through `just`; `docs/INSTALL.md` is the authoritative
sequence - reread it before starting, then follow it with the values below.

## Fixed values for the test run

| item | value |
|---|---|
| gitops repo | `williaumwu/openci-test-gitops`, PR #1, branch `openci-test-run-1` (backends already point at the real account ids, commit `b70b57e`) |
| trigger id | `openci-test-gitops` |
| control PAT + private-module token | the one `GITHUB_TOKEN` in `~/project/repos/hamburger/exclude_folder/github/williamwu/openci-tf.env` (classic PAT; scope is deliberately not a concern for this run) |
| private-module dotenv SSM path | `/openci-tf/env/github/williaumwu/openci-test-private-modules` |
| engine image | pulled from GHCR by `just install`; rebuild/push creds only if needed: `~/project/repos/hamburger/exclude_folder/github/config0-hub/builds.env` |
| Lambda concurrency in fresh accounts | 10, so set `run_folder_max_concurrency` to `4` before deploy |

## Phase 1 - hub (leases-1)

```sh
just config set target_account_ids '["<leases-1 id>"]'
just config set run_folder_max_concurrency 4
just install
just verify                       # every check passed, or stop and fix
```

## Phase 2 - secrets and registration (leases-1)

```sh
just config set-stdin webhook_secret < <(openssl rand -hex 32)      # stdin only
just install-github-control-token <args: see just install-github-control-token>
just install-ssm-env /openci-tf/env/github/williaumwu/openci-test-private-modules <dotenv file>
just register-account --alias primary --account-id <leases-1 id> --enable-apply true
just target-create-aws-poweruser <leases-1 id>                     # mutation lane on the hub
just register-repo --trigger-id openci-test-gitops --repo-name williaumwu/openci-test-gitops \
  --git-url https://github.com/williaumwu/openci-test-gitops.git \
  --webhook-secret-ssm <path> --github-token-ssm <path> \
  --upstream-urls-json '{"tofu:1.8.0":"https://github.com/opentofu/opentofu/releases/download/v1.8.0/tofu_1.8.0_linux_amd64.tar.gz","tfsec:1.28.10":"https://github.com/aquasecurity/tfsec/releases/download/v1.28.10/tfsec_1.28.10_linux_amd64.tar.gz","infracost:0.10.39":"https://github.com/infracost/infracost/releases/download/v0.10.39/infracost-linux-amd64.tar.gz"}' \
  --github-capability-pr-number 1
just create-webhook --repo williaumwu/openci-test-gitops \
  --webhook-url <api_url>/webhook/openci-test-gitops \
  --secret-ssm <path> --github-token-ssm <path>
```

Read each script's `--help` for exact flags; the paths above are the ones the
previous run used. Redeliver the webhook ping from GitHub and require HTTP 200.

## Phase 3 - secondary (leases-2, then back to leases-1)

```sh
# leases-2 shell
just bootstrap
just target-onboard <leases-1 id>
# leases-1 shell
just register-target secondary <leases-2 id>
```

## Phase 4 - prove it

Comment `tf plan terraform/primary/ap-northeast-1/03-sqs` on PR #1 and require the
newest `openci-tf` Step Functions execution to end SUCCEEDED and a `Plan succeeded`
comment to appear. Then `tf plan all`: 21 folders, all ok, including the three
`02-dynamodb` folders that clone the private module.

## Done means

- `just verify` passed, webhook ping 200, both plans SUCCEEDED with comments.
- Report the exact commands run, every deviation from `docs/INSTALL.md`, and
  every snag with its root cause - the point of repeating this install is to
  reach zero snags, so nothing gets papered over.

## Appendix - test repository shape

The live openci-tf test setup uses three GitHub repositories owned by
`williaumwu`:

| repo | visibility | purpose |
|---|---|---|
| `williaumwu/openci-test-public-modules` | public | public Terraform modules |
| `williaumwu/openci-test-private-modules` | private | private Terraform module auth test |
| `williaumwu/openci-test-gitops` | private | GitOps repository for PR-driven openci-tf runs |

Repository URLs:

- `https://github.com/williaumwu/openci-test-public-modules`
- `https://github.com/williaumwu/openci-test-private-modules`
- `https://github.com/williaumwu/openci-test-gitops`

Module split:

```text
openci-test-public-modules/
└── modules/
    ├── vpc-basic/
    ├── sqs-queue/
    ├── cloudwatch-log-group/
    ├── s3-bucket/
    ├── sns-topic/
    └── eventbridge-rule/

openci-test-private-modules/
└── modules/
    └── dynamodb-table/
```

GitOps repository shape:

```text
openci-test-gitops/
└── terraform/
    ├── primary/
    │   └── ap-northeast-1/
    │       ├── 01-vpc/
    │       ├── 02-dynamodb/              # private module
    │       ├── 03-sqs/
    │       ├── 04-cloudwatch-log-group/
    │       ├── 05-s3-bucket/
    │       ├── 06-sns-topic/
    │       └── 07-eventbridge-rule/
    └── secondary/
        ├── ap-northeast-1/
        │   ├── 01-vpc/
        │   ├── 02-dynamodb/              # private module
        │   ├── 03-sqs/
        │   ├── 04-cloudwatch-log-group/
        │   ├── 05-s3-bucket/
        │   ├── 06-sns-topic/
        │   └── 07-eventbridge-rule/
        └── ap-northeast-2/
            ├── 01-vpc/
            ├── 02-dynamodb/              # private module
            ├── 03-sqs/
            ├── 04-cloudwatch-log-group/
            ├── 05-s3-bucket/
            ├── 06-sns-topic/
            └── 07-eventbridge-rule/
```

Expected counts in `openci-test-gitops`: 21 Terraform roots total; 3 VPCs,
3 DynamoDB tables, 3 SQS queues, 3 CloudWatch log groups, 3 S3 buckets,
3 SNS topics, and 3 disabled EventBridge rules. No EC2, IAM, security groups,
NAT gateways, or EIPs are intentionally part of this test GitOps repo.

The DynamoDB folders use the private module source:

`git::https://github.com/williaumwu/openci-test-private-modules.git//modules/dynamodb-table?ref=main`

All other folders use public module sources from:

`git::https://github.com/williaumwu/openci-test-public-modules.git//modules/<module>?ref=main`

Backend account ids: the fixed test branch `openci-test-run-1` at `b70b57e`
already carries the real hub and target account ids. Verify that mapping
(`grep -r 111111111111 terraform/` must find nothing) and make no remote
change. Only a newly created fixture branch still holds the placeholders
`111111111111` (hub) and `222222222222` (secondary); patch those before its
first run, after confirming which account is the hub and which aliases are
`primary` and `secondary`.

Private module cloning needs a GitHub token that can read
`williaumwu/openci-test-private-modules` (Contents: read). For this test run
that is the classic PAT in the `openci-tf.env` file listed under "Fixed
values" above. A replacement token may be fine-grained or classic as long as
it has that read permission. Store it as a dotenv value, never a raw token
string, for example:

```dotenv
GITHUB_TOKEN=<token-with-read-access-to-openci-test-private-modules>
```

Recommended SSM parameter path for that dotenv during openci-tf install/testing:

`/openci-tf/env/github/williaumwu/openci-test-private-modules`

Then add that path to each DynamoDB folder's `.openci_tf/config.yaml` as
`ssm_env_paths` before running PR tests against the private module.
