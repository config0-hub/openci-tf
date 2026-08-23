# openci-tf Console — Design Brief

Confirmed design brief for the openci-tf web dashboard ("the console"). Produced by
a shape pass; implementation is delegated to external coding agents. This
document is the contract those agents build against. Product truth lives in
PRODUCT.md; API contracts in docs/API.md; mutation gates in docs/APPLY.md.

## 1. Job and audience

Platform/DevOps engineers operating openci-tf across repositories and AWS
accounts. Visitor mode: **Operate** — they arrive mid-task (checking a run,
chasing a failure, verifying gate state), often many times a day. Scanability,
state legibility, and consistency outrank expression everywhere.

## 2. Outcome and proof

The operator can, without leaving the console:

- see run history and live run status across all repos/accounts (bounded by
  the 90-day run-registry TTL — the UI must make retention legible, never
  imply permanence);
- drill into one run: folder executions, step timeline, artifacts (plan.out,
  drift.json, tfsec.json, infracost.json, manifest), and the S3 plan pointer
  with its expiry (default 1 day);
- see admin state: registered repos, accounts/targets, active locks, and
  apply/destroy gate status (installation `enable_apply` + per-folder opt-in);
- trigger safe verbs only — plan / drift / report — via the existing
  `POST /runs` (explicit folders or all);
- see a placeholder for **Pipelines** (future: sequential stages of Terraform
  folders; concurrency undecided).

Hard boundary: the console can NEVER initiate apply or destroy, and must never
render UI that implies it can. Gate state is displayed, not operated.

## 3. Selected direction — The Quick Reference Handbook

Lineage: aviation QRH, normal and non-normal checklists.

**Thesis:** infrastructure runs as flight procedures. Every run is a checklist
executing line by line; every gate is a boxed challenge-response; drift is a
non-normal procedure with an amber border; failure is an abnormal procedure
with a red border and the corrective evidence beneath it.

**World:**

- Palette: paper `#f4f2ec` ground, ink `#141210`, caution amber `#e8a013`
  (drift / in-progress attention), warning red `#c22f21` (failure), completed
  green `#2e7d43`. Light surface — this is a document world, not a terminal.
- Type: ONE condensed grotesk at two sizes only; rank is weight and caps,
  never a third size. One fixed column measure for all prose.
- Materials: heavy edge index tabs (the primary nav), dotted leaders between
  item and state word, boxed "memory items" (gates/confirmations), ruled
  section lines, rubber-stamp verdicts.
- **State is a mark, not a hue** (raised from the cutting-bench challenger):
  check, cross, stamp, and flag glyphs carry state; color only reinforces.
  A monochrome print of any page must still read completely.
- **Consequence motion** (raised from alphabet-storm): a status change plays
  as the procedure completing — the dotted leader draws, the state word
  stamps in. Never a fade, never a spinner; in-progress is a blinking caret
  or drawing leader.
- **One shared motion clock** (kept from cracktro): everything that moves
  moves off a single tempo.
- Strictly typographic: rules, tabs, leaders, stamps. No illustrated cockpit
  props, no skeuomorphic paper texture, no drop shadows imitating pages.

**First viewport (Runs index):** a bank of edge tabs — RUNS / PIPELINES /
REPOS / ACCOUNTS / LOCKS / GATES — with the RUNS procedure page open. Each run
is a numbered checklist line: `NN  repo · folder-count · verb` with a dotted
leader running to its state word (COMPLETE / IN PROG / FAILED / DRIFT). Drift
rows carry an amber left border, failures red. A "NEW PROCEDURE" action
(trigger a run) sits as a boxed item at the page head — challenge on the left,
the action on the right.

**Signature interaction:** completing runs stamp their state word in; the
apply/destroy gate on the GATES page renders as a read-only boxed memory item
whose challenge-response is filled by the installation config, visibly not
operable.

## 4. Scope and boundaries

- Fidelity: production screens, full breadth (observability + admin + safe
  triggering), desktop-first, usable at 1280px+; narrow screens collapse the
  tab bank to a top strip — mobile is a courtesy, not a target.
- Screens: Runs index · Run detail · New run (trigger form) · Pipelines
  (placeholder) · Repos · Accounts/Targets · Locks · Gates.
- Anti-goals: no apply/destroy UI ever; no charts-for-charts'-sake; no
  invented data (costs, drift summaries, etc. render only what the API
  returns); no dark terminal-diff aesthetic (that direction was declined).

## 5. Screens, states, and ranges

**Runs index.** Filter by trigger_id/repo; bounded pagination as the API
provides. Ranges: 0 runs (empty state: "NO PROCEDURES FILED — runs appear when
a PR comment or API call creates one"), typical tens, cap per API page.
Expired items never render.

**Run detail.** One vertical time axis rules the page (raised from the dive
challenger): every folder execution, step, and artifact pins to a single
descending timeline with tabular timestamps. Folder executions are sub-
procedures: init ✓, validate ✓, plan ✓ with leaders to their outcomes.
Artifacts list with size, checksum, and expiry; the binary plan shows its S3
pointer + expiry only — never a download of the binary through the console.
Expired-plan state is explicit: "PLAN EXPIRED <date> — re-run plan."

**New run.** A procedure form: trigger, pinned 40-char SHA, verb
(plan/drift/report only — the form physically has no other options), folder
mode (all/explicit), idempotency key. Submission returns 202 + run_id and
routes to the run detail in IN PROG state.

**Pipelines (placeholder).** Tab exists from day one. Page renders the
concept honestly: "EXTENDED PROCEDURES — multi-stage runs executing folders in
sequence. Not yet available." plus a static schematic of a staged checklist
(Stage 1 → Stage 2 → Stage 3, each a boxed section). No dead controls, no
fake data. Sequential-only framing; concurrency undecided.

**Repos / Accounts / Locks / Gates.** Registered repos and accounts/targets as
reference pages (settings rows). Locks list with TTL remaining. Gates page
shows `enable_apply` and per-folder opt-in as read-only boxed memory items.

**Cross-cutting states.** Loading: leader-drawing placeholder rows, no
spinners. Error: an abnormal-procedure block (red rule, the HTTP failure
verbatim, a retry item). Auth failure: full-page non-normal procedure naming
the required IAM access. Empty states everywhere in checklist voice.

## 6. Stack and hosting (DECIDED)

**Vite + React SPA + Hono, deployed as a single Lambda with a Function URL.**
Chosen for zero idle cost (the user's stated priority); no Fargate, no ALB,
no CloudFront, no S3 website bucket.

```
Browser
   │  HTTPS
   ▼
Lambda Function URL              (auth: NONE at the URL; see token check below)
   │
   ▼
One Lambda (Node 20 + Hono, hono/aws-lambda adapter)
   ├── /        → serves the static Vite/React build, bundled in the zip
   └── /api/*   → SigV4-signs with the execution role → existing API Gateway
```

- Hono app: ~50–100 lines. Static file serving + `/api/*` proxy that signs
  with aws4fetch (or @aws-sdk/signature-v4) using the Lambda execution role.
  Credentials never reach the browser. The same app must run locally with
  `node` for development (env-provided AWS creds), identically to Lambda.
- Access control: the function URL is publicly reachable. The Hono app serves
  the static login shell and assets without authentication, then enforces a
  shared bearer token on every `/api/*` request (constant-time compare). At
  cold start the Lambda reads and decrypts the token from its exact SSM
  SecureString parameter; only the parameter name is in the environment. The
  SPA obtains the token via a simple login prompt stored in sessionStorage.
  This is the resolved answer to the "operator identity layer" open question.
  Do not add Cognito, OAuth, or any auth framework.
- Frontend: React 18 + TypeScript, TanStack Router (client-side, type-safe
  routes; list filters like trigger_id/repo live in typed search params so
  filtered views are shareable URLs) and TanStack Query (fetching + polling
  of IN PROG runs). Plain CSS with design tokens as custom properties. No
  component library, no CSS framework, no state library beyond TanStack
  Query, and no other TanStack pieces (no Start, no Table) — the QRH world
  is bespoke and small.
- Deploy: one Terraform-managed Lambda + execution role + function URL,
  following this repo's existing Lambda/Terraform conventions and `just`
  recipe patterns. The execution role must be added to
  `api_caller_policy_json` (see INSTALL.md).
- Cold starts (~300–500ms after idle) are accepted; irrelevant for an
  internal tool.
- If the static bundle ever outgrows the Lambda zip comfortably, graduate to
  S3 + CloudFront for assets; do not build that now.

## 7. Constraints and open decisions

- Auth is decided (§6): the browser may fetch the static login shell/assets;
  every browser `/api/*` request requires the shared bearer token, and the
  Lambda uses its execution role for SigV4 at the proxy→API boundary. Nothing
  further — the implementer must not add identity machinery.
- Caller policy: the Lambda execution role needs an `api_caller_policy_json`
  entry (see INSTALL.md) covering trigger_ids, actions, artifact classes.
- The operator does not write frontend code: the implementation must be
  fully self-sufficient — working build, deploy recipe (`just` target),
  local dev instructions, and no steps that assume frontend expertise.
- Pipelines data model does not exist yet; the placeholder must not guess
  field names.
- Retention values (90d registry, 1d plans) are installer-configurable —
  render what the API returns, do not hard-code.
- Do not overcomplicate: no state library beyond React Query, no auth
  framework, no design-system package. Clean, small, typed.
