# Pipelines

A pipeline is an ordered list of Terraform folders in one repository. Each folder
still owns its own `.openci_tf/config.yaml`. Pipelines only choose folder order; they
are not a workflow engine.

## Define a pipeline

Create `.openci_tf/pipelines/<name>.yaml`. The file path without `.yaml` is the
pipeline name.

```yaml
# .openci_tf/pipelines/data/primary.yaml -> data/primary
steps:
  - folder: infra/vpc
  - parallel:
      - folder: infra/rds
      - folder: infra/ec2
```

A step is either one `folder` or a `parallel` list of folders. The next step waits
for all folders in the current step during read-only runs. `parallel` means
concurrent plan/drift in the read lane. Mutation checkpoints flatten parallel
groups into one deterministic folder order (listed order within each parallel group).

Limits and rejects:

- maximum 20 folders per pipeline
- the same folder cannot appear twice
- nested `pipeline:` references are rejected
- every folder must exist and contain `.openci_tf/config.yaml`
- names may use letters, numbers, `_`, `.`, `/`, and `-`; `all`, absolute paths,
  and `..` are rejected

## Blast radius

Pipelines can affect many folders and accounts. The safeguards below are mandatory:

- preview the whole pipeline before mutation (`tf plan pipeline` / `tf plan --destroy pipeline`)
- one fresh pinned plan and one single-use confirmation token per folder checkpoint
- stop fail-closed on any plan, gate, confirmation, mutation, or registry failure
- destroy runs in deterministic reverse flattened order

## PR comment syntax

- `tf plan pipeline <name>`
- `tf plan --destroy pipeline <name>`
- `tf drift pipeline <name>`
- `tf apply pipeline <name> [step <n>]`
- `tf apply confirm <token>`
- `tf destroy pipeline <name> [step <n>]`
- `tf destroy confirm <token>`

`tf report pipeline <name>` is not supported.

`tf plan pipeline <name>` and `tf plan --destroy pipeline <name>` run Terraform plan
only for every pipeline folder. They skip tfsec and Infracost, render one PR comment
with an ordered plan summary table and collapsible detailed plans per folder (apply
order for plan, reverse order for destroy-plan). Security and cost analysis remain
available on a regular single-folder `tf plan`.

Multi-folder `plan`, `report`, and pipeline read-only runs post a summary table on the
pull request. The third column is **Plan** and shows each folder's plan delta as
`+add ~change -destroy` counts, or `no changes` when the plan is empty. API
`drift` and pipeline drift runs use **Drift Check** instead, with `clean` or `changes`
from the authoritative drift result. Security and Cost columns are unchanged.

Read-only runs (`plan`, `plan --destroy`) validate the whole pipeline,
lock every folder up front, then run step by step. `tf plan --destroy pipeline X`
runs the steps in reverse order. A failed step stops later steps and the summary
marks them `not run`.

## Mutation checkpoints (apply and destroy)

Mutation uses flattened per-folder checkpoints, not YAML step groups. A pipeline
with one folder step followed by a parallel pair has three checkpoints in listed
order.

Apply order:

1. `tf apply pipeline X` starts checkpoint 1 (first folder in apply order).
2. openci-tf resolves a fresh pinned plan for that folder only, publishes it in
   the stable aggregate pipeline PR comment, and posts `tf apply confirm <token>`.
3. After `tf apply confirm <token>` succeeds, the aggregate comment shows the
   result and the next required command before Metadata:
   `tf apply pipeline X step 2` (or completion on the last checkpoint).
4. Each later checkpoint requires a successful apply of the prior checkpoint for the
   same pipeline definition hash, plus a fresh plan created after that prior apply.
5. A plan from the original pipeline preview cannot be reused for a later checkpoint.

Destroy uses the same checkpoint model in reverse flattened order:

1. Preview with `tf plan --destroy pipeline X`.
2. `tf destroy pipeline X` starts at the last folder in destroy order.
3. Each checkpoint gets its own fresh destroy plan and `tf destroy confirm <token>`.
4. Ad hoc multi-folder `tf destroy a,b` is rejected; use `tf destroy pipeline <name>`.

### Machine-readable intent block

Every intent comment (the one carrying the confirmation token) ends with a
fenced JSON block so automation can parse the token instead of scraping prose.
The human-readable text stays; the block is additive:

````markdown
## tf apply intent created

- `infra/vpc`: pinned plan from execution `run-abc`

To proceed within 10 min: `tf apply confirm a1b2c3d4`

```json
{"intent_id": "intent-0011223344556677", "confirm_token": "a1b2c3d4", "expires_at": 1700000600, "pipeline": "data/primary", "step": 2}
```
````

- `intent_id` — non-secret identifier minted with the intent record
- `confirm_token` — the single-use confirmation token (same value as the prose command)
- `expires_at` — epoch seconds when the token expires
- `pipeline` / `step` — the pipeline name and 1-based checkpoint index; both
  `null` for non-pipeline (ad hoc folder) intents

Locks are held only for the current mutation checkpoint. One stable aggregate managed
PR comment is updated in place after each plan publication and mutation result. Separate
command-audit and confirmation comments are preserved.

Deploying a release that changes pipeline checkpoint registry keys leaves in-flight
multi-step pipelines fail-closed: checkpoints recorded under the prior key are not
visible, so the next step is rejected until the pipeline restarts from step 1.

## User-visible errors

Common messages are:

- `unknown pipeline: <name>`
- `invalid pipeline '<name>': <reason>`
- `report is not supported for pipelines`
- `multi-folder destroy is not supported; use tf destroy pipeline <name> for ordered destroy`
- `pipeline step must be an integer >= 1`
- `pipeline <name> step <n> is out of range; step_count=<count>`
- `pipeline <name> step <n> requires a completed apply of step <n-1> first`
- `pipeline <name> step <n> requires a completed destroy of step <n-1> first`
- `pipeline <name> changed since step <n-1> was applied; restart from step 1`
- `pipeline <name> changed since step <n-1> was destroyed; restart from step 1`
- `plan predates prior pipeline checkpoint — create a fresh plan for this folder`
- `folder '<folder>' is locked during pipeline resolution`

## API

`POST /runs` accepts pipeline runs for SigV4 callers:

```json
{
  "trigger_id": "registered-trigger-id",
  "commit_hash": "40-character-pinned-sha",
  "action": "plan",
  "pipeline": "data/primary",
  "idempotency_key": "stable-key-min-8-chars",
  "notification_target": {"type": "registry"}
}
```

`pipeline` is mutually exclusive with `folders`; `folder_mode` may be omitted or
`pipeline`. API callers can start pipeline `plan` and `drift` runs. API `apply`,
`destroy`, `plan_destroy`, and pipeline `report` are rejected.

`GET /runs/{run_id}` includes `pipeline` and `step_count` for pipeline runs.
`GET /runs/{run_id}/folders` includes a 1-based `step_index`; non-pipeline runs
report `step_index: 1` for every folder.

## Out of scope

Pipelines do not support scripts, environment variables, passing outputs between
steps, nested pipelines, per-step refs, read-only resume from a later step,
`report`, or automatic hidden approval between checkpoints.
