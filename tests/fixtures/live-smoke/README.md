# Live smoke repository fixture

`sample-target-repo/` is the complete tracked snapshot of
`<REPO_ORG>/<REPO_NAME>` commit
`2f772fca6dfab7c92c2444e77ce9efc08118c32d`.

That exact commit supplied the Terraform configuration for the final live API
validation. The successful folders were:

- `terraform/ap-northeast-1/01-vpc` in hub account `REPLACE_MAIN_ACCOUNT`
- `terraform/test2-us-east-1/01-vpc` in target account `REPLACE_SECONDARY_ACCOUNT`

`sample-target-repo.snapshot.json` records the source repository, commit, live
folders, and SHA-256 digest of every copied file. The openci-tf unit suite verifies
that manifest and runs the live folders through the current discovery and plan
configuration resolver. The snapshot also retains its original contract test.

This fixture makes the branch self-contained for review and regression testing.
It does not make live AWS tests hermetic: a new live API run still requires a
registered Git repository commit because openci-tf deliberately fetches and pins
repository content by commit SHA.
