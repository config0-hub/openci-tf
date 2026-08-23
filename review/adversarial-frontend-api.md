DEFECTS: 1 blocker / 8 major / 5 minor

## Findings

### BLOCKER — `POST /runs` accepts the non-console `plan_destroy` verb

**Files:** `src/domain/run/request.py:16,179-185`; `src/domain/run/api_authorization.py:80-82,133-139`; `src/services/api/handler.py:130-145`; `tests/unit/test_api_control_plane.py:48-59`

**Exact failure scenario:** Editing the New Run request body to set `"action":"plan_destroy"` bypasses the three radio buttons. The API parser classifies `plan_destroy` as safe, and caller-policy parsing accepts arbitrary action strings. If the caller policy includes `plan_destroy`, `_create_run` starts it and returns 202. I reproduced this by giving the test caller a `plan_destroy` policy and stubbing `start_run_from_request`; the handler returned `{"run_id":"unsafe-run","created":true}` and passed an action of `plan_destroy` to orchestration. The existing test proves only that `apply` is rejected. This violates the binding plan/drift/report-only contract and can also make the console render `PLAN_DESTROY`, implying a destroy procedure.

**Smallest honest fix:** For `ingress_source == "api"`, allow exactly `plan`, `drift`, and `report`; reject `plan_destroy`, `apply`, and `destroy` before authorization. Also reject unsupported action names while loading API caller policies, and add handler-level tests proving all three non-console verbs return 400 without calling orchestration even when the policy names them.

### MAJOR — clearing/rejecting the token leaves authenticated query data available to an unverified token

**Files:** `frontend/src/AppShell.tsx:30-33`; `frontend/src/AuthGate.tsx:9-18,24-34`; `frontend/src/api.ts:27-33`; `frontend/src/main.tsx:12-24`

**Exact failure scenario:** An operator opens Accounts, Runs, or a run manifest, presses **CLEAR TOKEN**, and another user enters any non-empty string. `AuthGate` immediately renders children before an API response verifies the token, while the process-wide `QueryClient` still contains the previous session's data. React Query renders that cached data synchronously. On a slow/hung request it remains visible; on a 401 it is exposed until rejection arrives. I reproduced the Accounts rows reappearing after clear-token with an arbitrary token while `/api/accounts` was pending. A 401 removes only `sessionStorage`; neither sign-out nor rejection clears the query cache.

**Smallest honest fix:** Clear the `QueryClient` on explicit sign-out, token replacement, and token rejection, and do not render cached protected routes until the replacement token has completed an API authorization probe. At minimum, key or reset every protected query by an opaque token-session generation.

### MAJOR — Accounts renders two blank, nonexistent ARN fields and omits the documented role name

**Files:** `frontend/src/admin-contract.ts:19-25`; `frontend/src/AdminScreens.tsx:100-108`; `docs/API.md:68-78`

**Exact failure scenario:** A valid `/accounts` row is `{alias, account_id, role_name}`. `AccountRow` ignores `role_name` and renders `hub_role_arn` and `target_role_arn`, which the type explicitly marks as impossible. The mock-mode browser showed empty definitions for both HUB ROLE and TARGET ROLE for every account. Typecheck still passes because the obsolete fields were retained as optional `never` properties.

**Smallest honest fix:** Remove the impossible ARN properties from `AccountTarget` and render one truthful `ROLE NAME` row from `account.role_name`. Add a component/DOM contract test using the real response shape.

### MAJOR — artifact existence is falsely rendered as successful step completion

**Files:** `frontend/src/RunDetail.tsx:34-40,128-157`; `frontend/src/types.ts:60-67`; `frontend/README.md:120-123`

**Exact failure scenario:** `stepOutcome` maps the mere presence of `validate.out` or `tf/plan.out` to `✓ COMPLETE`. Failed commands also produce output artifacts. A validation failure with `validate.out` therefore displays `VALIDATE ✓ COMPLETE` beside corrective evidence saying validation exited 1. The supplied failed mock visibly renders INIT, VALIDATE, and PLAN all COMPLETE for the failed folder. Conversely, without a manifest the UI guesses that INIT is the in-progress/failed step regardless of where execution actually is. The manifest contract has no per-step outcomes, so these success/failure claims are invented.

**Smallest honest fix:** Until the API supplies authoritative step outcomes, label artifact presence as `FILED` rather than `COMPLETE` and do not assign failure/in-progress to a guessed step. To meet the full brief, add bounded step outcome/status data to the API contract and render that.

### MAJOR — Repos, Accounts, and Locks silently discard backend pagination

**Files:** `docs/API.md:53-58`; `frontend/src/admin-contract.ts:14-16,28-30,39-41`; `frontend/src/api.ts:54-57`; `frontend/src/AdminScreens.tsx:82-96,113-127,162-177`

**Exact failure scenario:** All three APIs default to 25 rows and return `cursor` when more rows exist, but the frontend response types omit `cursor`, never sends one, and provides no next-page control. Installations with 26+ registrations/accounts/locks can never see rows after the first page. Locks are worse because DynamoDB applies `Limit` before the TTL filter: an expired-only first evaluated page can return `locks:[]` plus a cursor while an active lock exists later, and the UI states **NO ACTIVE LOCKS FILED**.

**Smallest honest fix:** Include `cursor` in all three response types, pass it through the API functions and typed route/search state, and provide bounded next/previous navigation. Preserve and expose a cursor even for an empty filtered lock page.

### MAJOR — the real Runs screen cannot show history across repositories, and its repo filter is only page-local

**Files:** `docs/FRONTEND_BRIEF.md:17-21,92-95`; `frontend/src/RunsIndex.tsx:24-47,83-86`; `frontend/src/api.ts:41-47`; `src/services/api/handler.py:165-180`; `frontend/README.md:117-119`

**Exact failure scenario:** In real mode the query is disabled until one `trigger_id` is supplied. The backend lists only that trigger's GSI partition and ignores the `repo` parameter that the browser sends. The browser then applies `repo` only to the current 25-row page. It cannot provide the contracted all-repo live/history view; and when a matching row is on a later page, the current page can claim **NO PROCEDURES FILED** even though a match exists.

**Smallest honest fix:** Add an authorization-bounded, cross-trigger run-list contract with server-side repo filtering and one stable cursor, then make the index use it. Do not claim an all-repo empty result from a client-filtered slice.

### MAJOR — real drift runs can never receive the DRIFT state shown by the design and mocks

**Files:** `frontend/src/types.ts:80-87`; `src/domain/run/registry_schema.py:67-76`; `frontend/README.md:124-126`; `docs/FRONTEND_BRIEF.md:39-42`

**Exact failure scenario:** `runState` renders DRIFT only when `run.status === "drift"`. The authoritative registry status set has accepted/running/succeeded/failed/infrastructure_error/in_progress/skipped and never emits `drift`. A successful real drift run is therefore shown as COMPLETE; only mock data invents a `drift` status. An operator cannot distinguish “drift detected” from “drift procedure completed with no drift,” while a failed drift run correctly needs failure to dominate.

**Smallest honest fix:** Add an authoritative drift-result field or terminal state to the run API/registry, define precedence for drift plus failure, and map that field in the UI. Do not infer drift merely from the requested verb.

### MAJOR — `/gates` can never provide the contracted per-folder gate state, and the UI misstates unavailable data as no opt-ins

**Files:** `src/services/api/handler.py:242-254`; `docs/API.md:89-96`; `frontend/src/AdminScreens.tsx:181-203`; `docs/FRONTEND_BRIEF.md:25-26,116-118`

**Exact failure scenario:** `_get_gates` always returns `folders: []`, because folder gates exist only in checked-out repo config. `GatesPage` interprets that as **NO FOLDER OPT-INS FILED**. An installation can have multiple `apply: true`/`destroy: true` folders and the console will still assert there are none. The page therefore neither shows the required per-folder gate status nor honestly distinguishes “not centrally available” from “disabled.”

**Smallest honest fix:** Immediately render the empty array as “per-folder state unavailable; source is pinned repo config,” not “no opt-ins.” To satisfy the brief, introduce an authoritative, SHA-bound projection of repo-config gate rows and return/render it without creating controls.

### MAJOR — one in-progress detail poll can refetch 100 immutable manifests every five seconds

**Files:** `frontend/src/RunDetail.tsx:65-71`; `frontend/src/api.ts:59-70`; `src/platform/aws/run_registry.py:471-493`

**Exact failure scenario:** `refetchInterval` reruns all of `getRunDetail`. That function fetches the run, all folders, then launches an unbounded `Promise.all` manifest request for every folder carrying `manifest_s3_uri`. With the API's 100-folder maximum and an overall run still in progress after many folders have completed, every five seconds produces 102 requests and repeatedly downloads unchanged manifests. Multiple open detail tabs multiply the storm.

**Smallest honest fix:** Poll only the run/folder registry state. Fetch each manifest through its own query keyed by run/folder/digest with effectively immutable caching, and bound manifest request concurrency.

### MINOR — an in-progress run is labeled a terminal registry state

**File:** `frontend/src/RunDetail.tsx:170-173`

**Exact failure scenario:** `/runs/run-progress` renders `RUN IN PROG` followed by `▸ TERMINAL REGISTRY STATE`, even though both the header and polling behavior say the run is active. This creates a direct state contradiction on the operator timeline.

**Smallest honest fix:** Render a terminal item only for terminal states; for active runs use `LATEST REGISTRY STATE` or omit the terminal event.

### MINOR — lock and artifact expiry decisions trust the browser clock

**Files:** `frontend/src/AdminScreens.tsx:130-165`; `frontend/src/RunDetail.tsx:42-53`; `frontend/src/RunsIndex.tsx:44-47`

**Exact failure scenario:** A workstation clock ahead hides server-active locks/runs and labels plans expired early. A clock behind keeps a plan looking usable and a lock looking active after server expiry. The absolute API timestamps are authoritative, but all countdown/filter decisions use local `Date.now()` and the UI claims expired locks are never shown.

**Smallest honest fix:** Return a server observation time (or preserve the upstream HTTP `Date`) and calculate a per-response clock offset. Keep the absolute UTC expiry visible and avoid claiming authoritative usability from an unsynchronized client clock.

### MINOR — unknown client routes fall through to TanStack's generic paragraph

**File:** `frontend/src/router.tsx:9,42-43`

**Exact failure scenario:** Navigating to `/not-a-route` renders only `Not Found` inside the shell. TanStack emits a warning that no `notFoundComponent` is configured. This bypasses the brief's abnormal-procedure error treatment and gives no recovery action.

**Smallest honest fix:** Configure a root/default not-found component using the abnormal-procedure block and a working return-to-Runs action.

### MINOR — passing tests do not pin the most sensitive new contracts

**Files:** `tests/unit/test_api_control_plane.py:298-359,419-438`; `frontend/server/mock.test.ts:10-32`

**Exact failure scenario:** The account leakage test patches `list_account_targets`, so it never proves that the adapter strips `external_id` or session metadata. The admin authorization test exercises only `/repos`, not all four routes. There is no direct limit-clamp/forged-cursor/fail-loud adapter coverage. Frontend tests exercise mock response lengths but no rendered components, which is why the blank Accounts regression passes. All 24 Python tests and all 8 frontend tests are green despite these contract failures.

**Smallest honest fix:** Add direct adapter tests with rows containing sentinel secrets, parameterize authorization-before-I/O across all four routes and path/method variants, cover bounds/cursors/error propagation, and add rendered component tests for documented response shapes.

### MINOR — successful create does not invalidate the Runs cache

**Files:** `frontend/src/NewRun.tsx:33-36`; `frontend/src/main.tsx:12-19`; `frontend/src/RunsIndex.tsx:29-34`

**Exact failure scenario:** The 202 handler navigates to detail but never invalidates any `['runs', ...]` query. Returning to Runs while a prior list is still considered fresh can omit the just-created procedure; under a delayed/offline refetch the stale list remains visible. This is avoidable stale state immediately after a mutation.

**Smallest honest fix:** On successful creation, invalidate all `['runs']` queries before/while navigating and ensure the detail query begins in IN PROG from authoritative data.

## Attempted, not confirmed

- **Admin AuthZ bypass:** I found no path for a run-only caller to reach `/repos`, `/accounts`, `/locks`, or `/gates`. `read_classes` defaults empty, each exact GET route calls `authorize_admin_read` before I/O, and HEAD, case changes, trailing slashes, or extra path segments fall through to 404.
- **Admin data leakage in current adapter code:** Current repo/account/lock projections are explicit allowlists; I found no returned ExternalId, ARN, webhook secret, token, or session-TTL field. The concern above is missing regression proof, not a confirmed current leak.
- **Cursor cross-partition injection:** Admin cursors supply only `sk`; `_query_partition` always reconstructs `pk` from its fixed route partition. I did not find a cross-PK read. Arbitrary/oversized cursors are not validated or signed and may induce DynamoDB validation errors, but I did not confirm data exposure.
- **TTL boundary/fail-loud:** Active locks use `expires_at > now` in DynamoDB and defensively skip `<= now`; the equality boundary is correct. DynamoDB and malformed-row failures are not swallowed into empty 200 responses.
- **Search-param XSS:** typed search accepts only strings, `URLSearchParams` encodes outbound values, and React escapes values reflected in inputs/errors. I found no HTML injection sink.
- **Polling leaks and motion/a11y sample:** React Query owns interval cleanup on unmount; I did not confirm an interval leak. Focus-visible rules, reduced-motion animation suppression, alert/live-region wiring, and token-rejection focus are present in the sampled paths.
- **Checks run:** `cd frontend && npm test` (8 passed), `cd frontend && npm run build:client` (passed), and `uv run --with pytest --with boto3 python -m pytest tests/unit/test_api_control_plane.py -q` (24 passed). Mock-browser checks covered Accounts, failed and in-progress run details, token-cache behavior, and the unknown route.
