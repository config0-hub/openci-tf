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
for all folders in the current step. `parallel` means concurrent plan/drift in the
read lane. Apply for a parallel step uses one confirm token and then applies those
folders serially in the listed order.

Limits and rejects:

- maximum 20 folders per pipeline
- the same folder cannot appear twice
- nested `pipeline:` references are rejected
- every folder must exist and contain `.openci_tf/config.yaml`
- names may use letters, numbers, `_`, `.`, `/`, and `-`; `all`, absolute paths,
  and `..` are rejected

## PR comment syntax

- `tf plan pipeline <name>`
- `tf plan --destroy pipeline <name>`
- `tf drift pipeline <name>`
- `tf apply pipeline <name> [step <n>]`
- `tf apply confirm <token>`

`tf report pipeline <name>` and `tf destroy pipeline <name>` are not supported.

Multi-folder `plan`, `report`, and pipeline read-only runs post a summary table on the
pull request. The third column is **Plan** and shows each folder's plan delta as
`+add ~change -destroy` counts, or `no changes` when the plan is empty. API
`drift` and pipeline drift runs use **Drift Check** instead, with `clean` or `changes`
from the authoritative drift result. Security and Cost columns are unchanged.

Read-only runs (`plan`, `plan --destroy`) validate the whole pipeline,
lock every folder up front, then run step by step. `tf plan --destroy pipeline X`
runs the steps in reverse order. A failed step stops later steps and the summary
marks them `not run`.

Apply is deliberately split into short runs. `tf apply pipeline X` creates the
normal intent for step 1 only, after checking gates for every folder in the
pipeline. Step `n > 1` is accepted only after the run registry shows a
successful apply of step `n-1` for the same trigger, repository, and pipeline.
If the canonical parsed pipeline changed since that prior step, restart from
step 1. Applying step `n` after step `n-1`'s run has aged out of the
run-history retention window (default 90 days, `RUN_HISTORY_RETENTION_DAYS`)
requires restarting the pipeline from step 1, since the step-order anchor is a
run-registry record. For step `n > 1`, the preliminary gate checks only folders
in steps `n` and later. Folders from earlier steps are not re-checked for a
fresh plan because their successful apply already superseded the pipeline plan
for that folder. Step `n` still uses the per-folder plan from the original
pipeline plan run when that folder has not been applied since that plan. After
`tf apply confirm <token>` succeeds, the comment ends with
`next: tf apply pipeline X step 2` until the last step. The final step says
`pipeline X complete (N steps)`. Locks are held only for the current apply step.

## User-visible errors

Common messages are:

- `unknown pipeline: <name>`
- `invalid pipeline '<name>': <reason>`
- `report is not supported for pipelines`
- `destroy pipeline is not supported`
- `pipeline step must be an integer >= 1`
- `pipeline <name> step <n> is out of range; step_count=<count>`
- `pipeline <name> step <n> requires a completed apply of step <n-1> first`
- `pipeline <name> changed since step <n-1> was applied; restart from step 1`
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
`report`, or `destroy`.
