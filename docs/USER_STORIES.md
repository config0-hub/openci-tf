# openci-tf user stories

These stories describe the supported operator workflows and the expected external
behavior. They are product acceptance stories, not records of a specific test run.
Apply and destroy always require human confirmation through the gated token flow.

| Field | Meaning |
|-------|---------|
| Mutation risk | `none` = read-only lane; `intent` = posts token only; `confirmed` = poweruser after confirm |
| PR-visible | What appears in the GitHub PR comment thread |
| Artifacts | S3 manifest keys under `openci-tf/<repo>/<execution_id>/<folder>/` |
| Step Functions | Outer and inner execution state |
| CodeBuild | Engine build when the mutation lane or configured folder execution uses it |

## US-01 — Plan one folder

| | |
|---|---|
| **Command** | `tf plan <folder>` |
| **Accounts** | Folder `account_alias` target using the readonly executor |
| **Preconditions** | PR open, non-fork, collaborator write/admin, pinned head SHA, folder lock free |
| **Mutation risk** | `none` |

Expected behavior:

- A collapsed folder comment is posted with folder, account, SHA, and plan status.
- Initialize, Validate, Plan, Security Scan, and Cost Analysis sections are shown
  when their artifacts are available.
- Plan output includes add/change/destroy counts and a bounded diff block.
- CI details link to the Step Functions execution.
- Artifacts include `manifest.json`, `init.out`, `validate.out`, `tf/plan.out`,
  and, for successful plans, the pinned binary plan plus checksum metadata.

## US-02 — Run all read-only folders

| | |
|---|---|
| **Command** | `tf report` or `tf plan <folder1>,<folder2>,...` |
| **Accounts** | Every discovered folder's configured target account (for `tf report`) or each listed folder's account |
| **Preconditions** | Same as US-01; `tf report` resolves all configured folders |
| **Mutation risk** | `none` |

Expected behavior:

- One folder comment is posted per folder.
- A final summary table is posted after folder comments complete.
- Folder rows show drift, security, and cost outcomes only from authoritative
  artifacts.
- The outer Step Functions execution succeeds only when every required folder
  execution succeeds.

## US-03 — Apply one folder after plan and confirmation

| | |
|---|---|
| **Commands** | `tf plan <folder>` → `tf apply <folder>` → `tf apply confirm <token>` |
| **Accounts** | Target with `enable_apply`, folder `apply.allow: true`, and a poweruser role |
| **Preconditions** | Successful plan on the same PR head SHA; approved review if required; token within TTL |
| **Mutation risk** | `intent` before confirmation; `confirmed` after confirmation |

Expected behavior:

- The plan run creates the pinned `tf/plan.tfplan` artifact.
- `tf apply <folder>` creates an intent and posts a single-use confirmation token.
- Confirmation rechecks token, PR head SHA, and the pinned plan before starting
  the apply lane.
- The apply lane waits the configured grace period, runs CodeBuild only, executes
  `tofu show` against the pinned plan, then applies that exact plan.
- Terminal comments show success or failure, CodeBuild link, source plan ID, and
  bounded command output.

## US-04 — Destroy one folder after destroy plan and confirmation

| | |
|---|---|
| **Commands** | `tf plan --destroy <folder>` → `tf destroy <folder>` → `tf destroy confirm <token>` |
| **Accounts** | Same gates as apply, with `destroy.allow: true` |
| **Preconditions** | Successful `plan_destroy` on the same PR head SHA; token within TTL |
| **Mutation risk** | `intent` before confirmation; `confirmed` after confirmation |

Expected behavior:

- The destroy plan writes `tf/destroy.plan.tfplan` and destroy-plan metadata.
- Destroy intent creation refuses stale, missing, or mismatched plans.
- Confirmation starts the destroy lane, which uses CodeBuild only and applies the
  pinned destroy plan.
- Destroy output is bounded and attached to the terminal PR comment.

## US-05 — Block invalid or unauthorized commands

| | |
|---|---|
| **Commands** | Examples: apply without plan, fork PR comment, non-collaborator command, bad token, disallowed folder |
| **Preconditions** | A resolver, permission, token, lock, or configuration gate fails |
| **Mutation risk** | `none` |

Expected behavior:

- A clear failure comment explains the gate that failed.
- No mutation CodeBuild job starts for blocked commands.
- No success marker or complete mutation manifest is written.

## US-06 — Show execution links and account context

| | |
|---|---|
| **Commands** | Any accepted command |
| **Accounts** | Hub account for outer executions and CodeBuild; target account for folder credentials |
| **Mutation risk** | Depends on command |

Expected behavior:

- In-progress and terminal comments link to the relevant Step Functions execution.
- Mutation comments link to the CodeBuild build when available.
- Comments identify the account context needed to open AWS console links.
- Registry rows, manifest paths, and PR comments agree on execution ID.

## US-07 — Plan a pipeline in ordered steps

| | |
|---|---|
| **Command** | `tf plan pipeline <name>` |
| **Accounts** | Each step's configured folder target account |
| **Preconditions** | PR open, non-fork, collaborator write/admin, pinned head SHA, all pipeline folders lock free |
| **Mutation risk** | `none` |

Expected behavior:

- The outer execution runs pipeline steps in order.
- A later step does not start until the prior step succeeds.
- Folder registry rows include a 1-based `step_index`.
- If a step fails, later steps are shown as not run.

## US-08 — Apply a pipeline one confirmed step at a time

| | |
|---|---|
| **Commands** | `tf plan pipeline <name>` → `tf apply pipeline <name>` → `tf apply confirm <token>`; repeat per step |
| **Accounts** | Target accounts with `enable_apply`, folder `apply.allow: true`, and poweruser roles |
| **Preconditions** | Fresh successful plans on the same PR head SHA; approval gates satisfied; token within TTL |
| **Mutation risk** | `intent` per step; `confirmed` after each confirmation |

Expected behavior:

- Each step creates its own intent and confirmation token.
- A successful step comment names the next step command when another step remains.
- No later step starts without its own confirmation.
- The final step reports pipeline completion.

## Summary table rules

The multi-folder summary table is posted only for `report` and multi-folder runs.
Single-folder `plan_destroy` posts one folder comment only. Multi-folder destroy
plans derive their summary counts from `destroy.plan.out`.

## Report presentation

`tf report` posts two managed comment types:

- **Summary (`report-all`)** — `## openci-tf report` with folder counts, a
  needs-attention table (Drift and Security icon cells only; Cost shows amounts),
  and a collapsed clean-folder section.
- **Per-folder (`report`)** — compact outer summary (`<folder> · Drift · Security`),
  then blockquote-indented child sections in order: Setup, Plan, Security, Cost,
  Execution, Artifacts.

Security and Cost expanded bodies show native `tfsec.output` and `infracost.output`
artifacts. The Artifacts section lists only objects that exist for the run, linked
through authenticated AWS Console shortcuts when Identity Center is configured.
Execution shows the Step Functions link when present; CodeBuild links appear only
for apply/destroy comments.
