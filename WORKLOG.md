# Integration worklog (merged lanes)

# Outer state-machine simplification work log

## Completed

- Removed the read outer's pure `RouteFolderConcurrency` pass-through; both `RenderPlaceholder` transitions now target `RunFolders` directly.
- Verified the mutation outers no longer contain the pure `RouteAfterFinalize`; `FinalizeRun` targets `PipelineFailed` directly.
- Moved folder result shaping into the render Lambda:
  - successful child envelopes become bounded outer Map outcomes;
  - missing `exec_id` and otherwise malformed child envelopes retain the `malformed child execution output` infrastructure-error result;
  - caught nested-execution failures retain the `nested execution failed` infrastructure-error result.
- Moved configuration-error result shaping into the render Lambda for read, apply, and destroy.
- Preserved mutation `MaxConcurrency = 1`, `GraceWait`, the fail-fast success assertion/Fail state, intent create/confirm routing, Choice/Fail safety states, and lane-specific read/apply/destroy child state-machine ARNs.

## Load-bearing states retained

`NormalizeFolderOutcome` remains as a Task because the nested `RunFolder` Catch and success transition need a common ASL target. The state no longer shapes data; the render Lambda does.

`NormalizeConfigError` remains as a Task because `ValidateAndResolve`'s typed `ConfigResolutionError` Catch must target an ASL state. The state now only invokes the render Lambda, which creates the bounded configuration outcome.

`SequentialNormalizeFolderOutcome` remains as a Task on the successful branch of the mutation fail-fast Choice. The Lambda now performs the former `SequentialMergeFolderOutcome` shaping; the Choice and its failure branch remain unchanged.

## Verification

- Focused structural and behavior suite, twice: `63 passed` each run.
- `ruff check` on all changed Python/test files: passed.
- `tofu -chdir=infra/deploy fmt -check -recursive`: passed.
- `tofu -chdir=infra/deploy validate`: passed using the machine-local cached AWS provider; only pre-existing deprecation warnings were emitted.
- `git diff --check`: passed.

A full `python3 -m pytest tests/unit -q` attempt reached `987 passed` but the repository baseline has unrelated environment/snapshot failures outside this task's permitted scope: tests invoke an unavailable `terraform` executable (only `tofu` is installed), checked-in anonymized `REPLACE_*_ACCOUNT` values fail existing 12-digit account validators and invalidate fixture provenance hashes, and `IMAGE_VERSION` is absent from `HEAD`. Those baseline files and the run-folder module were not changed.

# Work log — authoritative submission and terminal redaction

## Changed

- Split `prepare_and_submit` into an authoritative acknowledgement phase and a non-authoritative mutation progress-notification phase.
  - Engine acceptance is persisted first as create-only identity/status fields in a `submission#…#attempt#…` run-registry record with `status=accepted`, trigger/execution identity, attempt, and accepted timestamp.
  - Notification outcome is updated separately and cannot modify accepted status.
  - The Lambda envelope now adds `submission_status` and `notification_status`; operational comment failures return `notification_failed: true` plus a compact redacted error instead of raising as preparation failure.
  - Prepare IAM gained only the run-registry `GetItem`, `PutItem`, and `UpdateItem` permissions needed by this acknowledgement.
- Added `src/core/terminal_evidence.py` as the single recursive redaction-and-bounding policy.
  - Scrubs AWS access IDs, labeled credentials, bearer tokens, GitHub tokens, URL userinfo, and private-key blocks.
  - Caps text/encoded bytes, mapping fields, sequence items, keys, and nesting depth.
  - Done-marker top-level errors, derived errors, collect output, failure manifests, registry outcomes, pipeline/folder/mutation rendering, and manifest failure reasons now pass through it.
  - Full permitted command/done-marker detail remains in S3; ASL-facing errors are compact and pointers remain available.
- Preserved all six physical state machines and the exact seven-field engine payload. No ASL template or rendered run-folder fixture changed.
- Documented acknowledgement/notification semantics in `docs/APPLY.md` and terminal evidence policy in `docs/DESIGN.md`.

## Verification

- Focused contract/regression suite (run twice): **132 passed**.
  - Includes exact engine payload contract, lane isolation field contract, done-marker and outer-state bounds, collect/failure-manifest equivalence, phase-two rendering, terminal status compatibility, registry transactions, and the new submission/redaction tests.
- New tests prove:
  - accepted engine work is persisted before the comment attempt;
  - a comment exception leaves `submission_status=accepted` and durably records `notification_status=failed`;
  - the notification update cannot rewrite accepted status;
  - every terminal path imports/calls the shared redactor;
  - credential-like patterns are removed and every recursive field/count bound is enforced;
  - done-marker, manifest, PR, and pipeline failure text is redacted.
- `python3 -m ruff check` on all changed Python source and focused tests: **passed**.
- `git diff --check`: **passed**.
- `tofu fmt -check infra/deploy/modules/run_folder/iam.tf`: **passed**.
- `tofu -chdir=infra/deploy init -backend=false -input=false && tofu -chdir=infra/deploy validate`: **passed** (deprecation warnings only).

## Existing-suite environment notes

A direct host `pytest tests/` run reached **984 passed** before environment/checkpoint failures unrelated to this change: the host lacks a `terraform` binary (OpenTofu is installed), several checkpoint tests still use literal `REPLACE_MAIN_ACCOUNT`/`REPLACE_SECONDARY_ACCOUNT` values rejected by current account validation, and existing rendered-ASL/image/live-fixture expectations are stale at this checkpoint. The task-focused and touched-path suites above are green. The repository `just test` recipe could not be parsed in this worktree because its imported `.shared-llm/.../justfile` files are absent; no remote engine or GitHub/AWS mutation was attempted.

# Deadline and durable-lock work log

## What changed

- Added one canonical UTC `deadline_at` computed once during `ValidateAndResolve`.
  - Read lanes use the longest validated folder budget.
  - Apply/destroy lanes include every folder budget plus every sequential grace period.
  - Folder budgets that exceed the selected target role's mintable session lifetime are rejected during resolution.
- Persisted `deadline_at` on the run, terminal folder-attempt records, folder summaries, lock rows, and durable run-lock ownership rows.
- Threaded `deadline_at` through all three outer Maps and all three physical inner machines without changing their lane isolation or the engine's exact seven-field payload.
- Capped target-session requests and presigned artifact URLs by the remaining absolute deadline, added a post-upload/pre-submit deadline check, and changed polling to consume the absolute deadline rather than starting another relative budget.
- Made lock acquisition atomically write both the holder lock and a `run-locks#{run_id}` ownership row in the existing lock table. Lock TTL covers the complete permitted run plus a bounded closer margin.
- Changed `finalize-run` (including EventBridge abnormal invocations) to recover and release locks from the durable run index, independent of transient/corrupt Step Functions envelopes. Holder-checked late and duplicate releases remain idempotent.
- Regenerated `tests/fixtures/rendered/run_folder_state_machine.json` after the ASL plumbing change.

The six physical state machines remain separate, mutation remains sequential/fail-fast, `prepare_and_submit.py` notification ordering is unchanged, and the execution-engine payload remains exactly seven fields.

## Verification

- `311 passed` twice: deadline/lock tests, all acceptance suites, outer graph reachability/isolation, poll freshness, phase 0/security, phase 2 services, and registry transaction tests.
- `8 passed`: engine payload contract tests (`tests/contract`).
- `24 passed, 2 deselected`: run-folder service tests not dependent on the checkout's pre-existing account placeholder.
- Focused Ruff gate passed for all new and materially changed deadline/lock/finalizer modules.
- `python3 -m py_compile` passed for all `src/**/*.py`; `git diff --check` passed.
- OpenTofu formatting passed for touched modules.
- `tofu -chdir=infra/deploy validate` passed using the locally cached AWS provider (warnings only for pre-existing deprecated AWS provider arguments).
- Rendered fixture regeneration and source hash verification passed; all 38 inner states render.

A complete bare-host unit run was also attempted. This worktree's unrelated generated-install prerequisites are absent (`IMAGE_VERSION`, numeric replacements for `REPLACE_MAIN_ACCOUNT`/`REPLACE_SECONDARY_ACCOUNT`, and the kit-synced `.shared-llm` justfile imports), so those pre-existing tests/`just test` cannot complete from this checkout without installation-time material. The directly affected and structural suites above are green and repeatable.

# Inner run-folder simplification — worklog

Branch: iac-simplify/inner-machine. Work started by an UpAgent worker
(pi / gpt-5.6-sol, high effort) that was cut off at its time cap twice;
finished and verified by the requesting session.

## What changed

- **Lane-specific rendered ASL.** The shared run_folder template now renders a
  closed graph per lane: the read machine carries no mutation-lane states and
  the apply/destroy machines carry no safe-lane collector or shaping variants.
  New structural tests: `tests/unit/test_run_folder_lane_graphs.py` (graph
  closed and reachable per lane, lane action embedding, no opposite-lane
  collectors).
- **Credential-expiry retry cluster collapsed.** The ~14-state Choice/Pass
  cluster became one retry Choice plus a bookkeeping Lambda task
  (`persist_retry_attempt`), preserving one-retry-max and
  manifest-before-retry/terminal behavior. New tests:
  `tests/unit/test_credential_retry.py`.
- **Explicit polling.** The while-loop in `src/services/run_folder/poll_done.py`
  was replaced by a single-shot bounded probe plus an ASL
  `ProbeDone → RouteProbeResult → WaitBeforeProbe` loop honoring `deadline_at`
  from the execution context when present.
- **Test-helper fix (session):** `tests/helpers/rendered_run_folder_asl.py`
  hardcoded the `terraform` binary, which is absent on this machine; it now
  falls back to `tofu` (`_terraform_binary()`), which is what made the new
  lane-graph render tests runnable at all.

## Verification

- `python3 -m pytest tests/unit -q`: 28 failed / 994 passed / 92 errors.
  Baseline on `main`: 30 failed / 973 passed / 92 errors. Zero failures are
  new to this branch (set-compared); 2 baseline failures fixed, +21 passing.
- `tofu -chdir=infra/deploy validate`: Success (pre-existing deprecation
  warnings only).
- Rendered fixture `tests/fixtures/rendered/run_folder_state_machine.json`
  regenerated; source hash updated.

## Constraints held

Six physical machines remain six; engine payload remains exactly the seven
fields (trigger_id, s3_package_uri, sops_type, sops_path, commands_b64,
done_endpoint, execution_target); local commits only.

# Adversarial Step Functions review — F1–F8

## Finding-by-finding changes and equivalence evidence

- **F1:** Replaced the two-stage probe/retry routing with guarded rules in
  `RouteProbeOutcome`. Every `$.probe.*` value comparison now has an `IsPresent`
  predicate in the same `And` rule. The new double-expiry regression evaluates an
  attempt-1 `CredentialExpiredError` state with no `$.probe` and proves the Choice
  safely selects `WriteFailureManifest`, rather than raising `States.Runtime`.
- **F2:** Removed `RejectUnsafeAction` (review option 2) and all six lane-specific
  `Normalize*Failure*` definitions from the template, which removes four rendered
  Pass states from each inner machine. `ValidateAction` Default and all ordinary
  Task catches now target `WriteFailureManifest` directly. That Task supplies the
  raw-state/execution-start wrapper; the Lambda unwraps it, uses the execution
  start only when no submission timestamp exists, and synthesizes the rejected
  action reason from `LANE_MODE`. The leak-proof regression passed: raw
  `ssm_openci_tf_github_token`, `ssm_infracost_api_key`, `folder_config`, and
  `upstream_urls` keys and sentinel contents are absent from the persisted
  manifest, returned summary, and registry outcome.
- **F3:** Merged `RouteProbeResult` and `RetryOnCredentialExpiry` into
  `RouteProbeOutcome`, ordered as pending, probe retry, caught-error retry,
  complete, terminal, expired, then safe Default. The explicit Wait/probe loop,
  one-retry limit, bookkeeping-before-resubmit, and terminal manifest path remain.
- **F4:** Merged read-outer `CreateApplyIntent`/`CreateDestroyIntent` into
  `CreateIntent`, and their failure Passes into `FailCreateIntent`. The marker
  carries the routed action and the render regression proves a destroy failure is
  posted as `CreateIntent (destroy)`.
- **F5:** Removed read-outer `RouteAfterFinalize` and
  `ConfigResolutionFailed`; `FinalizeRun` now terminates at `PipelineFailed`,
  matching both mutation outers. Final rendering still happens before finalization.
- **F6:** Removed `RouteResolved`, `RenderNoOp`, and `FailRenderNoOp`.
  A no-folder resolution now returns empty `map_items`/`skipped` plus a bounded
  `deadline_at` and `no_op_reason`; the ordinary placeholder, empty Map, and final
  `RenderPR` path preserve the reason. The render regression proves the final PR
  comment still says `Plan skipped` and includes “no configured Terraform folders
  are affected”. Normal resolved events carry `no_op_reason = null`, making the
  final ASL parameter path total.
- **F7:** Removed `RenderEarlyPlaceholder` from the read, apply, and destroy
  outers. Safe actions and successful mutation confirmations now enter
  `ValidateAndResolve` directly; post-resolution placeholder behavior remains.
- **F8:** Corrected both mutation failure markers to
  `failed_step = "RunFoldersSequential"`.

## Rendered graph counts

Iterator states are included in the all-state column.

| Machine | Top-level | All states |
|---|---:|---:|
| read outer | 23 | 25 |
| apply outer | 23 | 28 |
| destroy outer | 23 | 28 |
| read inner | 10 | 10 |
| apply inner | 10 | 10 |
| destroy inner | 10 | 10 |
| **Total** | | **111** |

The exact total is 111, rather than the review's approximate 116: F2 option 2
removes all four normalizing Pass states per inner, and F4's four duplicate states
collapse to two (a two-state, not three-state, reduction). Rendered reachability,
dangling-transition, lane-isolation, sequential fail-fast, and state-count tests
all pass.

## Verification

- Focused structural/behavior suite: **231 passed**.
- Full `python3 -m pytest tests/unit -q`, twice: **1029 passed, 26 failed,
  92 errors** each run. Failure and error node-id sets exactly match the captured
  branch baseline (**1024 passed, 26 failed, 92 errors**); no new baseline failure
  or error exists.
- `python3 -m ruff check` on every changed Python/test file: passed.
- `tofu -chdir=infra/deploy fmt -check -recursive`: passed.
- `tofu -chdir=infra/deploy init -backend=false -input=false` followed by
  `tofu -chdir=infra/deploy validate`: passed (pre-existing deprecation warnings
  only).
- Rendered read-inner fixture and source hash regenerated; `git diff --check`
  passed.

All six physical machines, sequential `MaxConcurrency = 1` mutation execution,
`GraceWait`, fail-fast mutation routing, intent/confirm gates,
evidence-before-terminal-failure, explicit probe/Wait polling, and the frozen
seven-field engine payload remain intact.


# Audit prerequisites work log

Implemented the human-approved audit items 2–5 on `iac-simplify/audit-items`. The eight-field engine payload and all Step Functions topology remain unchanged.

## Item 2 — Per-folder target state authority

### What changed

- Added a compact STS inline session policy rendered for one repository, folder, action, account, project, and region.
- Matched the checked-in backend layout: `targets/<owner>/<repo>/<folder>.tfstate`.
- Limited S3 object access to that exact state ARN and `ListBucket` to that exact key.
- Limited DynamoDB lock access to the exact `<bucket>/<state-key>` lock ID. Mutation can also update the exact `-md5` state digest key. Read lanes can write only the lock row needed by `tofu plan`.
- Kept workload permissions unchanged through a broad session `Allow` whose `NotResource` excludes the state bucket and lock table; exact backend statements add back only this folder.
- Read lanes have no state `PutObject` or `DeleteObject`.
- Reused folder path validation, rejected IAM wildcard characters before ARN interpolation, and enforced STS's 2,048-character policy limit with `ConfigResolutionError`.
- Passed the rendered policy to `sts:AssumeRole`.

### Files

- `src/domain/accounts/target_session.py`
- `src/platform/aws/sts.py`
- `src/services/run_folder/prepare_and_submit.py`
- `infra/deploy/modules/run_folder/lambdas.tf`
- `tests/unit/test_target_session_policy.py`
- `tests/unit/iam_policy_evaluator.py`

## Item 3 — Hub resource protection without mutation permission boundaries

### What changed

- Removed the managed permissions boundary and boundary attachment from `executor-poweruser`.
- Added `DenyProtectedHubResources` to the target mutation poweruser policy and the legacy same-account mutation-capable executor policy.
- Protected the hub workflow lock and run-registry tables; state, tmp, package, and done buckets; executor and webhook roles; and the engine's IAM, Lambda, CodeBuild, Step Functions, and ECR resources.
- Preserved same-account backend access: the state bucket remains governed by the backend-only policy plus the per-folder STS session policy rather than an all-action deny.
- Updated installation verification to reject any poweruser permissions boundary and to accept a poweruser role only when no boundary is attached.
- Updated Terraform helpers to select `tofu` when `terraform` is absent.

### Files

- `infra/modules/executor-poweruser/main.tf`
- `infra/modules/executor-poweruser/outputs.tf`
- `infra/modules/executor-poweruser/tests/policy_render.tftest.hcl`
- `infra/modules/hub-setup/main.tf`
- `infra/modules/hub-setup/local_executor.tf`
- `scripts/verify.sh`
- `scripts/validate_terraform.sh`
- `tests/unit/iam_policy_evaluator.py`
- `tests/unit/test_apply_destroy_iam.py`
- `tests/unit/test_executor_iam_policy.py`
- `tests/unit/test_executor_policy_evaluator.py`
- `tests/unit/test_executor_role_split.py`
- `tests/unit/test_verify_poweruser_probe.py`

## Item 4 — Frozen account binding

### What changed

- Added a validated account-binding value that freezes account ID, readonly and mutation role names, ExternalId, and maximum TTL.
- Read runs resolve the alias once in `ValidateAndResolve`; compact Map state carries the frozen binding to the inner run-folder machine.
- Intent records persist the binding. Confirmed mutation runs use the intent's binding and do not resolve the alias again.
- Removed `load_account_alias` from the submit path.
- After `AssumeRole`, the prepare service calls `GetCallerIdentity` with the newly minted credentials. It records the expected and actual safe account IDs in the raised error and refuses packaging/submission on mismatch.
- Preserved the 50-folder Step Functions state budget by compacting account binding, folder config, and execution ID inside the outer Map envelope, then restoring their public names in `ItemSelector`.

### Files

- `src/domain/accounts/binding.py`
- `src/domain/intent/models.py`
- `src/domain/intent/gates.py`
- `src/services/intent/registry.py`
- `src/services/intent/confirm.py`
- `src/services/resolve/validate_and_resolve.py`
- `src/services/run_folder/prepare_and_submit.py`
- `src/domain/engine/inner_state.py`
- `src/domain/engine/outer_map_state.py`
- `infra/deploy/modules/openci_tf/step_function.tf`
- `infra/deploy/modules/openci_tf/step_function_mutation_outer.tf`
- `tests/unit/test_apply_destroy.py`
- `tests/unit/test_target_session_policy.py`
- `tests/unit/test_webhook_outer_state.py`
- outer-state budget and service tests under `tests/unit/`

## Item 5 — Generated package member reservation

### What changed

- Reserved `openci_tf_run.sh` and `secrets.enc.json` by basename across the whole cloned repository tree.
- Root, nested, and symlink collisions now raise `ConfigResolutionError` before execution.
- `ValidateAndResolve` runs the same preflight so GitHub PR rendering follows the existing normalized configuration-error path.
- The package builder enforces the rule again at the final archive boundary and removes partial archives on failure.
- Clean archives retain the same repository members, generated member names, and bytes.

### Files

- `src/platform/git/package.py`
- `src/services/resolve/validate_and_resolve.py`
- `tests/unit/test_package_security.py`

## Test evidence

- Focused items 2, 4, and 5 suite, twice: `171 passed` on each run.
- Mutation IAM and verification suite: `232 passed`.
- `tofu -chdir=infra/modules/executor-poweruser test -no-color`: `1 passed, 0 failed`.
- `tofu -chdir=infra/deploy init -backend=false -input=false` followed by `tofu -chdir=infra/deploy validate -no-color`: valid configuration; only pre-existing provider deprecation warnings.
- Targeted Ruff checks for every changed Python module and focused test file: passed.
- Full unit baseline before changes: `26 failed, 1029 passed, 92 errors`; 118 failing/error test names.
- Final full unit run: `25 failed, 1141 passed`; all 25 names were already in the 118-name baseline set. **New failing test names: zero.**
