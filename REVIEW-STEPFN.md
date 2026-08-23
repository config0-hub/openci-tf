# Adversarial review — can the Step Functions be simplified further?

Reviewed at head `589c228` on `iac-simplify/integration`. Scope: the three ASL sources, the
rendered read-lane fixture, and the Lambdas that the graphs invoke where their behavior decides
whether a state is load-bearing (`write_failure_manifest.py`, `persist_retry_attempt.py`).

## VERDICT: FURTHER_SIMPLIFICATION_POSSIBLE

Roughly 15–20 more rendered states (~11–15%) are removable with provable equivalence, and one
**correctness defect** in the new retry Choice must be fixed regardless. No finding below
violates the locked constraints (six machines, no shared router, sequential fail-fast mutation
with GraceWait, intent/confirm gates, evidence-before-failure, frozen 7-field payload, explicit
probe/Wait polling).

## Current state counts (verified by hand against source; iterator states included)

| Machine | Source | Top-level | All states | Was (audit) |
|---|---|---:|---:|---:|
| `openci-tf` (read outer) | `openci_tf/step_function.tf` | 31 | 33 | 37 |
| `openci-tf-apply` | `openci_tf/step_function_mutation_outer.tf` | 24 | 29 | 30 |
| `openci-tf-destroy` | same template, destroy lane | 24 | 29 | 30 |
| `openci-tf-run-folder` (read inner) | `run_folder/step_function.tf` | 15 | 15 | 38 |
| `openci-tf-run-folder-apply` | same template, apply lane | 15 | 15 | 38 |
| `openci-tf-run-folder-destroy` | same template, destroy lane | 15 | 15 | 38 |
| **Total** | | | **136** | **211** |

The read-lane fixture `tests/fixtures/rendered/run_folder_state_machine.json` matches the
template render (15 states). The earlier pass's claimed removals are real: no
`RouteFolderConcurrency` anywhere, and the mutation outers contain no `RouteAfterFinalize`
(verified by grep; the only remaining `RouteAfterFinalize` is the read outer's Choice, which the
worklog never claimed to remove).

---

## Findings (worst first)

### F1 — CORRECTNESS DEFECT, fix before further simplification: unguarded `$.probe.probe_status` rules in `RetryOnCredentialExpiry` can throw `States.Runtime` and bypass the failure manifest

**States:** `RetryOnCredentialExpiry` — `infra/deploy/modules/run_folder/step_function.tf:74-107`
(rendered: `tests/fixtures/rendered/run_folder_state_machine.json:193-250`).

Rules 3 and 4 test `Variable = "$.probe.probe_status"` with **no `IsPresent` guard**. A Choice
rule referencing a missing path throws `States.Runtime`, which is not catchable by this state and
terminates the execution immediately.

Reachable scenario: attempt 0 hits `CredentialExpiredError` in `PrepareAndSubmit` →
`BookkeepCredentialRetry` resubmits with `attempt = 1` and **strips `probe` and `error`**
(`persist_retry_attempt.py:49-57`, `resubmit_state`). The retried `PrepareAndSubmit` (or the
first `ProbeDone` before any pending probe has populated `$.probe`) throws
`CredentialExpiredError` again → Catch routes to `RetryOnCredentialExpiry` with `$.probe`
absent. Rule 1 is `IsPresent`-guarded (safe), rule 2 fails because `attempt = 1` is not
`NumericLessThan 1`, then rule 3 evaluates `$.probe.probe_status` on a missing path →
`States.Runtime` → the machine dies **without reaching `WriteFailureManifest`**. This violates
the locked evidence-before-failure property (manifest before terminal Fail), and the outer sees
a nested failure with no folder manifest.

`tests/unit/test_credential_retry.py` does not simulate this second-expiry routing (no
`probe_status` structural assertion exists there).

**Fix:** add `IsPresent` guards on rules 3/4 (or an `And` with `$.probe` present). Zero state
count change; this is a bug, not a style point. **Risk of not fixing: high.**

### F2 — Collapse the four failure-normalizing Pass states per inner into `WriteFailureManifest` (−4 states × 3 machines = −12)

**States:** `RejectUnsafeAction` (:20-38), `NormalizePrepareFailure[Mutation]` (:140-157,
:224-240), `NormalizeProbeFailure[Mutation]` (:158-176, :241-258), `NormalizeCollectFailure[Mutation]`
(:202-221, :283-301) — all in `run_folder/step_function.tf`.

These Pass states only re-shape state before `WriteFailureManifest`. But
`write_failure_manifest.py` **already extracts every one of those fields from raw state**: it
scans nested payloads `[$ , $.probe, $.result]` for `failure_reason`/`error.Cause`
(`_failure_reason`, :35-57), `attempt` (`_attempt`, :60-64), `credential_expired` (:67-73),
`exec_id`/`execution_id` (`_execution_id`, :76-88), and `submitted_at` (`_parse_submitted_at`,
:91-102). Proof it works on unnormalized input: the `RouteProbeResult` `expired` branch and the
`RetryOnCredentialExpiry` `Default` branch **already route raw state directly to
`WriteFailureManifest` in production paths today** (step_function.tf:65-67, :106).

Two things the Passes uniquely contribute, both movable:
1. `"submitted_at.$" = "$$.Execution.StartTime"` for prepare-phase/reject failures. Give
   `WriteFailureManifest` a `Parameters` block `{ "event.$": "$", "execution_started_at.$":
   "$$.Execution.StartTime" }` — the exact wrapper `BookkeepCredentialRetry` already uses
   (step_function.tf:111-114) and `persist_retry_attempt._unwrap_task_input` already parses —
   and add the same unwrap + start-time fallback in the manifest handler.
2. `RejectUnsafeAction`'s static reason string. The Lambda is deployed per-lane and can
   synthesize "action X not allowed in lane Y" when it receives a disallowed action with no
   error payload; or keep `RejectUnsafeAction` alone and still delete the other three.

Then every failure Catch and the `ValidateAction` Default target `WriteFailureManifest`
directly. Evidence-before-failure is untouched (manifest still written before `Fail`).
Equivalence obligation: the Passes currently whitelist fields (raw state additionally carries
`ssm_*`, `upstream_urls`, `folder_config`); the manifest builder only reads named fields, but a
test must prove nothing extra leaks into manifest/summary/registry. **Saving: 12. Risk: medium
(Lambda change + leak-proof tests).**

### F3 — `RouteProbeResult` and `RetryOnCredentialExpiry` decide `complete`/`terminal` twice (−1 state × 3 = −3)

**States:** `RouteProbeResult` (step_function.tf:59-68) and `RetryOnCredentialExpiry`
(:74-107). `complete` and `terminal` are matched in `RouteProbeResult`, routed to
`RetryOnCredentialExpiry`, and matched again there before reaching `Collect[Mutation]`. That is
a duplicated upstream decision. The two Choices can merge into one: order the rules
`pending → Wait`, `probe credential-retry → Bookkeep`, `error credential-retry → Bookkeep`,
`complete|terminal → Collect`, `expired → WFM`, `Default → WFM`, and point both
`CredentialExpiredError` Catches at the merged Choice. The stale-`$.error` hazard does not
arise because `resubmit_state` strips `error`/`probe` (`persist_retry_attempt.py:49-57`), and
post-retry matches are blocked by `attempt NumericLessThan 1`. All probe-path rules must be
`IsPresent`-guarded (see F1). Naming also improves: routing a successful `complete` probe
through a state called `RetryOnCredentialExpiry` is actively misleading. **Saving: 3. Risk:
low-medium (rule-order proof + F1 guards required).**

### F4 — Read outer: `CreateApplyIntent` / `CreateDestroyIntent` and their two Fail Passes are duplicates (−3 states)

**States:** `openci_tf/step_function.tf:20-31` (two Tasks, identical `Resource =
lambda_arns["intent-create"]`, identical Catch shape) and `:250-265`
(`FailCreateApplyIntent`/`FailCreateDestroyIntent`, differing only in the `failed_step`
string). The only reason two Tasks exist is to reach two differently-labeled Fail Passes.
Merge into one `CreateIntent` + one `FailCreateIntent` (`failed_step = "CreateIntent"`);
`$.action` is already in state and `RenderPipelineFailure` receives the full state
(`ResultPath = null`, :304), so the apply/destroy distinction survives in the rendered comment
if the render Lambda includes the action. This does not touch the intent gate itself
(`RouteAfterIntent`/`IntentFailed` stay). **Saving: 3. Risk: low (slightly coarser
`failed_step` label).**

### F5 — Read outer: `RouteAfterFinalize` + `ConfigResolutionFailed` duplicate a decision the mutation outers already dropped (−2 states, semantic caveat)

**States:** `openci_tf/step_function.tf:315-330`. After `FinalizeRun`, this Choice only selects
between two terminal `Fail` states whose sole difference is the error code
(`ConfigResolutionFailed` vs `PipelineFailed`). The mutation outers already collapsed exactly
this: their `FinalizeRun` goes straight to `PipelineFailed`
(`step_function_mutation_outer.tf:68-74`), so a mutation config-resolution failure already
terminates as `PipelineFailed`. The PR comment is rendered before this point; the distinct
error code is observability-only. Consumers found: only
`tests/unit/test_outer_state_machine.py:129-132` asserts it. Removable for symmetry with the
mutation lanes, **but this changes an externally visible terminal error code** — I can prove
graph equivalence, not that no operator/alarm keys on `ConfigResolutionFailed`. Recommend only
with a deliberate decision. **Saving: 2. Risk: low technically, needs a human observability
call.**

### F6 — Read outer no-op lane: `RouteResolved` + `RenderNoOp` + `FailRenderNoOp` (possible −3, equivalence NOT proven)

**States:** `openci_tf/step_function.tf:77-102, 274-281`. If `ValidateAndResolve` returned a
no-op as `map_items = []` with the reason in `skipped`, the normal path
(`RenderPlaceholder → RunFolders(empty) → RenderPR → Done`) would subsume it: an empty Map
succeeds with empty `outcomes`, and `RenderPR` already receives `skipped`. That deletes three
states and one render branch flag. However, the rendered PR comment would change (placeholder +
final instead of a single no-op comment), and `RenderPR`'s no-op formatting would need to
absorb `no_op_reason`. I cannot prove output equivalence from the graphs alone, so this is
flagged as possible, not asserted. **Saving: 3. Risk: medium (user-visible comment shape).**

### F7 — `RenderEarlyPlaceholder` vs `RenderPlaceholder`: two best-effort cosmetic renders per outer (possible −1 × 3, product decision)

**States:** `openci_tf/step_function.tf:50-67, 104-121`; `step_function_mutation_outer.tf:194-211,
232-249` (×2). Both are best-effort (Catch continues), both call the same `render-pr` Lambda.
The early one exists to acknowledge the comment before validation (seconds earlier). Removing it
loses immediate user feedback — an operational/UX property, not a safety one. Cannot be proven
equivalent; listed only so the state has to justify itself, which it does *if* the instant-ack
comment is wanted. **Saving: 0–3. Risk: UX regression; not recommended without a product call.**

### F8 — Cosmetic: mutation outers' `FailRunFolders` records `failed_step = "RunFolders"` but the failing state is `RunFoldersSequential`

`step_function_mutation_outer.tf:322, 498`. Zero states; rendered evidence mislabels the step.
One-string fix.

### F9 — Cosmetic: `RouteProbeResult`'s explicit `expired` rule and its `Default` both target `WriteFailureManifest`

`run_folder/step_function.tf:65-67`. The rule is redundant with the Default (kept only as
documentation of the expected status). Zero state saving; fine to keep.

---

## States challenged and verified as load-bearing (must stay)

| State(s) | File:line | Why it must stay |
|---|---|---|
| `NormalizeConfigError` (all 3 outers) | `step_function.tf:124-132`; `mutation_outer.tf:223-231, 399-407` | `ValidateAndResolve`'s typed `ConfigResolutionError` Catch must target a state, and no other state passes `normalize_config_error` parameters. Task-not-Pass per the prior pass; shaping lives in the Lambda. Cannot merge into `RenderPR` because a Catch cannot set Parameters. |
| `NormalizeFolderOutcome` (read Map iterator) | `step_function.tf:170-178` | Common required target for the nested-execution success and Catch paths; the minimum iterator is exactly 2 states. Verified minimal. |
| `SequentialRouteChildOutcome` + `SequentialFailFolderIteration` | `mutation_outer.tf:291-313, 467-489` | The explicit fail-fast success assertion on `MaxConcurrency=1` mutation — a locked property. Folding the assertion into the normalize Lambda (throw on unsuccessful child) would preserve fail-fast but hide a safety assertion inside code; rejected. `SequentialRunFolder` deliberately has no Catch so child failure propagates to the Map Catch — verified correct fail-fast wiring. |
| `SequentialNormalizeFolderOutcome` | `mutation_outer.tf:304-312, 480-488` | Only success-branch shaper; Task per prior pass. Minimal. |
| `GraceWait` | `mutation_outer.tf:279-283, 455-459` | Locked. |
| `ValidateAction` + single-action allowlist (mutation inners) | `run_folder/step_function.tf:13-19` | Locked static one-action allowlist per physical machine; not redundant with the outer's routing because the inner must be safe even if invoked directly. |
| `WriteFailureManifest` → `FolderExecutionFailed` / `WriteFailureManifestFailed` | `run_folder/step_function.tf:120-137` | Evidence-before-failure terminal commit; both Fail states carry distinct causes (manifest written vs writer exhausted). Locked semantics. |
| `ProbeDone`/`RouteProbeResult`/`WaitBeforeProbe` loop | `run_folder/step_function.tf:49-73` | Locked explicit polling model; already the audit's minimal 1-Task/1-Choice/1-Wait target shape (modulo F3's upstream merge). |
| `BookkeepCredentialRetry` | `run_folder/step_function.tf:108-119` | Persists attempt-0 evidence before the single resubmit (manifest-before-retry); replaces the old ~14-state cluster. Verified real in `persist_retry_attempt.py`. |
| Seven/five `FailX` Pass states (outers) | `step_function.tf:234-289`; `mutation_outer.tf:318-322, 494-498` | A Catch cannot inject parameters or reference `$$.State.Name`; these Passes are the only way to record `failed_step` while preserving full state (`ResultPath = "$.pipeline_failure"`) so `FinalizeRun` can release locks. Only the F4 pair-merge is safe. |
| `Done` Pass | all three outers | Choice `Default` must target a state; a Choice cannot `End`. (Could be `Type = Succeed`; same count.) |
| `RouteAfterRender` / `FinalizeAfterRenderFailure` / `RenderPRFailed` | shared terminal block | Distinguish render-failure (finalize then Fail) from success (Done) and gate lock release; each path is behaviorally distinct. |
| `RouteAfterIntent`/`RouteAfterConfirm` + `IntentFailed` | outers | Locked intent/confirm gates. |

## Prior-pass claims audited

- `RouteFolderConcurrency` removed — **confirmed** (grep: absent).
- Mutation `RouteAfterFinalize` removed — **confirmed** (only the read outer's Choice remains,
  which was never claimed).
- ~14-state credential-retry cluster → 1 Choice + 1 Task — **confirmed** (but see F1).
- Lane-closed inner graphs, no opposite-lane collectors — **confirmed** in the template
  comprehensions (`run_folder/step_function.tf:139-222` mutation-only, `:223-302` read-only)
  and the rendered read fixture (15 states, no `*Mutation` states).
- Poll while-loop → single-shot probe + ASL loop — **confirmed** in graph shape.

## Net estimate

Provable savings: F2 (12) + F3 (3) + F4 (3) = **18 states** (136 → 118), plus F5/F6/F7
(up to 8 more) pending human decisions on error-code and comment semantics. F1 is mandatory
regardless and saves nothing.
