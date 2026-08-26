# openci-tf Core API

AWS IAM-authenticated HTTP routes on the existing API Gateway stage complement the public GitHub webhook route.

## Routes

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/runs` | AWS IAM | Create async run (202 + `run_id`) |
| `GET` | `/runs?trigger_id=&repo=` | AWS IAM | List recent authorized runs (bounded pagination) |
| `GET` | `/runs/{run_id}` | AWS IAM | Get one run |
| `GET` | `/runs/{run_id}/folders` | AWS IAM | List folder executions |
| `GET` | `/runs/{run_id}/folders/{folder}/manifest` | AWS IAM | Manifest JSON |
| `GET` | `/runs/{run_id}/folders/{folder}/artifacts?name=` | AWS IAM | Bounded inline artifact or presigned plan URL |
| `GET` | `/repos` | AWS IAM | List repository registrations |
| `GET` | `/accounts` | AWS IAM | List stored account targets |
| `GET` | `/locks` | AWS IAM | List active folder locks |
| `GET` | `/gates` | AWS IAM | Read installation and repository-config gate sources |

GitHub webhook ingress remains `POST /webhook/{trigger_id}` with HMAC verification.
All four admin reference routes require the caller policy's `admin` read class in
addition to API Gateway IAM authorization. The routes are read-only.

## Create run body

Explicit folder run:

```json
{
  "trigger_id": "registered-trigger-id",
  "commit_hash": "40-character-pinned-sha",
  "action": "plan",
  "folder_mode": "explicit",
  "folders": ["infra/example"],
  "idempotency_key": "stable-key-min-8-chars",
  "notification_target": {"type": "registry"}
}
```

Pipeline run:

```json
{
  "trigger_id": "registered-trigger-id",
  "commit_hash": "40-character-pinned-sha",
  "action": "drift",
  "pipeline": "data/primary",
  "idempotency_key": "stable-key-min-8-chars",
  "notification_target": {"type": "registry"}
}
```

`folder_mode` is `all` or `explicit` for service/API callers. For pipeline runs,
`pipeline` is mutually exclusive with `folders`; `folder_mode` may be omitted or
set to `pipeline`. API callers can create `plan`, `drift`, and `report` runs, but
`report` is not supported for pipelines. GitHub webhook ingress uses `explicit`
folder selection from `tf plan <folder-or-csv>` and
`tf plan --destroy <folder-or-csv>`, `all` from bare `tf report`, and `pipeline`
from `tf plan pipeline <name>`, `tf plan --destroy pipeline <name>`, and
`tf drift pipeline <name>`. Folder-targeted or bare `tf drift` is only available
through the API. Every accepted or rejected `tf` comment is recorded in the
durable audit comment described in [docs/GITHUB_WEBHOOK.md](GITHUB_WEBHOOK.md).

## Retention

Run-registry items carry `expire_ttl` epoch seconds. Default retention is **90 days** (installer-configurable via `RUN_HISTORY_RETENTION_DAYS` on Lambdas). DynamoDB TTL deletes asynchronously; API reads hide expired items when `expire_ttl <= now`.

## Example (SigV4)

```bash
just api-create-run registered-trigger-id 0123456789abcdef0123456789abcdef01234567 run-key-1 infra/example
just api-create-run registered-trigger-id 0123456789abcdef0123456789abcdef01234567 run-key-2 "" data/primary drift
just api-get-run run-123
```

Use `just api-list-runs registered-trigger-id` for bounded history queries. The
`api-create-run` positional arguments are `trigger_id sha idempotency_key [folder]
[pipeline] [action]`; use an empty folder argument (`""`) before `pipeline`.

`GET /runs/{run_id}` returns the stored run record. Pipeline runs include
`pipeline` and, after pinned checkout resolution, `step_count`. Non-pipeline runs
omit both fields. `GET /runs/{run_id}/folders` returns one row per folder with a
1-based `step_index`; non-pipeline folder rows report `step_index: 1`.

`GET /runs` accepts an optional `trigger_id`, optional case-insensitive `repo`
substring filter, `limit`, and `cursor`. When `trigger_id` is omitted, the API
merges only the trigger partitions named in the verified caller policy. Action
filtering also happens server-side from that policy. Results are globally ordered
newest-first by the immutable registry creation key; `cursor` is one stable
boundary across that merged result space. Page size defaults to 25 and is clamped
to 1–100. Invalid cursors are rejected before DynamoDB I/O.

## Drift result

Run and folder registry responses can include `drift_detected: true|false` for
`drift` procedures. Collect reads and strictly validates the SHA-bound
`drift.json` object; it does not infer drift from the requested action. A folder
field is absent when no authoritative drift artifact was observed. The run field
is `true` when any folder authoritatively detected drift, `false` only when every
folder has an authoritative false result, and absent when the aggregate is
unknown. Consumers must treat an absent field as an unknown drift result.

On pull requests, multi-folder summary tables use a **Plan** column for `plan`,
`report`, and `plan_destroy` runs. Cells show `+add ~change -destroy` counts or
`no changes`. Only `drift` runs use a **Drift Check** column with `clean` or
`changes`. Do not read plan delta counts as drift detection.

Terminal execution status has precedence over the drift result: a failed or
`infrastructure_error` run remains failed even if one completed folder recorded
`drift_detected: true`. Consumers should classify failure first, then drift, then
ordinary success.

## Admin reference reads

`GET /repos`, `GET /accounts`, `GET /locks`, and `GET /gates` accept `limit`
and `cursor` using the same bounded convention as `GET /runs`. Page size
defaults to 25 and is clamped to 1–100. A response includes `cursor` only when
another DynamoDB page exists.

`GET /repos` returns one truthful projection per stored `pk=repo` registration
row. `trigger_ids` is therefore a singleton list for each row; the API does not
merge rows that could carry different approval policies:

```json
{"repos":[{"repo_name":"org/repo","trigger_ids":["repo-prod"],"require_approval":true}]}
```

`GET /accounts` exposes the account alias, account ID, and the role name that is
actually stored in each `pk=account` row:

```json
{"accounts":[{"alias":"production","account_id":"123456789012","role_name":"openci-tf-executor-readonly"}]}
```

Account rows do not store resolved hub or target role ARNs, so the API does not
fabricate them. It also deliberately omits stored `external_id` and optional
session TTL metadata because those authentication details are not part of the
console contract.

`GET /locks` reads the existing TTL table and returns only rows whose
`expires_at` epoch seconds are greater than the server's current time. DynamoDB
TTL deletion is asynchronous, so the query filters expiry and the adapter
checks it again before returning data:

```json
{"locks":[{"repo_name":"org/repo","folder":"infra/vpc","holder_execution_id":"run.abc.0","expires_at":1700000300}]}
```

`GET /gates` reads `ENABLE_APPLY`, the same runtime source used by mutation gate
checks. The apply/destroy gate itself reads each pinned checkout's
`.openci_tf/config.yaml` at PR intent time. Because there is no authoritative
central copy of those files, validation records a bounded projection of each
folder's parsed `apply` and `destroy` flags whenever a run observes a pinned
commit. One latest-observed row per repository/folder is retained in the run
registry:

```json
{
  "enable_apply": false,
  "folders_source": "latest-run-observation",
  "folders": [{
    "repo_name": "org/repo",
    "folder": "infra/vpc",
    "trigger_id": "repo-prod",
    "run_id": "run-123",
    "source_sha": "0123456789abcdef0123456789abcdef01234567",
    "apply": true,
    "destroy": false,
    "observed_at": 1700000000
  }]
}
```

`folders_source: "latest-run-observation"` means exactly that: each row is the
newest pinned configuration this installation observed while validating a run,
not a claim about the repository's current branch contents. `source_sha`,
`run_id`, and `observed_at` identify where the row came from. Rows use run-history retention;
a folder with no retained observation is unavailable/unknown. An empty
`folders` page therefore never means that every folder opted out, and an absent
folder must not be rendered as `apply: false` or `destroy: false`.

## Caller policy

`api_caller_policy_json` remains keyed by canonical IAM role ARN. Existing run
permissions use `trigger_ids`, `actions`, `artifact_classes`, and
`binary_plan`. The optional `read_classes` list defaults to no admin access;
add `"admin"` to grant all four installation-wide, read-only reference routes.
The API rejects unknown read classes and checks this policy before reading a
table or runtime gate.

## Future apply contract (disabled)

API verbs remain `plan`, `drift`, and `report` only; PR apply and destroy use
the gated flow in [docs/APPLY.md](APPLY.md). A future human-controlled apply outside this API would need the exact `plan.tfplan` S3 URI from the manifest plus matching metadata, checksum, expiry, repository, pinned SHA, account, folder, attempt, and runtime verification before using the binary.
