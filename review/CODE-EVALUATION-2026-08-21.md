# openci-tf code evaluation — flow, dead code, structure, and cleanup plan

> **Execution status (2026-08-22):** Phases 1, 2, and 4 are implemented as local
> commits on `dev` (phase 1 hygiene; phase 2 correctness incl. registry-key
> consolidation into `src/core/registry_schema.py`, fail-loud fixes, frontend
> mock removed from the prod bundle, import-direction ratchet; phase 4 module
> splits — note the tfsec module landed as `tfsec_findings.py` because a guard
> test blocklists the legacy name `tfsec.py`). The full Docker suite matches
> the pre-change baseline after every phase (32 pre-existing failures, none
> new; baseline recorded before any change). **Phase 3 remains open — it needs
> human decisions** (fate of `test_folders_handler.py`, wiring `tests/install/`,
> the three tests-only modules). One layering note: consolidating the key
> layout forced the pure key builders down into `src/core/registry_schema.py`
> so platform code imports downward; `domain/run/{registry_schema,folder_key}.py`
> are now thin re-export views plus the domain-dependent `manifest_key`.

Date: 2026-08-21. Scope: `src/` (~17,400 lines Python), `infra/`, `frontend/`, `docker/`, `scripts/`, `tests/`.
Standards baseline: `../jiffy-rewrite-2026` (layered import DAG, fail-loud error handling, deep modules
with narrow interfaces, no stubs/dead code, no mock paths in production bundles).

## Verdict

The codebase is in good shape — better than most generated code. Import layering is genuinely clean
(zero `domain→services` or `platform→domain` edges), exception handling is almost uniformly narrow
(one `except Exception` in the whole tree, and it re-raises), and Lambda entrypoints follow one
uniform `def handler(event, context)` convention wired from `infra/`. The problems are localized:
a handful of grab-bag modules that grew too large, ~2 fully dead modules plus ~19 dead symbols, one
divergent duplicated utility that is an active correctness risk, and a 206 MB legacy snapshot tracked
in git. Nothing found requires an architectural rework.

## 1. Execution flow (ASCII tree)

### Entry A — GitHub webhook (primary path)

```
POST /webhook  (API Gateway — infra/deploy/modules/openci_tf/api_gateway.tf)
└── Lambda: src/services/webhook/handler.py:52  handler()
    ├── src/services/webhook/validate.py:9          verify_signature()      HMAC check
    ├── src/services/webhook/parse_event.py         parse_github_event()    normalize payload
    ├── src/domain/command/grammar.py               parse "plan|apply|..." comment
    ├── src/domain/authorization.py                 collaborator write/admin gate
    ├── src/services/webhook/run_request.py:14      github_run_request()    build RunRequest
    └── src/services/orchestration/start_run.py:117 start_run_from_request()
        └── StartExecution → OUTER STATE MACHINE (idempotent, deterministic exec name)
        [platform: aws/ssm.py, aws/dynamo.py, github/client.py; core/logging.py]
```

### Entry B — Console API

```
API Gateway routes
└── Lambda: src/services/api/handler.py:633  handler()   (route table :635-653)
    ├── POST /runs                  _create_run:133  → orchestration/start_run.py (same as webhook)
    ├── GET  /runs, /runs/{id}      _list_runs:165, _get_run:151        → platform/aws/run_registry.py
    ├── GET  .../folders            _list_folders:287
    ├── GET  .../manifest           _get_manifest:380                   → domain/engine/manifest.py
    ├── GET  .../artifacts          _get_artifact:464  (presign + URI confinement :306-441)
    ├── GET  /repos /accounts /locks                                    → platform/aws/admin_registry.py
    └── GET  /gates                 _get_gates:260                      → domain/intent/gates.py
```

### Outer state machine  (infra/deploy/modules/openci_tf/step_function.tf, + mutation variant)

```
ParseCommand            → src/services/resolve/handler.py:17            re-parse grammar, routing flags
RouteAction (Choice)
├── CreateIntent        → src/services/intent/handler.py:31             apply/destroy step 1
│                          └── services/intent/create.py, domain/intent/{models,token}.py
├── ConfirmIntent       → src/services/intent/handler.py:85             apply/destroy step 2
│                          └── services/intent/confirm.py, domain/intent/plan_lookup.py
ValidateAndResolve      → src/services/resolve/validate_and_resolve.py:181
│                          ├── platform/git/{clone,origin,package}.py   clone + package upload
│                          ├── domain/config/outer_state.py:51          folder/global config resolve
│                          ├── domain/command/affected_folders.py       changed paths → folders
│                          ├── domain/accounts/{aliases,binding,budget}.py
│                          ├── domain/locks/run_lock.py                 per repo+folder locks
│                          └── domain/engine/outer_map_state.py         map items + size budget
RenderPlaceholder       → src/services/render/handler.py:918            (placeholder modes)
RunFolders (Map)        → startExecution.sync on INNER STATE MACHINE (below)
│   NormalizeFolderOutcome → render handler (normalize mode)
RenderPR                → src/services/render/handler.py:918            full render
│                          ├── domain/formatters/{artifacts,infracost_table}.py
│                          ├── domain/engine/{summary,pointer_publish,plan_artifacts}.py
│                          └── platform/github/client.py               PR comments
FinalizeRun[Failed]     → src/services/orchestration/finalize_run.py:158
                           └── domain/locks/run_lock.py (release), domain/run/outcome.py,
                               platform/aws/run_registry.py (terminal write)
```

### Inner run-folder state machine  (infra/deploy/modules/run_folder/step_function.tf)

```
ValidateAction (Choice)
PrepareAndSubmit        → src/services/run_folder/prepare_and_submit.py:546
│                          ├── domain/accounts/target_session.py + platform/aws/sts.py   mint creds
│                          ├── domain/ssm_env/resolve.py + platform/aws/sops.py          secrets.enc.json
│                          ├── domain/cmd_builder/{cmd_resolver,installers,script_generator}.py
│                          ├── domain/engine/{prepare,payload,presign,run_artifact_layout}.py
│                          └── platform/aws/engine.py                                    invoke engine
ProbeDone               → src/services/run_folder/poll_done.py:202
│                          └── platform/aws/{engine,s3}.py, domain/engine/result.py
RouteProbeOutcome (Choice)
├── running             → WaitBeforeProbe → ProbeDone
├── CredentialExpired   → src/services/run_folder/persist_retry_attempt.py:114 → PrepareAndSubmit
├── done                → src/services/run_folder/collect.py:226
│                          └── domain/engine/{manifest,summary,pointer_publish}.py, aws/{s3,run_registry}
└── failure             → src/services/run_folder/write_failure_manifest.py:252 → Fail state
```

### Frontend console (TypeScript)

```
frontend/server/lambda.ts  (deployed)  ─┐
frontend/server/local.ts   (dev)       ─┴→ app.ts:52 createConsoleApp()
    ├── auth middleware :66            bearer token via token.ts (SSM)
    ├── /api/* :74 → proxy.ts:24       SigV4 (aws4fetch) → Python API Gateway
    ├── SPA static fallback :77
    └── mock.ts/mock-data.ts           ⚠ bundled into prod, gated only by env var (see §3)
```

## 2. Dead code and stubs (verified)

Confirmed dead (safe to delete):

| Item | Evidence |
|---|---|
| `src/services/render/timeout_handler.py` (58 ln) | Docstring claims Lambda `openci-tf-render-timeout`; no reference anywhere in `infra/`, `src/`, `scripts/`, `justfile`. Verified by direct grep. |
| `src/platform/aws/cost_query.py` (74 ln) | Zero references repo-wide; belongs to the disabled cost feature. |
| `src/platform/github/client.py:223 verify_webhook_signature` | Zero references; duplicates (and is *weaker than* — no `sha256=` prefix guard) the live `services/webhook/validate.py:9`. Security-adjacent: delete so nobody imports the wrong one. |
| `src/domain/config/settings.py` | No src or test callers of `load_settings`; also freezes `SETTINGS_TABLE_NAME` at import time. |
| `src/core/serialization.py` (dead trio of b64 helpers) + ~15 more dead symbols | Unreferenced: `ActionDisabledError`, `AuthorizationError`, `ChecksumMismatchError` (core/errors.py); `object_sha256`, `put_json`, `get_json`, `list_object_keys` (aws/s3.py); `new_run_id`, `create_run_record`, `list_folder_attempts` (run_registry.py); `read_file_from_clone` (git/clone.py); `folder_sk` (registry_schema.py); `IntentConfirmError` (intent/confirm.py); `load_global_config`, `load_folder_config` (callers bypass them for `parse_*`). |
| `docker/engine_ref/payload.py.placeholder` | Real `payload.py` is tracked; placeholder has no consumer. |
| `scripts/test_console_config.sh` | Zero references repo-wide. |
| `job_builder.py:113,118` NotImplementedError stubs | `build_pipeline_payload` / `build_cost_payload` — "disabled pending v3 migration", zero callers. Jiffy standard: no stale stubs. |

Test-only modules — decide, don't blind-delete:

- `src/domain/engine/inner_run_folder_state.py` — Python model of the inner ASL, asserted against the rendered Terraform by `test_rendered_asl_remediation.py`. This is a deliberate spec oracle. **Keep**, but move the model into the test package (or comment its role) so it stops looking like unwired runtime code.
- `src/domain/command/validator.py`, `src/platform/github/capability_verifier.py` (366 ln), `src/domain/cmd_builder/job_builder.py` — only tests import them. `capability_verifier` may be invoked by an install script; confirm, then keep or drop.
- `src/services/render/test_folders_handler.py` — production-looking code, `test_` name (pytest has collected it — stale `.pyc` proves it), docstring names a Lambda that does not exist in `lambdas.tf` (verified). Delete, or rename to `resolved_folders_preview.py` and wire it.

False positive worth recording: `cmd_resolver.resolve_plan/…/resolve_destroy` look dead to grep but are live via the `_RESOLVERS` string-dispatch table (`cmd_resolver.py:27-32`). Do **not** delete.

Not code, but weight: `.original/` is 206 MB tracked in git (mostly old `.zip` artifacts), zero imports from `src/` (three docstring provenance mentions only). Committed `__pycache__/` dirs also litter `src/`.

## 3. Correctness / risk findings (fix regardless of cleanup)

1. **Divergent duplicate registry key layouts.** `platform/aws/registry_keys.py` and `domain/run/registry_schema.py` both define `run_pk`, `folder_gate_pk/sk`, `terminal_rank`, etc. They diverge: `registry_keys.py:11 _folder_opaque_key` omits the control-character rejection and 192-byte length check that `domain/run/folder_key.py:10 normalize_folder_path` enforces. `orchestration/start_run.py:13,16` imports from **both**. Consolidate on `registry_schema` (the majority, domain-side copy) and delete `registry_keys.py`.
2. **Frontend mock API ships in the production Lambda.** `app.ts:6` imports `mock.ts` unconditionally; `package-lambda.sh` bundles it; the only guard is `CONSOLE_MOCK_API === "1"` at runtime (`app.ts:70,106`), which also skips the SSM token load. Anyone who can set one env var turns the authenticated console into fixtures. Move mock into a dev-only entry (`local.ts` composition) so `build:server` never bundles it.
3. **Stale Dockerfile CMD.** `docker/Dockerfile:30` is `CMD ["src.webhook.handler.handler"]` — a module path that no longer exists (real: `src.services.webhook.handler.handler`). Masked in prod because every Lambda overrides `image_config.command`; any bare `docker run` fails at import.
4. **`tests/install/` (11 shell suites) never runs.** Not referenced by `justfile` or either GitHub workflow; CI runs pytest only. Two `scripts/*.sh` are referenced *only* by these unrun tests. Wire them into a `just` recipe / CI job, or mark them explicitly manual.
5. **Silent-default soft spots** (vs. the jiffy fail-loud rule):
   - `run_registry.py:73-75` — malformed `RUN_HISTORY_RETENTION_DAYS` silently falls back to default TTL. Raise on garbage.
   - `webhook/parse_event.py:75-78` swallows `InvalidInvocationIdentityError` that `webhook/handler.py:95` elsewhere treats as a hard 400 — two policies for one error in one service.
   - `formatters/artifacts.py:539-550` — on `JSONDecodeError` sets `data = {}` then still renders the cost table from the same bad text; three inconsistent "cost unavailable" fallbacks across the formatter.
6. **`platform/git/package.py:91-95`** — `validate_reserved_package_names` is a `for … : pass` loop that validates purely via generator side effects. Live and correct, but reads exactly like a stub; a comment is mandatory so nobody "cleans it up".

## 4. Structure vs. jiffy standards

Good — keep as-is: strict downward imports (measured: no upward edges at all); one narrow `except Exception` repo-wide; uniform handler convention; small single-purpose domain modules (median < 150 lines); `src/core` is a real shared kernel (imports nothing, used by 25+ files), not a utils dump.

Gaps:

- **Import-direction test has a hole.** `tests/unit/test_import_direction.py:5` orders layers `core=0, platform=1, domain=2, services=3`, so `domain→platform` is legal — and 6 such edges exist (`locks/run_lock.py:11`, `config/settings.py:7`, `intent/plan_lookup.py:23-24`, `accounts/aliases.py:7`, `engine/run_artifact_layout.py:17`). Jiffy's rule is that domain has no I/O. Fix by ratcheting: pin the current 6 edges as an explicit allowlist in the test ("do not add"), then shrink it — `run_artifact_layout.py` is the cheapest (inject the registry lookup as a callable; the codebase already does this in `manifest.py:735`). The test also only sees `src.`-prefixed absolute imports; add a guard against relative imports.
- **Five oversized grab-bags** (jiffy governs by interface width, and these fail that test):
  - `services/render/handler.py` (1063 ln, 41 functions, 4 concerns) → split into `render/comments.py` (GitHub comment lifecycle), `render/artifacts.py` (S3 plumbing), `render/registry_update.py` (`_terminal_status:692`–`_update_run_registry:728`), leaving handler + render modes (~450 ln). Highest-value split.
  - `platform/aws/run_registry.py` (1026 ln) → package `run_registry/{runs,folders,queries}.py` with a re-exporting `__init__.py` — zero churn at its 55 call sites.
  - `domain/engine/manifest.py` (1102 ln) — cohesive topic, wrong shape: `build_manifest:728` is 306 lines with **26 kwargs**. Move the constant tables (:40-200) to `manifest_schema.py`; fold the kwargs into two structs (`ManifestBinding`, `BucketSet` — `FolderArtifactKeys` already sets the pattern).
  - `domain/formatters/artifacts.py` (887 ln) — clean seam at `:765`: everything below is markdown truncation/balancing → `formatters/comment_bounds.py`; the tfsec cluster → `formatters/tfsec.py` (consistent with the existing `infracost_table.py`).
  - `services/run_folder/prepare_and_submit.py` (812 ln) → extract `run_folder/notify.py` (`_record_notification:434`…`_notify_after_acceptance:477`) and `run_folder/secrets.py` (`_plan_artifact_secrets:184`…`_infracost_secret:325`).
  - `services/api/handler.py` (655 ln) is fine except: move the presign/URI-confinement helpers (:306-441) to `api/artifact_access.py` so the security surface is reviewable in isolation.
- **Placement/naming:** `domain/aws/console_urls.py` is pure string-building (correctly domain) but the dir name collides with `platform/aws` — move to `domain/formatters/console_urls.py` (6 call sites). Seven packages missing `__init__.py` (`core`, `domain/aws`, `domain/github`, `domain/run`, `services/api`, `services/intent`, `services/orchestration`) — mixed namespace/regular packages are a Lambda-packaging hazard; add empty files.
- **Logging:** only 6 of ~14 handlers use `core/logging.get_logger`; the biggest ones (api, render, prepare_and_submit) log nothing structured. Additive fix.

## 5. Cleanup plan (ordered, low-risk first)

Each phase is independently shippable; run `just` test recipes (full pytest in Docker) after each.

**Phase 1 — zero-behavior-change hygiene (½ day)**
1. Fix `docker/Dockerfile:30` handler path.
2. Add the 7 missing `__init__.py`; gitignore + remove committed `__pycache__`.
3. Delete confirmed-dead: `timeout_handler.py`, `cost_query.py`, `settings.py`, `serialization.py`, `verify_webhook_signature`, the 2 NotImplementedError stubs, `payload.py.placeholder`, `scripts/test_console_config.sh`, and the ~15 dead symbols (keep `cmd_resolver.resolve_*` — live via dispatch). Delete each symbol's dead tests with it.
4. Add the "validates via generator side effects" comment at `git/package.py:91`.
5. `git rm -r .original/` (history rewrite via filter-repo is a separate, human-gated decision).

**Phase 2 — correctness (1 day)**
6. Consolidate registry keys on `domain/run/registry_schema.py`; delete `platform/aws/registry_keys.py`; ensure the stricter `normalize_folder_path` is the single hashing path. This has real behavior implications for hostile input — keep the existing key-format tests green (digests are identical for valid input).
7. Fail loud on malformed `RUN_HISTORY_RETENTION_DAYS`; reconcile the `InvalidInvocationIdentityError` double-policy; unify the three infracost "unavailable" fallbacks.
8. Frontend: move mock wiring out of `app.ts` into the local dev entry so prod bundles exclude it.
9. Ratchet `test_import_direction.py` (domain→platform allowlist of 6 + relative-import guard).

**Phase 3 — wiring decisions (needs your call, ½ day)**
10. `test_folders_handler.py`: delete or rename+deploy — it is currently unreachable.
11. `tests/install/*.sh`: add a `just test-install` recipe and (optionally) a manual CI job, or document as human-gated (fits the CLAUDE.md install posture).
12. `capability_verifier.py` / `validator.py` / `job_builder.py`: confirm intended future use; keep with a comment or delete.
13. Move `inner_run_folder_state.py` next to its ASL test.

**Phase 4 — module splits (2-3 days, one PR each, mechanical moves only)**
14. `formatters/artifacts.py` split at `:765` (cleanest seam, private helpers, no callers affected).
15. `run_registry.py` → package with re-exporting `__init__.py` (zero import churn).
16. `render/handler.py` four-way split.
17. `prepare_and_submit.py` extract notify + secrets.
18. `api/handler.py` → extract `artifact_access.py`.
19. `manifest.py`: schema tables out, then the 26-kwarg → 2-struct refactor of `build_manifest` (do last; highest care, do with a focused test pass).
20. Move `console_urls.py`; add structured logging to the remaining handlers.

Risk posture: phases 1–2 are deletions of unreferenced code plus verified one-line fixes; phase 4 is
mechanical moves preserving public names via re-exports, each guarded by the existing ~180-file test
suite. Nothing touches the engine contract (eight-field payload) or the state machine ASL.
