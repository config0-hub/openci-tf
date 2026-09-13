# openci-reset - tear down the live test install and prove it is gone

Repo: `openci-tf` checkout root. Run everything through `just`; never hand-run
`terraform destroy` on `infra/`. Never print, echo, or commit token or key values.

## Evidence

Set `EVIDENCE_DIR` to a fresh directory (default `/tmp/openci-reset/evidence`).
Before the first command, `mkdir -p "$EVIDENCE_DIR"`.

Every command you run in every phase must leave a raw output file under
`$EVIDENCE_DIR`: one file per command, named from the phase and step, containing
stdout, stderr, and a final line `exit_code: <n>`. The report must list every
evidence file path.

## Accounts - name them by credential file, never by number

| name | role in the test install | credentials |
|---|---|---|
| leases-1 | hub AND `primary` target | `~/project/repos/hamburger/exclude_folder/aws/gear+leases-1@thytruth.com/exports.env` |
| leases-2 | `secondary` target | `~/project/repos/hamburger/exclude_folder/aws/gear+leases-2@thytruth.com/exports.env` |

GitHub token (control + private modules): `~/project/repos/hamburger/exclude_folder/github/williamwu/openci-tf.env` (`GITHUB_TOKEN`).

Before every phase: `set -a; . <exports.env>; set +a` then
`aws sts get-caller-identity --query Account --output text` and compare it with
`AWS_ACCOUNT_ID` in that same file. Mismatch = stop. Every `just` recipe below
resolves the account from the caller identity, so a wrong shell means the wrong
account gets destroyed.

## Phase 0 - test resources created by PR runs (leases-1, region ap-northeast-1)

The gitops PR flow can leave real resources (queues, topics, and anything else a
folder under `terraform/primary/...` applies). List first, then judge:

```sh
for r in ap-northeast-1 ap-northeast-2; do
  aws sqs list-queues --region $r; aws sns list-topics --region $r
  aws dynamodb list-tables --region $r
  aws ec2 describe-vpcs --region $r --filters Name=tag:ManagedBy,Values=openci-tf --query 'Vpcs[].VpcId'
  aws logs describe-log-groups --region $r --query "logGroups[?contains(logGroupName,'openci')].logGroupName"
done
```

Resources tagged `ManagedBy=openci-tf` came from the test folders. Destroy them
the product way when the install is still up (`tf plan --destroy <folder>` ->
`tf destroy <folder>` -> `tf destroy confirm <token>` on the PR, see
`docs/APPLY.md`). If the install is already gone and resources remain, that is a
FAIL: do not delete them by hand. Record the listing in evidence and stop.
Repeat for leases-2 (its folders live under `terraform/secondary/...`).

## Phase 1 - secondary account (leases-2) first

The hub's IAM trusts the target roles; remove the target side while the hub id is
still known. `<hub_id>` is `AWS_ACCOUNT_ID` from the leases-1 file.

```sh
just target-delete-aws-poweruser <hub_id>   # only if it was created; "not found" is fine
just target-delete-aws-readonly  <hub_id>
OPENCI_TF_KEEP_STATE=no just bootstrap-destroy
./scripts/ssm_config.sh delete-all
just verify-clean
```

## Phase 2 - hub account (leases-1)

```sh
OPENCI_TF_KEEP_STATE=no just uninstall     # poweruser -> deploy -> engine -> foundation -> bootstrap, then SSM
just verify-clean                           # must print "verify clean: all checks passed"
```

If `just uninstall` or `just deploy-destroy` stops because a Terraform state lock
is present, the recipe prints the exact `terraform -chdir=infra/deploy
force-unlock <lock-id>` command and exits. Confirm no deploy is using that lock,
run only that printed command, record it as a snag in the report, then retry
`just uninstall`.

KMS keys show as pending deletion for up to 30 days; `verify-clean` lists them as
residuals and still passes. Anything else it reports is a real leftover: that is a
FAIL. Do not delete leftovers by hand. Fix the product teardown, rerun from the
failing phase, and record the snag.

## Phase 3 - beyond verify-clean (both accounts)

`verify-clean` checks the install footprint by name. Also sweep, per account, in
`us-east-1`:

```sh
aws ecr describe-repositories --query "repositories[?contains(repositoryName,'openci') || contains(repositoryName,'engine')].repositoryName"
aws codebuild list-projects
aws stepfunctions list-state-machines --query "stateMachines[?contains(name,'openci')].name"
aws lambda list-functions --query "Functions[?contains(FunctionName,'openci')].FunctionName"
aws dynamodb list-tables --query "TableNames[?contains(@,'openci')]"
aws s3api list-buckets --query "Buckets[?contains(Name,'openci')].Name"
aws logs describe-log-groups --query "logGroups[?contains(logGroupName,'openci')].logGroupName" --output text
aws iam list-roles --query "Roles[?contains(RoleName,'openci')].RoleName"
aws ssm describe-parameters --query "Parameters[?starts_with(Name,'/openci-tf')].Name"
```

Every list must be empty. A non-empty list is a FAIL: do not delete resources by
hand. Record the listing in evidence and stop.

## Phase 4 - GitHub

Delete the openci-tf webhook on the gitops repo (keep the repo, the PR, its
branch, and the placeholder-patched backends - the next install reuses them):

```sh
set -a; . ~/project/repos/hamburger/exclude_folder/github/williamwu/openci-tf.env; set +a
gh api repos/williaumwu/openci-test-gitops/hooks --jq '.[] | select(.config.url|contains("execute-api")) | .id'
gh api -X DELETE repos/williaumwu/openci-test-gitops/hooks/<id>
```

Do not delete PR comments; they are the record of the previous run.

## Done means

- Both `verify-clean` runs passed and every Phase 3 list is empty in both accounts.
- No webhook with an `execute-api` URL remains on the gitops repo.
- Report: per phase, the command, its exit status, the empty listing, and every
  evidence file path. Any leftover that required manual deletion is a product
  defect: record it as a FAIL snag, not as a successful step.
