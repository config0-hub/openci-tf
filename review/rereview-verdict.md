VERDICT: 13/0/1 of 14

| Finding | Score | One-line evidence |
|---|---|---|
| B1 — API accepts `plan_destroy` | resolved | `parse_run_request` now allows API ingress only for `plan`/`drift`/`report`, caller-policy loading rejects unsupported actions, and handler tests prove `plan_destroy`/`apply`/`destroy` return 400 before authorization or orchestration. |
| M1 — unverified token can see cached protected data | resolved | `AuthGate` and `AppShell` clear the `QueryClient` on sign-out, token replacement, and 401 rejection, while protected children render only after the generation-keyed authorization probe succeeds. |
| M2 — Accounts invents blank ARN fields | resolved | `AccountTarget` now contains the documented `role_name`, `AccountRow` renders only `ROLE NAME`, and the rendered contract test rejects the obsolete ARN labels. |
| M3 — artifact presence is shown as successful completion | resolved | Step evidence now maps manifest presence to `FILED`/`NOT FILED` and no longer guesses a failed or in-progress step when authoritative per-step outcomes are absent. |
| M4 — admin pagination is discarded | resolved | Repos, Accounts, and Locks carry/send cursors, persist bounded previous-page history in typed search state, expose next/previous controls, and describe an empty cursored lock page as only a bounded slice. |
| M5 — Runs cannot list/filter across repositories | resolved | `GET /runs` now merges only policy-authorized trigger partitions with server-side action/repo filtering and one creation-key cursor; the real Runs query is no longer disabled or repo-filtered page-locally. |
| M6 — real drift runs cannot render DRIFT | resolved | Checksum-bound `drift.json` booleans are persisted on folder/run registry rows, failure takes precedence, and rendered tests cover detected, clean, legacy-unknown, and failed drift results. |
| M7 — Gates falsely reports no folder opt-ins | resolved | Validation now stores latest SHA-bound folder-gate observations and `/gates` returns their provenance; an empty page is rendered as unavailable and explicitly not an opt-out. |
| M8 — detail polling refetches every manifest | unresolved | `RunDetail` still polls the monolithic `getRunDetail` every five seconds, and that function still launches an unbounded `Promise.all` manifest fetch for every manifest-bearing folder. |
| N1 — active run says terminal registry state | resolved | Active details now say `LATEST REGISTRY STATE`; only non-active states receive the `TERMINAL REGISTRY STATE` label. |
| N2 — expiry trusts the browser clock | resolved | API responses derive a clock offset from the HTTP `Date`; runs, locks, and plan expiry use that offset, retain absolute UTC expiry, and decline a decision when server time is unavailable. |
| N3 — unknown routes use generic not-found text | resolved | The root route now supplies `NotFoundProcedure`, which renders the abnormal-procedure treatment and a working return-to-Runs link. |
| N4 — tests do not pin sensitive contracts | resolved | New tests cover adapter secret allowlists, all four admin authorization gates and route variants, limit/cursor/error behavior, run-list scope, and rendered account/gate/drift/pagination contracts. |
| N5 — create leaves Runs cache stale | resolved | Successful creation invalidates the entire `['runs']` query family, removes any stale new-run detail, and then navigates to authoritative detail. |

## Unresolved detail

### M8 — manifest request storm remains reproducible

The polled query in `frontend/src/RunDetail.tsx` still calls `getRunDetail(runId)` as one unit. `frontend/src/api.ts` fetches the run, fetches all folder rows, and then executes `Promise.all` over every row with `manifest_s3_uri`. Static reproduction against the current source confirms that a 100-folder in-progress run still performs 102 requests per poll (run + folders + 100 manifests), repeats the 100 immutable manifest reads every five seconds, and imposes no manifest concurrency bound. Separate digest-keyed immutable manifest queries and registry-only polling were not implemented.

## New regression

### NEW-M1 (major) — selective cross-trigger filtering can perform unbounded DynamoDB reads

`src/platform/aws/run_registry.py::_list_partition_matches` keeps querying a trigger partition until it accumulates the requested number of post-filter matches or exhausts the partition; there is no evaluated-row/page budget. A read-only fake-table reproduction with 40 nonmatching pages and `limit=1` returned zero rows only after 40 DynamoDB queries. Real high-volume 90-day partitions, multiple authorized triggers, or the authorization probe can therefore turn one console request into an unbounded scan and Lambda timeout/cost spike. Bound evaluated pages/items and return an evaluated-boundary cursor—even for an empty filtered page—so the frontend can continue truthfully.

## Checks

- `cd frontend && npm test` — 25 passed (run twice).
- `cd frontend && npm run typecheck` — passed.
- `cd frontend && npm run build` — client and server builds passed.
- `uv run --with pytest --with boto3 python -m pytest tests/unit/test_api_control_plane.py -q` — 47 passed (run twice).
- Related admin/run-list/drift/gate/run-folder tests — 65 passed.
