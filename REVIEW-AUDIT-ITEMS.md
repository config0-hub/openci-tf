# Adversarial review — audit prerequisites 2–5 (iac-simplify/audit-items @ 17c334d, 5 commits ahead of main @ 8f59984)

## VERDICT: VEERED

Independently re-ran the focused suites (283 passed), the tofu module test
(1 passed), the baseline comparison for the changed test files, and live
Python probes against the reservation code. One concrete bypass survives in
item 5; everything else checked out against the spec.

## Findings (worst first)

### F1 — Item 5 reservation is bypassed by a DIRECTORY named like a reserved member (VEER)

`src/platform/git/package.py:81-82` — `_iter_packable_files` checks
`_RESERVED_MEMBER_BASENAMES` only on the symlink branch (line 74) and the
regular-file branch (line 84). A **directory** named `openci_tf_run.sh` or
`secrets.enc.json` hits the `entry.is_dir()` branch (line 81) and is pushed to
the walk stack with **no reservation check**.

Reproduced live in this worktree:

```
root/openci_tf_run.sh/x.tf   (directory + file)
→ validate_reserved_package_names(root): no error   # preflight passes
→ build_package(...): succeeds; archive members:
  ['openci_tf_run.sh/x.tf', 'openci_tf_run.sh', 'secrets.enc.json']
```

The produced archive contains BOTH the repo-controlled tree `openci_tf_run.sh/…`
and the generated file member `openci_tf_run.sh`. On engine-side extraction the
directory and the generated script collide — exactly the collision class the
reservation exists to refuse, and it now fails **after** credential minting,
secret packaging, and upload instead of loudly at ValidateAndResolve. The
same holds for a directory named `secrets.enc.json`. The work order explicitly
listed "directories named like reserved files" as a bypass to check;
`tests/unit/test_package_security.py` covers root files, nested files, and
(implicitly, via code path) symlinks — but has no directory case.

Claim vs reality: WORKLOG says "Root, nested, and symlink collisions now
raise `ConfigResolutionError` before execution" — true as far as it goes, but
the reject-anywhere guarantee of the spec ("any generated member" cannot be
shadowed by repo content) does not hold for directory entries.

Fix is small: check the basename before the `entry.is_dir()` branch (or at the
top of the loop for every entry type), plus one parametrized test for
directory and symlink variants.

### F2 — 8 tests in `tests/unit/test_phase1_completion.py` now fail on a dead monkeypatch (note, not a new regression)

Lines 88, 152, 208, 265 still do
`monkeypatch.setattr(prepare_and_submit, "load_account_alias", …)`; that
attribute was removed by item 4, so these tests now die with
`AttributeError` instead of their previous baseline failure
(`hub_account_id must be exactly 12 decimal digits` from the
`REPLACE_*_ACCOUNT` placeholder environment). Verified: the same 8 test names
fail on main, so the "zero new failing test names" claim is **true** — but the
failure cause changed, and these tests can no longer recover even once
install-time account material is present. They exercise the prepare handler
(presign ordering, external-id validation, expiry classification) and are now
dead until rewritten against the frozen-binding event shape.

### F3 — Item 3: what was actually removed vs main (assessed, prominently, per the order — no net weakening found)

Main carried `aws_iam_policy.executor_poweruser_permissions_boundary` attached
to the role (`infra/modules/executor-poweruser/main.tf` on main, lines 59-213).
The branch deletes the boundary policy, its attachment, and its two outputs.
Assessment of what that layer provided and where it went:

- The boundary's unconditional Denies (`iam:CreateUser`, `iam:CreateAccessKey`,
  `cloudformation:*`, etc.) **duplicate** the inline
  `DenyIamAndCloudFormationUnconditionally` statement, which pre-existed on
  main and is unchanged on the branch (`main.tf:249`). Same for
  `DenyIamLifecycleOutsideWorkloadResources` (`main.tf:198`).
- The boundary's state-bucket ceiling is replaced by the per-folder STS session
  policy (item 2) plus the pre-existing inline
  `DenyStateBucketNonBackendPrimitives` / `DenyListBucket*` /
  `DenyControlPlaneStateAndSourceRecord` statements.
- Residual delta: the boundary constrained ANY session on the role even if a
  future code path forgot the session policy; that belt-and-suspenders layer is
  gone. That is precisely the risk the human accepted in the 2026-08-20
  decision ("NO permission boundaries — not even opt-in. Accepted risk,
  explicitly."). Removal implements the decision; no guard that existed only
  in the boundary was lost.
- `scripts/verify.sh` now FAILS when a poweruser boundary is attached
  (inverted from main), and `iam_policy_evaluator.render_poweruser_boundary_policy`
  raises if the module reintroduces a boundary. Consistent with the decision.

### F4 — Item 3 nuance: hub STATE bucket is excluded from `DenyProtectedHubResources` in the same-account case (disclosed, verified acceptable)

`infra/modules/executor-poweruser/main.tf` `protected_hub_bucket_arns` adds the
hub state bucket only when `target_account_id != hub_account_id`; and
`infra/modules/hub-setup/main.tf` (legacy same-account executor-local) never
includes the state bucket in the deny list. WORKLOG discloses this ("state
bucket remains governed by the backend-only policy plus the per-folder STS
session policy"). Verified the remaining inline `DenyStateBucketNonBackendPrimitives`
and backend-only Allows still guard it, and the tofu-rendered evaluator test
`test_poweruser_explicitly_denies_protected_hub_resources` passes against the
real rendered policy. Acceptable, but the spec's literal "state … buckets"
deny is conditional, not absolute — recording it so the human knows.

## Per-item verification detail

### Item 2 — backend session scoping: PASS
- Policy attached in the production submit path: `prepare_and_submit.py:573-580`
  renders it and `:685-693` passes it into `_assume_target_role`, which forwards
  `policy_json` to `sts.assume_role` (`src/platform/aws/sts.py:31-32`,
  `Policy` request field). Not merely defined.
- Exact ARNs, no wildcard: `target_session.py` interpolates
  `targets/<owner>/<repo>/<folder>.tfstate` after `decode_folder_id(encode_folder_id(...))`
  validation and an explicit `*?[]` rejection (`target_session.py:26-29`).
- folder-a denied folder-b: `test_folder_a_session_does_not_allow_folder_b_state_or_lock`
  evaluates the real rendered JSON through the evaluator (implicit-deny in the
  session intersection — correct STS semantics, no Deny statement needed). Ran it; passes.
- Read lane: `_state_actions("plan") == ["s3:GetObject"]` — no Put/Delete;
  lock still works: read lane keeps `dynamodb:PutItem`/`DeleteItem` on exactly
  the `<bucket>/<key>` lock ID (`_lock_write_keys`) and `GetItem` on the
  `-md5` digest, which is what `terraform plan`'s DynamoDB locking uses.
- 2048 cap loud: `ConfigResolutionError` at `target_session.py:141-144`;
  covered by `test_session_policy_limit_fails_loud`.
- Workload not narrowed: `KeepWorkloadAuthority` `Allow *` with `NotResource`
  limited to the state bucket + lock table only.
- `PROJECT_NAME` env threaded into the lambda (`run_folder/lambdas.tf:21`).

### Item 3 — hub protection: PASS (see F3/F4)
- Only Deny statements added (`DenyProtectedHubResources` in
  executor-poweruser and hub-setup executor_local); no boundary anywhere:
  grep for `permissions_boundary` in `infra/modules/executor-poweruser/` is
  clean, and the evaluator + `test_poweruser_policy_has_no_permissions_boundary`
  enforce it.
- Coverage: locks table, run-registry (+ index ARNs), tmp/package/done buckets
  (+ state cross-account), init-job lambda, codebuild project, state machine +
  executions, ECR repo, ten hub roles. Tenant resources stay allowed
  (`test_poweruser_still_allows_ordinary_tenant_resources`).
- Tofu-rendered proof: policies extracted from the module and rendered through
  real `tofu console`; `tofu -chdir=infra/modules/executor-poweruser test`:
  **1 passed** (re-ran myself).

### Item 4 — binding freeze: PASS
- Alias resolved once: read lanes at ValidateAndResolve
  (`validate_and_resolve.py:249-252`); mutation lanes pinned at intent
  creation (`gates.py:118-121` freezes `account_binding_from_alias(...)` into
  the `FolderPlanPin`, the PR-SHA "pin at intent" pattern) and
  ValidateAndResolve for a confirmed run consumes the pin's binding
  (`validate_and_resolve.py:243-247`) with an account_id cross-check.
- No resolution in submit path: AST test
  `test_submit_path_does_not_resolve_account_alias_again` plus my own grep —
  remaining `load_account_alias` call sites are aliases.py (definition),
  validate_and_resolve (read lane), gates.py (intent creation). Confirm path
  (`confirm.py`) only forwards the stored pin.
- STS check: `_assume_target_role` calls `GetCallerIdentity` with the newly
  minted credentials and raises
  `"assumed target identity account mismatch: expected …, got …"` before any
  packaging/submission (`prepare_and_submit.py:100-105`);
  `test_assumed_identity_mismatch_refuses_execution` passes.
- Compact Map plumbing (`b`/`c`/`e`) round-trips through
  `compact_map_item`/`merge_map_item` and all three outer ASL templates.

### Item 5 — name reservation: FAIL (F1)
- File and symlink collisions anywhere in the tree: verified rejected, and the
  rejection flows through `ConfigResolutionError` raised inside
  ValidateAndResolve **before** any run-folder execution or credential
  minting (`validate_and_resolve.py:192`), i.e. the normalized config-error →
  PR-comment path.
- Clean folders package unchanged: byte-for-byte member assertions in
  `test_regular_files_are_packaged_without_changing_generated_members`.
- Case variations: basename match is case-sensitive; engine extraction is
  Linux ext4, so `OPENCI_TF_RUN.SH` does not collide — acceptable.
- **Directory named like a reserved member: NOT rejected — see F1.**

### Cross-cutting: PASS
- Zero new failing test names: independently confirmed for the touched
  `test_phase1_completion.py` (same 8 names fail on the main checkout);
  focused suites re-run here: **283 passed**.
- The 92 former collection errors: spot-ran formerly-erroring rendered-ASL
  suites (`test_rendered_asl_remediation.py`, `test_run_folder_lane_graphs.py`):
  **25 passed**, genuinely executed (tofu fallback), not skipped.
- Engine payload: still exactly eight fields — `EnginePayload(execution_id,
  s3_package_uri, "kms", "", commands_b64, done_endpoint, execution_target,
  execution_budget)` with `timeout_seconds` last; untouched by this branch.
- Step Function topology: only `ItemSelector` field renames (`b`/`c`/`e` +
  `account_binding`) inside the existing Maps; no state added/removed.
- No weakened assertions found: the evaluator's fake all-Allow "boundary" is
  semantically correct post-removal (real world has no boundary); denials are
  asserted against the real rendered inline policy.

## Per-item table

| Item | Verdict |
|---|---|
| 2 — backend session scoping | PASS |
| 3 — hub protection, no boundaries | PASS (F3/F4 noted) |
| 4 — binding freeze | PASS |
| 5 — name reservation | **FAIL — directory bypass (F1)** |
| Cross-cutting (baseline, payload, topology) | PASS (F2 noted) |

## WHY

Items 2–4 match the spec with evidence I re-ran myself, but item 5's
reject-anywhere reservation is bypassable by a repository **directory** named
`openci_tf_run.sh` or `secrets.enc.json` — reproduced live: preflight passes and
the built archive contains colliding members — so the phase does not yet meet
its contract.
