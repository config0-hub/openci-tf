# openci-tf user story acceptance matrix

Stable IDs for PR-visible acceptance checks. Each story lists the operator command,
expected PR comment shape, artifact pointers, and safe live verification hooks.
No story authorizes unattended mutation in CI; apply/destroy always require human
confirmation.

**Accounts (live test hub):** hub `REPLACE_MAIN_ACCOUNT`, targets `REPLACE_MAIN_ACCOUNT` (same-account)
and `REPLACE_SECONDARY_ACCOUNT` (TEST2). **Regions:** read lane `us-east-1` / `eu-west-1` only for
Phase B live work; never `us-west-1` or `ap-northeast-1` for Phase B.

| Field | Meaning |
|-------|---------|
| Mutation risk | `none` = read-only lane; `intent` = posts token only; `confirmed` = poweruser after confirm |
| PR-visible | What must appear in the GitHub PR comment thread |
| Artifacts | S3 manifest keys under `openci-tf/<repo>/<execution_id>/<folder>/` |
| SFN | Outer / inner Step Functions execution status |
| CodeBuild | Engine build when mutation lane or engine invoked |
| Live state | Optional post-run AWS resource check (human-gated) |

---

## US-01 — Plan one folder

| | |
|---|---|
| **Command** | `tf plan <folder>` (e.g. `tf plan infra/vpc`) |
| **Accounts** | Folder `account_alias` target (readonly executor) |
| **Regions** | Folder backend region |
| **Preconditions** | PR open, non-fork, collaborator write/admin, pinned head SHA, folder lock free |
| **Mutation risk** | `none` |

**PR-visible checks**

- Collapsed folder comment: `` `folder` · `account_id` · `sha` · Plan succeeded ``
- Sections: Initialize, Validate, Plan (summary + diff), Security Scan, Cost Analysis (when configured)
- Plan summary lines: `N to add`, `N to change`, `N to destroy`
- Non-empty `diff` fence with `+`/`-`/`~` resource lines when changes exist
- CI Details with Step Functions link and SUCCESS/FAILED
- Execution Artifacts block with execution ID and manifest URI (single-folder plan)

**Artifact checks**

- `init.out`, `validate.out`, `tf/plan.out`, `tfsec.json`, `infracost.json` (latter two may be skipped)
- Binary: `tf/plan.tfplan`, `plan-metadata.json`, manifest `plan.tfplan` entry with checksum

**Step Functions checks**

- Outer `openci-tf` execution SUCCEEDED
- Inner `openci-tf-run-folder` per folder SUCCEEDED

**CodeBuild checks**

- Read lane: engine CodeBuild completes (when used by deployment)

**Live-state checks**

- Optional: `data.aws_caller_identity` in plan output matches registered target account

---

## US-02 — Report all / readonly all path

| | |
|---|---|
| **Command** | `tf report all` or `tf plan all` / `tf drift all` (multi-folder read) |
| **Accounts** | All folders' registered targets |
| **Regions** | Per-folder |
| **Preconditions** | Same as US-01; `all` resolves configured folders |
| **Mutation risk** | `none` |

**PR-visible checks**

- One collapsed comment per folder (linked from summary)
- **Multi-folder summary table** posted: Folder, Account, Drift Check, Security, Cost
- Drift cell derived from `tf/plan.out` (`clean` / `changes` / `unknown`)
- CI Details on summary when outer run completes

**Artifact checks**

- Per-folder manifest complete; `tf/plan.out` present for drift/report cells

**Step Functions checks**

- Outer Map state processes all folders; aggregate SUCCEEDED when all succeed

**CodeBuild checks**

- Per-folder engine runs complete

**Live-state checks**

- Summary drift column matches per-folder plan text (no false `clean` when changes present)

---

## US-03 — Apply one folder after plan + intent confirmation

| | |
|---|---|
| **Commands** | `tf plan <folder>` then `tf apply <folder>` then `tf apply confirm <token>` |
| **Accounts** | Target with `enable_apply` + folder `apply.allow: true`; poweruser role |
| **Regions** | `us-east-1` / `eu-west-1` for Phase B live |
| **Preconditions** | Successful plan on same PR head SHA; approved review if `require_approval`; token within 600s |
| **Mutation risk** | `intent` on step 1; `confirmed` on confirm |

**PR-visible checks**

- Plan comment per US-01 before apply
- In-progress: grace period message, Step Functions link, CodeBuild link with hub account note
- Terminal: `Apply succeeded` or `Apply failed`, pinned `plan.tfplan`, bounded `tofu show` output

**Artifact checks**

- Source plan run: `tf/plan.tfplan` pinned by `source_plan_run_id`
- Apply run: `apply.out`, `plan-show.out`, manifest records `source_plan_run_id`

**Step Functions checks**

- Intent: read outer `openci-tf` only
- Confirm: outer `openci-tf-apply`, inner `openci-tf-run-folder-apply` SUCCEEDED

**CodeBuild checks**

- `tofu show` then `tofu apply` on pinned plan; build SUCCEEDED

**Live-state checks**

- Resource changes visible in target account match plan (human verifies; e.g. EC2 instance id)

---

## US-04 — Destroy one folder via destroy plan + human confirmation

| | |
|---|---|
| **Commands** | `tf plan --destroy <folder>` then `tf destroy <folder>` then `tf destroy confirm <token>` |
| **Accounts** | Same gates as apply with `destroy.allow: true` |
| **Regions** | Phase B: `us-east-1` / `eu-west-1` |
| **Preconditions** | Newest successful `plan_destroy` on same PR SHA; token valid |
| **Mutation risk** | `intent` then `confirmed` |

**PR-visible checks**

- **Destroy plan comment must not show empty diff** (regression fixed): uses `destroy.plan.out`
- Summary line: `Plan_Destroy succeeded` (or failed with error)
- Plan section shows destroy counts and `- resource … will be destroyed` lines
- No multi-folder summary for single-folder `plan_destroy` (renderer skips summary table)
- Destroy confirm: same mutation comment pattern as US-03 with `destroy.plan.tfplan` pin

**Artifact checks**

- `plan_destroy` run: `destroy.plan.out`, `tf/destroy.plan.tfplan`, `destroy-plan-metadata.json`
- No `tf/plan.out` on destroy-plan runs
- Destroy run: `destroy.out`, manifest `source_plan_run_id` → plan_destroy execution

**Step Functions checks**

- `plan_destroy`: read lane outer + inner SUCCEEDED
- Confirm: outer `openci-tf-destroy`, inner `openci-tf-run-folder-destroy` SUCCEEDED

**CodeBuild checks**

- Destroy confirm: show + apply destroy plan SUCCEEDED

**Live-state checks**

- Resources absent after destroy (e.g. EC2 empty, security group removed)

---

## US-05 — Invalid / blocked command

| | |
|---|---|
| **Commands** | Examples: `tf apply` without plan, fork PR comment, non-collaborator, `tf apply confirm badtoken`, bare `apply`/`destroy` on disallowed folder |
| **Accounts** | n/a |
| **Regions** | n/a |
| **Preconditions** | Violates resolver or permission gate |
| **Mutation risk** | `none` (blocked before engine) |

**PR-visible checks**

- Clear failure comment: configuration error, permission denied, missing plan, expired token, or lock held
- No transient success markers; no empty plan diff masquerading as success

**Artifact checks**

- No complete mutation manifest; optional partial failure manifest with bounded error

**Step Functions checks**

- Outer or inner ends Failed / IntentFailed without mutation CodeBuild start (when blocked early)

**CodeBuild checks**

- Must not start for blocked commands

**Live-state checks**

- No resource changes in target account

---

## US-06 — CodeBuild console URL click-through / account context

| | |
|---|---|
| **Commands** | `tf apply confirm <token>` or `tf destroy confirm <token>` (mutation lane) |
| **Accounts** | Hub account for CodeBuild console link (`ENGINE_CODEBUILD_ACCOUNT_ID`) |
| **Regions** | Hub region of deployment |
| **Preconditions** | Mutation in progress or terminal |
| **Mutation risk** | `confirmed` |

**PR-visible checks**

- `[CodeBuild job](url)` present on mutation in-progress and terminal comments
- Account note: `hub account \`REPLACE_MAIN_ACCOUNT\`; switch the AWS console to this account first`
- Link uses valid build id format; opens correct project (`openci-tf-codebuild`)

**Artifact checks**

- Engine package submitted; build id resolvable from execution

**Step Functions checks**

- Inner execution reached CodeBuild integration state

**CodeBuild checks**

- Build exists in hub account; logs show `tofu` commands

**Live-state checks**

- Operator can open link after switching IAM Identity Center / console to hub account

---

## US-07 — Step Functions link + execution detail verification

| | |
|---|---|
| **Commands** | Any accepted command (`plan`, `drift`, `report`, `plan_destroy`, apply/destroy flows) |
| **Accounts** | Hub for outer SFN; target for inner folder work |
| **Regions** | Deployment region |
| **Preconditions** | Run accepted and execution started |
| **Mutation risk** | Depends on command (read vs mutation) |

**PR-visible checks**

- `[ci pipeline](url)` or `[Step Functions execution](url)` on in-progress and terminal comments
- CI Details includes commit short SHA and SUCCESS/FAILED
- Execution ID in comment matches registry and S3 prefix

**Artifact checks**

- `manifest.json` `execution_id` matches PR comment execution ID

**Step Functions checks**

- Outer execution ARN reachable; status SUCCEEDED/FAILED matches PR
- Inner per-folder execution linked from outer Map item; PollDone terminal state consistent

**CodeBuild checks**

- When applicable, build id correlates with inner execution id

**Live-state checks**

- `just api-get-run run_id=...` returns matching status and folder outcomes (IAM API smoke)

---

## US-08 — Plan a pipeline in ordered steps

| | |
|---|---|
| **Command** | `tf plan pipeline <name>` (fixture example: `tf plan pipeline smoke/eu-west-1`) |
| **Accounts** | Each folder's configured target account |
| **Regions** | Per-folder |
| **Preconditions** | PR open, non-fork, collaborator write/admin, pinned head SHA, all pipeline folders lock free |
| **Mutation risk** | `none` |

**PR-visible checks**

- One collapsed folder comment per pipeline folder
- Multi-folder summary includes step lines such as `Step 1/N · <folders> · ok`
- If a step fails, later steps are shown as `not run`
- CI Details links to the outer Step Functions execution

**Artifact checks**

- Per-folder manifest and plan artifacts match US-01
- `GET /runs/{run_id}/folders` returns each folder with a 1-based `step_index`

**Step Functions checks**

- Outer execution runs `RunStepFolders` once per step
- A later step does not start until the prior step succeeds

**CodeBuild checks**

- Read-lane engine runs complete for each launched folder

**Live-state checks**

- Optional: `just api-get-run run_id=...` includes `pipeline` and `step_count`

---

## US-09 — Apply a pipeline one confirmed step at a time

| | |
|---|---|
| **Commands** | `tf plan pipeline <name>` then `tf apply pipeline <name>` then `tf apply confirm <token>`; repeat with `tf apply pipeline <name> step <n>` |
| **Accounts** | Target accounts with `enable_apply` + folder `apply.allow: true` |
| **Regions** | Per-folder; Phase B live regions only when human-gated |
| **Preconditions** | Successful fresh plans on same PR head SHA; approval gates satisfied; token within 600s |
| **Mutation risk** | `intent` per step, `confirmed` after each confirm |

**PR-visible checks**

- Step 1 creates a normal apply intent for only step 1 folders
- Successful step comments end with `next: tf apply pipeline <name> step <n+1>`
- Final step ends with `pipeline <name> complete (N steps)`
- A parallel step uses one token and applies its folders serially

**Artifact checks**

- Apply manifests match US-03 and point at the pinned source plan run
- Folder registry rows include the step's `step_index`

**Step Functions checks**

- Each confirmed step is a separate mutation outer execution
- Mutation Map remains `MaxConcurrency = 1`

**CodeBuild checks**

- One CodeBuild run per applied folder; no later step starts without its own confirm

**Live-state checks**

- Human verifies resources after each step before triggering the next one

---

## Summary drift cell and `plan_destroy`

The multi-folder summary table is posted only for `report` and multi-folder runs
(`_should_post_final_summary`). Single-folder `plan_destroy` posts one folder comment
only; the summary table is intentionally omitted. When multiple folders run
`plan_destroy`, the summary drift column reads `destroy.plan.out` via
`_plan_artifact_key(action)` so destroy counts surface correctly.
