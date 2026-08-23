---
name: tui-control
description: Internal TUI plan controller for a checked runnable `plan.md + route.yaml` pair. Requires `HERDR_ENV=1`. This is the TUI agent — the one pane the human talks to after `just run-start` launches it. It gates the plan and route, sets up the cockpit plus shared services, then loops phases with one fresh phase leader each. Stage completion travels through durable Recruiter receipts; phase completion travels through the leader's event-driven marker plus authoritative `phase-result.json`, so no always-running LLM poller is needed. It applies forward-only backtracking and the configured advisor/human ladder, writing `run-status.md`. Every stage is done by a fresh Recruiter-hired worker. A true revert stops and asks the human.
argument-hint: --plan <plan.md> --route <route.yaml> [--run-tree <exact-dir> | --run-root <parent-dir>] [--slug <name>] [--start-phase <N>] [--max-phases <N>]
---

# Shared meta-runner phase protocol

This is the shared execution contract for the run controllers: `/tui-control` (the TUI) and `/phase-leader` (the phase leader). It is deliberately generic and public: examples use placeholder model and agent names only.

The defining rule of this protocol: **LLM implementation, audit, verifier, advisor, and consult work is always a work ORDER placed to the UpAgent Recruiter, which hires a fresh worker for it — never a native or nested subagent of the leader.** The ordinary deterministic stages are controller actions with durable result receipts, not native subagents. The phase leader does not call the harness's own agent/task tool to run stage work. It writes durable order/result files, invokes the per-command Recruiter when a worker is needed, and reads typed worker `result.json`, controller `controller-result.json`, and `phase-result.json` files as the authoritative outcomes; the Recruiter alone drives Herdr IPC. Everything else — the two-file input, the five-stage worktree lifecycle, the Stage 2 audit gate, the adversarial-evaluator persona — is transport-agnostic substrate that survives unchanged.

## Runnable input is two files

Every semi-AFK meta run consumes both files below. This is the schema from the meta runner synchronization plan plus the five-stage worktree lifecycle update.

```text
runnable-meta-job/
├── plan.md       # the work
└── route.yaml    # who runs it, when it merges, and how finalization proves green
```

The canonical plan body stays clean:

- one `# Plan: <title>` heading;
- one `Goal:` line;
- ordered `## Phase <N> — <title>` sections starting at 0;
- phase work plus required `Done:` and optional `Ideal:`.

Do **not** put runner, model, harness, agent, team, worker, stage routing, merge timing, worktree branch names, or CI/CD checks in the plan body. If a draft plan contains that information, move it to the route profile or fail loud for human correction.

The route profile centralizes `llm_profiles`, worktree lifecycle, finalization checks, deterministic merge timing, per-phase accuracy, escalation budgets, and explicit phase/stage agent choices.

Required route shape (see `meta-plan-format.md` for the full schema, including the optional `accuracy`, `advisor_profile`, and budget keys):

```yaml
llm_profiles:
  claude-low:
    harness: claude
    model: configured-claude-model
    effort: low
    advisor:
      enabled: false

  claude-auditor:
    harness: claude
    model: configured-claude-model
    effort: medium

  codex-implementation:
    harness: codex
    model: configured-codex-model
    effort: medium

  pi-default:
    harness: pi
    model: configured-default

# model is the HARNESS-NATIVE id shape: claude → alias/full name (paired with effort);
# codex → bare model id (paired with effort, passed as model_reasoning_effort);
# pi → provider/id[:thinking] (the :thinking suffix IS pi's effort). effort is optional —
# the phase leader resolves it to `medium` at order time when a profile omits it, so roster
# templates can always use {effort}.

worktree:
  branch_template: tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}

finalization_defaults:
  green_checks:
    - command: task ci
  log_checks:
    - source: build, deploy, and runner logs
      fail_patterns: ERROR,FATAL,Traceback,uncaught
  advisor_profile: claude-low   # OPTIONAL. absent ⇒ escalation goes budget → human
  watchdog_profile: claude-low  # OPTIONAL, legacy. Parsed for route compatibility; no standing watchdog is hired.
  phase_pass_budget: 3          # OPTIONAL. phase re-runs before escalate (default 3)
  stage_try_budget: 3           # OPTIONAL. stage retries before escalate (default 3)

phases:
  phase-0:
    accuracy: medium            # OPTIONAL. medium (default) = stages 1–5; high adds stage-0-alignment
    merge_back_at: stage-3-integration-acceptance-seams
    lead:
      llm_profile: claude-low
      agent: phase-leader
    stages:
      # stage-0-alignment goes here ONLY when accuracy: high (independent from stage-1)
      stage-1-implementation:
        purpose: "LLM implementation plus focused local tests in a TDD coding loop on the temp worktree branch"
        llm_profile: codex-implementation
        agent: backend
      stage-2-adversarial-audit:
        purpose: "independent semantic/adversarial audit of Stage 1 code on the same temp worktree branch"
        llm_profile: claude-auditor
        agent: adversarial-evaluator
        must_differ_from: stage-1-implementation
      stage-3-integration-acceptance-seams:
        purpose: "run deterministic changed-scope local seam/contract checks; hire verifier only on failure or ambiguity; merge here iff merge_back_at selects this stage"
        llm_profile: claude-low
        agent: qa
      stage-4-upstream-dag-verification:
        purpose: "ordinary phases do not deploy or run broad shared acceptance here; route-owned candidate-level finalization owns that when present; explicit integration-construction is a separate phase"
        llm_profile: pi-default
        agent: monorepo-pkgs
      stage-5-finalization:
        purpose: "deterministically merge if needed, run configured green checks, inspect logs, and clean up"
        llm_profile: claude-low
        agent: qa
```

Rules:

- Every phase has `merge_back_at`, with exactly one of Stage 3, Stage 4, or Stage 5.
- Every phase lead has explicit `llm_profile` and `agent` fields.
- Every stage has explicit `llm_profile` and `agent` fields.
- Every phase has all five base stage ids; `stage-0-alignment` is present **iff** `accuracy: high` and forbidden otherwise.
- The `agent` value is the configured project or harness agent/persona name, such as `general`, `backend`, `golang`, `python`, `frontend`, `qa`, or a project-specific specialist.
- The runner resolves `agent` from the appropriate harness/project agent directories and fails loud if a required agent cannot be found. The route is authoritative and deterministic per stage: `llm_profile` → harness + model, `agent` → persona. The phase leader includes that persona contract in `instructions.md`; this is mandatory for Codex and any other harness without an `--agent` flag. The Recruiter never picks the agent; it only holds a mechanical per-harness launch template.
- Prefer domain or feature-specific agents when available. Use `agent: general` only when generic behavior is intentional.
- Repeated templates are allowed only if resolved to explicit phase/stage entries before execution begins.
- Stage 2 must be independent from Stage 1 by profile, agent, harness, model family, or persona. When `accuracy: high`, `stage-0-alignment`'s audit reviewer follows the same independence rule against `stage-1-implementation`.
- Stage 3 and Stage 5 route entries remain required so the route has an explicit fallback verifier profile/agent for failures or ambiguity, but ordinary Stage 3 and Stage 5 work is deterministic controller work, not an automatic LLM hire.
- Stage 4 remains a required route slot for compatibility and for non-ordinary variants such as IaC approval/apply records. In an ordinary phase it is not a per-phase shared-environment, deployment, CI, or global acceptance stage.
- Stage 5 must have effective green checks and log checks from `finalization_defaults` or phase-level `finalization` overrides/additions. The leader always runs exactly the effective route-owned `green_checks`; it does not infer whether a later candidate-level finalization exists. At plan/conversion time, if an explicit later candidate-level finalization/gate owns repository-wide test/lint/static-analysis, the route author omits those generic commands from per-phase `green_checks`; otherwise the route retains the repository's normal green checks.
- Advisor settings may be used by a phase leader or by a stage worker when the selected harness supports advisors. Advisors are advisory only.

## The run tree — the durable record

Every run writes a filesystem tree that is the source of truth for what happened. Herdr IPC carries only live process/pane signals; the tree is what the leader, the TUI, and a human read. It is rooted at `<run-root>/<date>/<slug>/`, where `<run-root>` is the runner-supplied work-log root and `<slug>` names the run.

```text
<date>/<slug>/
├── plan.md · route.yaml · research.md      # frozen run inputs; route.yaml is this run's single live copy
├── run-status.md                           # TUI rolling log: phase order, passes, backtracks, why
├── active-leader-panes.json                # optional phase-id → {pane_id, herdr_session, ownership.pane:{state:"created"}, health} map
└── phases/<phase-id>/
    ├── phase-status.md                     # leader rolling log across this phase's passes/stages
    ├── phase-result.json                   # latest verdict + revisit:[phase-ids]  ← the TUI reads this
    ├── handoffs/<role>-vN.md               # versioned, never overwritten
    └── pass-<p>/                           # one TUI execution of this phase (forward only)
        └── stages/<stage-id>/              # only the stages that RAN this pass
            └── try-<m>/                    # leader's stage retry within the pass
                ├── order.json              # what the leader asked the Recruiter (type, cwd, refs, budgets)
                ├── instructions.md         # the stage brief the worker reads
                ├── result.json             # worker-order source of truth: verdict + revisit:[stage-ids] + full_log ptr
                ├── controller-result.json  # deterministic-controller source of truth when no worker order ran
                ├── compacted.md            # worker's own short summary back to the leader
                └── log/
                    ├── full_log →          # POINTER to the worker's harness transcript
                    └── otel/               # optional structured full-IO (only if OTEL_* injected)
```

- **run** / **pass** / **try**: a *run* is the whole plan; a *pass* is one TUI execution of a phase (forward only); a *try* is a leader retry of a stage inside a pass.
- **Durable vs heavy.** `order.json`, `instructions.md`, worker `result.json`, `compacted.md`, the handoffs, controller `controller-result.json`, and the `*-status.md` logs are durable in the work-log. The heavy harness transcript stays where the harness writes it; worker `result.json.full_log` points to it. OTel is captured only when `OTEL_*` env is injected into the order.
- **Deterministic controller stages.** Ordinary Stage 3/5 controller actions write `controller-result.json` under their stage try directory, not worker `result.json`. They do not have `order.json`, `instructions.md`, `compacted.md`, a handoff, or a worker `full_log` unless a separate anomaly verifier order was hired. Their `controller-result.json` uses the deterministic-stage evidence shape defined below.
- **Rolling summaries are the connective tissue.** `phase-status.md` gets one line per stage per pass (for example `pass1 stage-2 failed — reason X, revisit stage-1`). At the start of a new pass or try, the controller reads it, sees where work failed, and replays the pointed units forward. `run-status.md` is the TUI's phase-level equivalent.
- **The route copy is frozen from the origin but live inside the run.** Once the run tree is created, its `route.yaml` is the single live routing source; the origin is historical/read-only. Every leader receives the run-tree plan/route paths, and every mid-run edit (including the last-writer marker) touches only the run-tree route.

## Execution model — worker orders and deterministic controller stages

The phase leader runs all LLM work by placing a **work order** through the per-command UpAgent Recruiter, which hires exactly one fresh worker for that order and releases it when the order is done. This includes Stage 1 implementation, Stage 2 audit, `stage-0-alignment` workers, anomaly verifiers, advisors, and consults. The leader never spawns a native subagent, team, or nested harness session to do LLM work.

Ordinary Stage 3 seam checks, ordinary Stage 4 deferral/merge records, and Stage 5 finalization are deterministic controller actions. They still write typed stage evidence to `controller-result.json` and participate in `phase-status.md` / `phase-result.json`, but they are not worker orders. Do not invent a synthetic `order_id`, `instructions.md`, `compacted.md`, worker `result.json`, or worker `full_log` for a deterministic controller stage.

The worker order/result contract (the exact JSON fields the leader and worker exchange) is fixed by the UpAgent `contracts.py` module. The leader writes `order.json` (required: `order_id`, `phase_id`, `stage_id`, `harness`, `model`, `agent`, `cwd`, `instructions_path`, `result_path`, `cockpit_pane`; lifecycle fields: a caller-stable globally scoped `request_id` and `requester: {id, kind, address}`; optional `env`), and the worker writes `result.json` (required: `order_id` echoing the order, `verdict` — one of `passed` / `failed` / `blocked`, `full_log` — the pointer to the worker's own harness transcript; optional `revisit` — a list of recognized stage ids, required non-empty when `verdict` is `failed`).

A deterministic controller stage writes `controller-result.json` with this shape instead: stage id, `runner: controller` (or an equivalent controller marker), commands run, exit codes, log/evidence paths or bounded excerpts, try number, and final verdict. If it escalates to an anomaly verifier, that verifier is a normal worker order with its own `order_id`, worker `result.json`, and `full_log`; the controller result records the verifier evidence separately instead of pretending the controller stage had a worker transcript.

`cockpit_pane` is the id of an existing pane in the cockpit workspace to split the worker from — Herdr's `pane split` takes a source pane, not a workspace label, so the runner threads a live cockpit pane id (the phase leader's own pane) down into every order.

`just upagent-up` optionally ensures the Recruiter's visible services/status pane; it starts no daemon and is not a request prerequisite. Each command imports current canonical source, and mutating lifecycle commands opportunistically reconcile dead/expired durable leases. Each request uses direct Python-owned lifecycle by default; optional dedicated Account Managers remain available by roster opt-in. Python owns facts, state transitions, pane operations, durable requester mailboxes, and lease fencing. The leader submits with `just upagent-request`, then waits with `just upagent-await` or `just upagent-await-any`; shell pane input is never used as a queue.

The worker-order round-trip uses per-command UpAgent processes and Herdr's own IPC:

```text
leader:    write pass-<p>/stages/<stage>/try-<m>/order.json  +  instructions.md  (order.cockpit_pane = the leader's cockpit pane)
leader:    just upagent-request <order.json path>  # return only after verified startup
Recruiter: persist → start/verify manager → atomically `herdr agent start` the worker
Recruiter: verify expected process + detected harness + cwd; return manager/worker addresses
leader:    just upagent-await <order.json path>    # Python waits; LLM does not poll
Recruiter: write final lease-specific brief with literal order_id + one private result path
Recruiter: race one agent-status subscription against the private result; run one-shot LLM checks only at anomaly checkpoints
worker:    write private result + compacted.md + handoff, then exit
Recruiter: validate → close + verify owned worker absent → publish public result + receipt
leader:    receive ORDER_RECEIPT → read + validate public result.json
```

At a declared work cap, the Recruiter changes state to `awaiting-requester` and `upagent-await` returns `REQUESTER_DECISION_REQUIRED`. The recorded requester uses the per-generation control token from `REQUEST_ACCEPTED` to authorize `extend` or `cancel`. No response during the configured grace period permits the Recruiter's hard stop. A manager or checker may recommend an action, but cannot execute it.

Validate the installed Herdr command surface before launch (the documented baseline is `herdr agent start/get/wait`, `herdr pane get/process-info/run/read/close`, and `herdr wait agent-status`). The Recruiter uses one `agent start` request with direct argv; it does not split a shell and inject a launch command afterward. If the local Herdr version exposes different syntax, adapt only after validating it. A malformed order or result is fail-loud: the Recruiter refuses to hire on a bad order; the leader treats a missing or malformed result as a `blocked` stage.

**Notification follows ownership boundaries.** The worker has no Recruiter/leader/TUI addresses and sends no terminal text. Its one private result wakes the owning Recruiter job; the durable receipt wakes the phase leader through its blocking `upagent-await`/`upagent-await-any`; `phase-result.json` wakes the TUI through its blocking `upagent-phase-await`. Pane text and human toasts are observational only.

**Workers are terminal and non-delegating.** A hired worker does its one stage, writes its result/compacted/handoff, and then actually exits its harness session; stopping at an idle interactive prompt is not done. It must not create further agents, teams, panes, nested harness sessions, or advisors. If it needs more help it returns `blocked` with the decision needed, and the leader decides the next move. A worker may consult a specialist for repo knowledge through the same Recruiter that hired it — a consult is an ordinary UpAgent order, and asking a question is not delegation. Every consult a worker makes is recorded in its `result.json` under a `consults` list (`{consult_id, specialist, request_id, answer_path}` per consult; an empty list when none applied). The consult receipts are audit evidence, not optional bookkeeping, and that is now mechanical rather than a promise: the Recruiter keeps its own record of every consult it brokered, resolves each claimed entry against it at publication, and stamps the phase receipt with `consults_verified` and `consults_unverified`. A worker cannot bank a receipt for a consultation that never happened. Be equally precise about what that does NOT settle: it cannot tell whether a consult SHOULD have happened — that means judging whether the work touched an area a listed specialist owns, and it stays the Stage 2 auditor's call — and a verified consult is proof that a question was asked, never that it was a good one.

**Consulting a specialist is MANDATORY, not voluntary, when one owns the area.** An agent does not know what it does not know: grepping cold finds *something* and proceeds confidently past the repo's actual conventions (language idiom, how to test, onboarding/cleanup steps, domain contracts). The mechanism is deterministic, not aspirational: the leader runs `just upagent-specialists` and pastes its output — the **phone book**: every available specialist (kit base merged with this repo's own) plus the exact consult mechanics — VERBATIM into every stage brief. The worker MUST consult the owning specialist BEFORE deciding anything in a listed area — conventions are asked, never guessed. The Stage 2 auditor checks the receipts: it reads the brief's phone book and the worker's `result.json` `consults` list, and a worker that decided in a listed area with no matching consult receipt is a blocking Stage 2 audit finding.

## Handoff between workers

Every worker writes a short, versioned handoff before its pane closes so the next same-role worker — or the leader — resumes with immediate context instead of a cold start. The contract lives in the shared meta-runner handoff protocol; keep it to the `phases/<phase-id>/handoffs/<role>-vN.md` path, never overwritten. After `result.json`, `compacted.md`, and the handoff are durably written, the worker exits its session so Herdr can surface a real terminal transition instead of an idle prompt.

## Backtracking, forward-only passes, and escalation

Backtracking has two levels; both replay **forward, in order**, and neither reverts. A failing unit emits a structured `revisit: [ids]` pointer, and the controller replays from the earliest pointed id forward.

```text
stage fails → worker result.json or controller-result.json verdict=failed, revisit=[stage-ids], reason recorded in phase-status.md
leader: replay from the earliest revisit stage-id forward; increment try
  stage_try_budget hit → advisor configured? place an advisor order (context = phase-status.md)
                            advisor result.json: verdict=passed, decision = continue | loop | stop-ask-human
                         no advisor → stop-ask-human
phase fails (leader gives up) → phase-result.json.verdict=failed, revisit=[phase-ids]
TUI: replay from the earliest revisit phase-id forward as a NEW pass; increment pass
  phase_pass_budget hit → advisor configured? place an advisor order (context = run-status.md)
                             advisor result.json: verdict=passed, decision = continue | loop | stop-ask-human
                          no advisor → stop-ask-human
stop-ask-human → the TUI halts and surfaces status to the human
```

- **No revert automation.** This is a forward-only Ralph loop: nothing already produced is discarded to "go back". A true revert (throwing away merged work) is a major decision — the TUI stops and asks the human; it is never automated.
- **The advisor is hired like any worker**, through the Recruiter, on the route's `advisor_profile`. An advisor order is an ordinary order and must carry a **recognized** `stage_id` (the order contract rejects anything outside the six; there is no `stage_id: "advisor"`). For a stage-level advisor (the leader, after a stage try budget) it reuses the failing **stage's** id. For a phase-level advisor (the TUI, after a phase pass budget) a phase has a `phase_id`, not a `stage_id`, so there is nothing "phase" to reuse: set `phase_id` to the failing phase and use the fixed convention `stage-5-finalization` (the whole-phase judgment stage) as the `stage_id` — never write a `phase_id` into `stage_id`. The controller that placed the order (the leader for a stage try, the TUI for a phase pass) knows it is an advisor order. The advisor worker writes a normal `result.json` with `verdict: passed` **plus** the optional `decision` field — one of the exact tokens `continue`, `loop`, or `stop-ask-human` (the contract's `ADVISOR_DECISIONS`). The controller reads `result.json.decision` — not a special verdict — and maps it: `continue` = accept the unit and move on; `loop` = keep looping (reset/extend the budget for another round); `stop-ask-human` = halt and surface to the human. **Fail-safe: if an advisor result is missing `decision` (or its verdict is `blocked`/`failed`), the controller treats it as `stop-ask-human`** — never silently continues on an absent ruling. The advisor reads the relevant `*-status.md`, writes no code, and runs no commands. With no `advisor_profile` set, a budget exhaustion escalates straight to the human (`stop-ask-human`).
- Budgets default to 3 (`phase_pass_budget`, `stage_try_budget`) when the route omits them.
- **Mandatory specialist consult on repeated diagnosis.** When a retry (`try N+1`) revisits the same unresolved failure signature a prior try already investigated, the leader MUST put this instruction in that retry's `instructions.md`: **“Before re-investigating: this is not the first attempt at this failure. Consult the owning specialist first with `just upagent-consult`. Send it the failure signature and what the last try already tried and ruled out; do this before forming a new hypothesis from docs alone. If the specialist does not know, record that in `result.json` — it is still useful signal — but do not skip asking.”** Reading static specialist files does not satisfy this requirement. Record the consult id, its request id, and the answer/error path with the retry evidence.

## Accuracy: medium (five stages) or high (adds stage-0-alignment)

Each phase sets `accuracy:` in its route entry. **medium** (default) runs stages 1→5. **high** adds `stage-0-alignment` before Stage 1.

`stage-0-alignment` is a **lead-orchestrated sequence of separate non-delegating workers**, not one delegating agent. The leader places three ordered work orders and sequences them itself:

1. **mini-research** — a fresh worker researches this phase against the original `research.md` and records what it found.
2. **mini-plan** — a fresh worker drafts a mini-plan for this phase against the original `plan.md`.
3. **independent audit** — a fresh worker, independent from `stage-1-implementation` (by profile, agent, harness, model family, or persona), audits the mini-plan against the big plan.

Misaligned ⇒ the leader loops stage-0 (redo the mini-plan) within the stage-try budget. Unreconcilable ⇒ `blocked`, which escalates. Stage-0 outputs are versioned and never overwritten. Because accuracy is chosen per phase, one plan can mix cheap medium phases and high-accuracy phases.

## Phase leader responsibilities

A meta run creates one phase leader per phase (created, then destroyed at phase end; a backtrack reopens the leader on that phase as a new pass). The phase leader:

- validates the route profile entries for its phase;
- performs the pre-flight boundary/dependency check before any stage writes code;
- runs `stage-0-alignment` first when `accuracy: high`;
- places exactly one worker order at a time to the Recruiter for LLM-run stages or anomaly verifiers, and reads the worker's `result.json`;
- runs deterministic changed-scope Stage 3 seam/contract checks and Stage 5 merge/check/log/cleanup commands itself, writing typed stage evidence instead of hiring an ordinary LLM worker;
- injects the stage instructions, route details, worktree branch, deterministic merge timing, the non-delegation rule, and the specialist phone book — the VERBATIM output of `just upagent-specialists` (merged roster + exact consult mechanics + the mandatory-consult rule) — into `instructions.md`;
- records evidence and stage outcomes in `phase-status.md`;
- enforces stage-level `revisit` backtracking (replay forward, increment try) and the `stage_try_budget` → advisor → human ladder;
- enforces loops back to Stage 1 when Stage 2 raises blocking audit findings;
- enforces the Stage 3/4/5 merge point from `merge_back_at`, without treating ordinary Stage 4 as a shared-environment or deployment acceptance gate;
- enforces Stage 5 finalization, cleanup, green checks, and log review;
- writes `phase-result.json` (verdict plus `revisit:[phase-ids]` when it gives up).

A phase leader may consult an advisor when configured. The advisor does not write files, run commands, or create agents. After writing and validating `phase-result.json`, the leader's literal last action before idle is to print `PHASE_RESULT: phase-<id> verdict=<passed|failed|blocked> pass=<n>` to its own pane (map a detailed `partial` file verdict to `blocked` in the marker).

### Lifecycle monitors

Normal delivery is deterministic and uses no LLM polling loop. Python observes pane existence, process identity, cwd, result validity, agent status, and deadlines. At configured inactivity/anomaly checkpoints, the Recruiter may hire one fresh low-cost checker to interpret a bounded evidence snapshot. That checker returns one typed advisory assessment and exits; it never polls continuously, advances work, or controls a pane.

The deterministic phase controller starts the phase leader behind a filesystem gate, releases it once the durable `phase-start.json` receipt exists, and health-checks it. There is **no standing phase watchdog** (the receipt's `watchdog` block reads `not-configured` by design): the TUI blocks inside `upagent-phase-await` on that receipt, whose deterministic reconciliation correlates the exact leader, descendant request records, and `phase-result.json` — returning typed `completed`/`blocked`/`leader-missing`/`leader-stalled`/`inactivity-checkpoint` events. Fresh one-shot checkers interpret ambiguous inactivity evidence and exit; urgent unacknowledged events escalate to the human via `herdr notification`.

## Five-stage phase protocol

### Pre-flight — dependency/import safety

Before Stage 1, inspect the target module/layer and dependency graph. Look for `dependencies.yaml`, `build-dependencies.yaml`, repo-documented equivalents, or derive a graph from local import/build metadata. If a circular dependency involving the target module and parent/dependent layers is confirmed, stop with:

```text
CRITICAL FAIL: Circular dependency detected. Human intervention required before entering Stage 1.
```

Missing canonical graph files are not by themselves fatal. The runner must attempt dynamic graph derivation before blocking.

Before launching Stage 1, create or select the temporary worktree branch using the route template. The default template is:

```text
tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}
```

Refuse to reuse an existing dirty temp worktree. Record the temp worktree path, branch, base commit, and current main branch identity in evidence. The worktree path becomes the `cwd` on each stage's order.

### Stage 1 — LLM implementation plus focused local tests on the temp worktree

This is the implementation stage. The worker hired for the stage writes/updates focused local tests and production code in one TDD loop on the temporary worktree branch:

1. write or update the relevant unit test;
2. verify expected failure when practical;
3. write the minimal real implementation;
4. run the unit tests to pass;
5. refactor without widening scope.

No hardcoding, bypassing validation, empty stubs, or goal cheating just to pass tests.

### Stage 2 — independent semantic/adversarial audit of Stage 1 code on the same temp worktree

Run an independent hostile reviewer against the files modified in Stage 1 on the same temporary worktree branch. It checks signature mismatches, unused/dead code, goal cheating, and **unused intake / accepted-but-ignored inputs**.

The Stage 2 auditor must fail hard on any newly accepted input that does not influence real behavior. This includes newly added function parameters, destructured fields, request/schema fields, configuration/env values, command-line options, validation parameters, and fixture values. Every newly accepted input must affect validation, control flow, transformation, persistence, or downstream calls — otherwise the coder either removes the intake or wires it into real behavior. Do not allow hardcoding, bypassing, stubbing, or fake intake just to satisfy tests.

Use a multi-angle audit rather than a single generic unused-variable scan:

- start from the phase diff and enumerate newly accepted inputs;
- use AST-aware inspection where available to trace identifiers from intake sites to real usage sites;
- cross-check lint, type, and static-analysis signals for unused variables, unused parameters, unused imports, unreachable branches, and dropped arguments;
- trace directly affected public interfaces and call-sites for signature expansion where callers pass values that callees ignore;
- semantically inspect tests and implementation for assertions that pass only because inputs are accepted but not validated, transformed, persisted, or propagated.

Intentional unused inputs are allowed only when explicit and auditable: underscore-prefixed names, framework/interface-mandated parameters, or a short explanatory comment. These markers never excuse goal cheating; if the phase goal requires the input to matter, it must matter.

- `VERIFICATION_PASSED` advances to Stage 3.
- Blocking findings loop back to a new Stage 1 attempt with the raw findings (the worker returns `verdict=failed`, `revisit=[stage-1-implementation]`).
- Non-blocking notes are reported but do not fail the phase.
- Each blocking unused-intake finding must name the input, where it is accepted, the expected behavioral role, evidence that it is ignored, any affected call-site/public surface, and the recommended fix.

### Stage 3 — deterministic local seam/contract checks

If `merge_back_at` is `stage-3-integration-acceptance-seams`, merge the temporary worktree branch back to main at this stage and run Stage 3 from main. If that merge updates refs without touching a dirty primary checkout, immediately reconcile the primary checkout/index before continuing: `git checkout HEAD -- <phase-touched-files>`, using only the recorded phase-owned manifest. Otherwise, continue on the temporary worktree branch.

Stage 3 is deterministic changed-scope local seam/contract verification, not a second semantic reviewer and not a shared-environment acceptance stage. The leader derives the changed scope from the phase diff, dependency metadata, and phase-owned manifest, then runs only declared local commands that prove affected boundaries. Suitable checks include:

- local contract tests;
- component or package seam tests;
- hermetic integration tests that do not deploy or mutate shared services;
- targeted build/type checks required to exercise the touched public boundary.

Do not write tests for their own sake, do not rerun broad generic lint already owned by Stage 5's route-owned `green_checks`, and do not deploy or mutate shared infrastructure. If no public/deep-module seam changed, record the reason and pass the stage. A fresh LLM verifier is hired through the Recruiter only when a deterministic command fails, the output is ambiguous, or attribution to Stage 1 versus pre-existing state is unclear. Missing production wiring fails back to Stage 1; residual cross-slice production work belongs in an explicit integration-construction phase with its own Stage 1 implementation and independent Stage 2 audit, not in Stage 3 verification.

### Stage 4 — ordinary-phase shared acceptance is out of scope

If `merge_back_at` is `stage-4-upstream-dag-verification`, merge the temporary worktree branch back to main at this stage. If that merge is ref-only, immediately run `git checkout HEAD -- <phase-touched-files>` in the primary checkout using the recorded phase-owned manifest before continuing. If the branch was already merged in Stage 3, verify main contains the expected change. Otherwise, continue on the temporary worktree branch.

For ordinary phases, Stage 4 must not become a per-phase shared-environment, deployment, CI, upstream-DAG, or global acceptance stage. Broad shared acceptance belongs once at candidate level, after planned phase work has accumulated into a candidate branch and the runner has yielded custody to the route-owned candidate-level finalization/gate. If that gate fails later, it returns evidence to the responsible phase and the run replays forward.

Real residual cross-slice production wiring is construction, not verification. Add it as an explicit integration-construction phase only when the plan identifies production code still to write across slices. That phase uses the normal Stage 1 implementation plus Stage 2 independent audit path, then the same deterministic Stage 3/5 checks. Non-ordinary variants keep their explicit contracts; in particular, IaC phases still use the Stage 3 plan/approval table, TUI-owned apply receipt, and Stage 5 finalization described in `/phase-leader`'s "IaC phases (kind: iac)" section and the meta-plan format's "IaC phases (`kind: iac`)" section.

### Stage 5 — finalization, green checks, log review, and cleanup

Stage 5 always runs.

- If `merge_back_at` is `stage-5-finalization`, merge the temporary worktree branch back to main now; after a ref-only merge, immediately run `git checkout HEAD -- <phase-touched-files>` in the primary checkout using the recorded phase-owned manifest.
- If the branch was already merged in Stage 3 or Stage 4, verify main contains the expected change.
- Run exactly the effective `green_checks` from `finalization_defaults` plus any phase-level additions/overrides, scoped to this phase's configured finalization contract. The leader does not infer or branch on later candidate-level ownership.
- Route authors decide the command set before execution: when an explicit later candidate-level finalization/gate owns repository-wide test/lint/static-analysis, omit those generic commands from per-phase `green_checks`; otherwise retain the repository's normal green checks.
- Inspect the effective `log_checks` sources for hidden failures. Treat obvious fatal/error/traceback/uncaught/deploy-failure patterns as hard failures unless an explicit allowlist explains them.
- Destroy/prune the temporary worktree and temporary branch only after merge, green checks, and log review succeed.
- Write final evidence: merge point, main commit, cleanup actions, green-check output, and log-review summary.

If merge, green checks, log review, or cleanup fails, preserve evidence, keep the temporary branch when needed, and return `failed` or `blocked`. Never silently clean up and claim success.

## Rollback safety

At phase start, record a Git baseline: branch/worktree identity, status, and phase-owned file manifest.

- Temporary worktree branch: a failed deterministic check may use a hard reset after logs are saved, scoped to the phase-owned temporary worktree only.
- Main branch: inspect whether uncommitted changes include files outside the phase-owned manifest. The scoped post-ref-only-merge checkout is allowed only after that merge and only for recorded phase-owned paths; ask the human before any broader destructive rollback.
- Stage 5 cleanup is not allowed until merge/final checks/log review have succeeded.
- Because passes are forward-only, a `revisit` never rewinds merged history. A true revert is a human decision surfaced through the `stop-ask-human` path, never an automated step.

Never reset unrelated human or agent work without an explicit safety check and human gate.

## Result evidence

`phase-result.json` and the phase report should carry:

- runner name and phase id;
- phase lead `llm_profile` and `agent`;
- `accuracy` and, when high, the stage-0-alignment outcome;
- `merge_back_at` value and actual merge stage;
- temporary worktree branch/path and cleanup result;
- worker-stage evidence: stage id, `llm_profile`, `agent`, `order_id`, tries, final verdict, and `full_log` pointer;
- deterministic-stage evidence from `controller-result.json`: stage id, `runner: controller` or equivalent marker, commands, exit codes, log/evidence paths or bounded excerpts, tries, and final verdict;
- no synthetic `order_id`, worker `result.json`, or worker `full_log` for deterministic Stage 3/5 controller actions; if an anomaly verifier was hired, record that verifier as separate worker evidence;
- advisor status when applicable;
- dependency graph source;
- commands run and evidence paths/log excerpts;
- Stage 4 ordinary-phase deferral or non-ordinary variant result;
- Stage 5 green-check and log-review result;
- rollback or cleanup actions;
- the pass number and any `revisit:[phase-ids]` on a non-passing verdict.
# Shared meta-runner handoff protocol

Shared handoff contract for the meta runner. Deliberately generic and public. Baked into the workflow — it does not depend on any installed handoff skill or slash command.

Every worker (a stage/role agent) is hired fresh, does one job, then **writes a short handoff before its pane closes** so the next worker of that role — or the phase leader — resumes with immediate context instead of a cold start. Fresh context avoids drift; the handoff carries only the distilled signal forward.

## Where

Canonical per-role path inside the run tree, **versioned** (never overwritten):

```text
phases/<phase-id>/handoffs/<role>-vN.md
```

On spawn, a worker reads the latest `<role>-v*` for its role — or the full trail when it needs to see how the work evolved across passes.

## What (keep it to ~10 lines)

- Role · phase · pass.
- What I did — 1–3 lines.
- Key decisions + why — 1–3 lines.
- Alignment to the original plan — on-track, or the exact deviation and why.
- Open items / risks for the next worker.
- Pointers to artifacts (`plan.md`, changed files, this stage's `result.json`) — pointers, not re-summaries.

It is immediate context, not a report. Summarize your **actions and decisions**; **point** to the plan and code rather than paraphrasing them (a paraphrase drifts from the source).

## When

- Every worker writes its handoff as its last step, before the Recruiter closes its pane — alongside its `result.json` and `compacted.md`, whether the outcome is a pass, a fail, or `BLOCKED`.
- The phase leader reads the relevant handoffs before ordering the next stage's worker.
# /tui-control

Run a checked `plan.md + route.yaml` pair end to end through the TUI controller. This is the kickoff command — the **TUI agent** — the one pane you talk to. It sets up the run cockpit, loops the plan's phases, creates one phase leader per phase, and applies phase-level backtracking and escalation. It stays small: it decides only whether a phase re-runs or the run continues, and delegates every hard evaluation to an advisor worker (when configured) rather than doing the work itself.

## Invocation

```text
/tui-control --plan <plan.md> --route <route.yaml> [--run-tree <exact-dir> | --run-root <parent-dir>] [--slug <name>] [--start-phase <N>] [--max-phases <N>]
```

- `--plan <plan.md>` — canonical meta plan. Routing stays out of the plan body.
- `--route <route.yaml>` — route profile with `llm_profiles`, inline `agent` names, per-phase `accuracy`, and optional `advisor_profile`/budgets.
- `--run-tree <exact-dir>` — exact, already-created run directory containing the supplied `plan.md` and `route.yaml`. `just run-start` always supplies this; use it directly and never create another dated/slug directory around it.
- `--run-root <dir>` — optional root under which the run tree is written. Defaults to the repo's configured work-log root, or a local `./.runner-runs/` when none is configured.
- `--slug <name>` — optional run name. Defaults to a slug derived from the plan title.
- `--start-phase <N>` — optional phase to start from. Default `0`.
- `--max-phases <N>` — optional safety cap.

`--run-tree` and `--run-root` are mutually exclusive. All required flags must be present. Fail loud rather than guessing.

## Pre-flight

1. Verify `HERDR_ENV=1`. If not, stop with: `ERROR: /tui-control must run inside a Herdr-managed pane.`
2. Run `herdr pane list` to identify the current pane — this pane is the **tui-agent** (the top, full-width pane of the cockpit; the one the human talks to). Do not control Herdr from outside Herdr.
3. Validate the installed Herdr command surface (`herdr workspace list/create`, `herdr pane list/split/run/read/close`, `herdr wait output`, `herdr wait agent-status`). Adapt only after validating any local syntax differences.
4. Read the plan and route profile and run the same runnable gate used by `/cc-convert --herdr` and `/do-convert --herdr`. If it fails, stop **before** creating any workspace and tell the user to rerun the matching converter or fix the files. The check must confirm: canonical plan shape; `llm_profiles` defined; every phase to run has `lead.llm_profile`, `lead.agent`, `merge_back_at`, and its stage entries; `stage-0-alignment` present iff `accuracy: high` or `max`; when `max`, stage-2 also names a `second_llm_profile` on a different harness or model; worktree branch template, green checks, and log checks configured; all referenced profiles exist; each named agent resolves; Stage 2 (and stage-0's audit when high or max) independent from Stage 1.
5. Resolve the run tree without guessing. With `--run-tree`, require that its resolved path is the common parent of the supplied `plan.md` and `route.yaml`, use that directory exactly, and treat those files as already frozen. Without it, resolve `<slug>` and `<run-root>`, create `<run-root>/<date>/<slug>/`, and freeze the originals into it (`plan.md`, `route.yaml`, and `research.md` if present). Initialize `run-status.md`. The run-tree `route.yaml` is the **single live route copy** for this run; the origin passed by `--route` is historical/read-only after this point. All mid-run route changes apply only to the run-tree copy.
6. Record a Git baseline: branch/worktree identity, status, and phase-owned file manifest if the plan provides one.

## Cockpit + services setup

The default runtime topology is ONE unified workspace — services and the run share it as role
tabs (`just upagent-up --separate-workspaces` restores the two-workspace layout):

```text
ws: upagent                    ← everything, as tabs (single-workspace default)
  tab: services                ← optional status surface · plan-agnostic
    └── recruiter  (UpAgent)        deterministic lifecycle and durable mailboxes;
                                    ordinary work AND specialist consults, one door
  tab: control                 ← primary view
    ├── tui-agent              you talk to the TUI
    └── phase-leader           current phase owner
  tab: workers                 ← active work
    └── stage UpAgent workers
  tab: oversight               ← inspect when needed
    ├── account managers
    └── one-shot checkers

--separate-workspaces          ← legacy layout: same tabs, two workspaces
ws: <slug>                     ← one run cockpit (control/workers/oversight tabs)
ws: shared-services            ← recruiter, peripheral
```

Concurrent runs in the single-workspace default share the role tabs (each adds its own
tui-agent/leader panes); start heavy parallel runs with `--separate-workspaces` when you want
per-run isolation. The mode is chosen once at `just upagent-up` and inherited by `just run-start`.

1. The cockpit is the workspace holding this (tui-agent) pane. `just run-start` has already created and health-checked this TUI. Read `<run-tree>/control/plan-start.json` and acknowledge `ready` (its `watchdog` block says `not-configured` by design — there is no standing plan-lifecycle-watchdog in coordination v2; a legacy run may still show `ready-degraded`, which is equally continuable). Liveness does not come from an observer agent: this TUI hears every phase condition — completion, blocked, crash, stall, quiet — as the typed return value of its own blocking `upagent-phase-await` call, and urgent unacknowledged events additionally escalate to the human through `herdr notification`. **The TUI has no authority to create, launch, prompt, adopt, or replace a watchdog agent or a phase leader.** Its sole phase-start authority is the controller command in the phase loop below; never attempt an ad-hoc monitoring repair.
2. Optionally run `just upagent-up` to ensure the visible **UpAgent Recruiter** services pane and persist its presentation state, then inspect it with `just upagent-status`. Bring-up starts no daemon and is not a prerequisite: request commands self-heal missing service state and each imports current canonical source. The pane is status/observability only; requesters use `just upagent-request` / `upagent-await`, never its shell. Mutating lifecycle commands reconcile dead/expired leases opportunistically. The roster still owns all pre-hardened harness launch templates. Specialist consultation is an ordinary UpAgent order placed with `just upagent-consult`, and the phone book every stage brief embeds comes from `just upagent-specialists`.
3. Every order includes a `requester` (`id`, `kind`, `address`) and a caller-stable `request_id`; the Recruiter assigns/scopes its durable identity. Each phase leader uses its own pane as requester and `cockpit_pane`. The Recruiter defaults to direct lifecycle: Python validates configuration, atomically starts the worker, returns `worker-healthy` after process/agent/cwd proof, and publishes durable requester mailbox events consumed by `upagent-await` / `upagent-await-any`. A roster may opt into `management.mode: dedicated` for the historical Account Manager pane. The worker itself receives no controller addresses.
4. Multiple Remote Control TUI sessions can drive the same run; this is a warning-only last-writer check, not a lock. Before each route edit, read the run-tree `route.yaml` marker `# last-edited-by: <session-id> @ <iso-ts>`; before writing, warn if it changed since that session last read it. Update that marker on every edit. Never put this marker in the origin route.
5. Do not start an ad-hoc LLM result poller, and never create a standing watchdog agent. Observability is layered deterministically: the blocking awaits (`upagent-phase-await` here, `upagent-await`/`upagent-await-any` in the leader) reconcile durable state against live Herdr state every sweep and return `leader-missing`/`leader-stalled`/`inactivity-checkpoint` events; the Recruiter launches fresh **one-shot** LLM checkers only at configured inactivity/anomaly checkpoints; urgent unacknowledged events escalate to the human via `herdr notification`. Phase startup is one Python transaction invoked through `just upagent-phase-start`; do not reproduce its pane operations manually. A leader startup failure is terminal and must be reported.
6. Keep cockpit geometry deterministic and role-based. The launcher names the TUI tab `control`;
   phase leaders stay there. The Recruiter moves active stage workers to `workers` and Account
   Managers (opt-in) and one-shot checkers to `oversight` before publishing their addresses.
   Role tabs are created lazily from the first live pane, not with empty placeholder shells.
   Workers split right; support roles split down. Resizing is bounded and presentation-only:
   report a warning and leave the agent in its source tab if Herdr cannot move or resize it, but
   never fail or alter a healthy worker lifecycle because of cockpit geometry.

## Retained worker interaction

For an explicitly controller-owned ad-hoc task, this managed TUI may use `/upagent-run --duration-minutes <1..120> --keep-open <task>`. The runner owner token file is the caller proof. Follow the checkpoint loop: inspect the actual diff/tests, send authenticated `review-continue` feedback to the same live worker, and use `review-release` only when accepted; then await its terminal receipt. The initial duration covers coding plus review and may be extended through the existing timeout decision. Do not use this to bypass the phase transaction: a retained stage worker remains owned by its phase leader, not by this TUI, and ordinary workers remain one-shot.

## Phase loop

For each phase, in canonical order starting at `--start-phase` (respecting `--max-phases`):

1. **Start one complete phase transaction.** Run exactly `just upagent-phase-start <run-tree>/route.yaml <run-tree> <phase-id> <pass-number>` from the TUI pane. This is mandatory, not guidance. Do not call `herdr pane split`, `herdr agent start`, `herdr pane run`, launch an LLM, or `just upagent-request` yourself for phase startup. Do not send `/phase-leader` to any pane yourself. Those actions create an unmanaged phase and are a protocol violation. Python validates the route and roster, starts the leader behind a filesystem gate, releases and health-checks the leader, updates `active-leader-panes.json`, and atomically writes `<phase>/pass-<n>/control/phase-start.json`.
2. **Require a terminal startup response.** Continue after `PHASE_STARTED` with a live `leader_pane` and `state: ready` (the receipt's `watchdog` block is `not-configured` by design; a legacy `state: ready-degraded` receipt is equally continuable). Any command failure or missing leader means the phase never started; report the recorded cause and stop. The controller closes a gated leader on leader-start failure, but never destroys a previously owned live leader.
3. **The controller hands the phase to the leader.** The gated launch carries exactly one `/phase-leader --phase <phase-id> --plan <run-tree>/plan.md --route <run-tree>/route.yaml --run-root <run-tree>` assignment. The leader owns stages, Recruiter orders, stage-level backtracking, and `phase-status.md`.
4. **Wait inside the deterministic await — never by watching panes.** After `PHASE_STARTED`, block in exactly one repeated command:

   ```bash
   just upagent-phase-await <run-tree>/phases/<phase-id>/pass-<n>/control/phase-start.json <after> [timeout-ms]
   ```

   This is plain Python — no LLM turns are burned while blocked. It multiplexes the phase event journal, the leader's typed publications, the authoritative `phase-result.json`, and live Herdr state, then prints exactly one typed JSON event. Handle that event by `kind`, acknowledge it only after parsing (`just upagent-phase-ack <receipt> <event_id>`), and re-await with `after=<that event's sequence>` after every nonterminal event. An unacknowledged actionable event is redelivered by the next await, so a lost turn replays instead of disappearing. Never use `agent-status=done`: that marks a turn, not a phase. Never derive a verdict from pane scrollback; a `PHASE_RESULT` pane marker is display-only.

   | `kind` | terminal | TUI action |
   |---|---|---|
   | `completed` | yes | Validate `phases/<phase-id>/phase-result.json`, ack, record in `run-status.md`, advance. |
   | `failed` | yes | Ack; apply phase-level backtracking from the event/result `revisit` list. |
   | `blocked` | yes | The attempt is over. Read the evidence paths and `phase-result.json`, ack, destroy the leader, then decide: replay the phase as a fresh pass with the answer baked into its inputs, or stop for the human. |
   | `needs-input` | no | Advisory only until the owner-command channel lands: note the question in `run-status.md`; ack; re-await. A leader that cannot continue without the answer publishes `blocked` instead. |
   | `decision-required` | no | `requested_action: iac-approval` ⇒ run the IaC approval and apply flow (below). Otherwise a descendant hit a work cap: `just upagent-respond … extend/cancel`; ack; re-await. |
   | `worker-warning` | no | Note in `run-status.md`; act only if it changes phase risk; ack; re-await. |
   | `leader-missing` | no | Verify the recorded evidence; clean up the dead leader mapping and replay the phase as a new pass, or stop for the human. |
   | `leader-stalled` | no | Durable state contradicts live status: inspect once; if truly stranded treat like `leader-missing`, else ack and re-await. |
   | `inactivity-checkpoint` | no | Quiet too long: request one bounded checker/inspection; ack; re-await. |
   | `advisory` | no | Read the observer evidence; act only when it changes risk; ack; re-await. |
   | `startup-ready` / `startup-degraded` | no | Record observability state; re-await. |
   | `soft-timeout` | no | Extend or cancel within the decision window; ack; re-await. |
   | `hard-timeout` | yes | Enforced stop: record the enforcement evidence; treat the phase as failed. |
   | `cancelled` | yes | Record who cancelled and why; stop or replay per authority. |
   | `await-heartbeat` | no | Quiet and healthy: re-await immediately and silently — never narrate heartbeats to the human. |

5. **Read `phase-result.json` for detail.** The durable file supplies the verdict and evidence; the event is the wake-up, not the record.
6. **Destroy the phase leader unconditionally.** After result/evidence handling, close the recorded leader pane and remove its mapping. A replay creates a fresh leader and a fresh `phase-start.json` receipt for the new pass's await.
7. Append a `run-status.md` line for the phase outcome (phase id, pass number, verdict, and any `revisit`) before acting on it. On every start/pass/fail/backtrack — or hourly if unchanged — delegate a minimal static HTML snapshot to a small disposable, non-stage helper. Give it only the status/result paths; it returns only the artifact path.

## Phase-level backtracking (forward-only)

The TUI backtracks phases; the leader backtracks stages. Both replay **forward, in order** — nothing is reverted.

- A passing `phase-result.json` advances to the next phase.
- A failing `phase-result.json` carries `revisit: [phase-ids]`. The TUI replays from the **earliest** pointed phase forward as a new pass, incrementing the pass count, first applying the same unconditional prior-leader cleanup and then creating a fresh leader for each replayed phase. Already-good later phases are only re-run if they are pointed to.

## Escalation ladder

Applied at the phase level, mirroring the leader's stage-level ladder:

```text
phase fails → phase-result.json.verdict=failed, revisit=[phase-ids]
TUI: replay from earliest revisit phase forward (new pass); increment pass
  phase_pass_budget hit (default 3) →
    advisor_profile set?  place an advisor order via the Recruiter (context = run-status.md);
                          advisor result.json: verdict=passed, decision = continue | loop | stop-ask-human
    no advisor_profile →  stop-ask-human
stop-ask-human → the TUI halts and surfaces status to the human
```

- The advisor is hired like any worker — placed as an order to the Recruiter on `advisor_profile`, reading `run-status.md`, writing no code and running no commands. A phase-level advisor order sets `phase_id` to the failing phase, but its `stage_id` must still be a **recognized** stage id (the order contract rejects anything outside the six) — a phase has a `phase_id`, not a `stage_id`, so there is nothing "phase" to reuse. Use the fixed convention `stage-5-finalization` (the whole-phase judgment stage) as the `stage_id`; never write a `phase_id` into `stage_id`, and there is no `stage_id: "advisor"`. The TUI knows it placed an advisor order and reads `result.json.decision`. The TUI stays small: it never performs the hard evaluation itself when an advisor is configured.
- The advisor worker writes a normal `result.json` with `verdict: passed` **plus** the optional `decision` field, one of the exact tokens `continue`, `loop`, or `stop-ask-human` (the contract's `ADVISOR_DECISIONS`). The TUI reads `result.json.decision`, not a special verdict: `continue` accepts the phase as good enough and advances; `loop` grants another pass (reset/extend the budget); `stop-ask-human` halts and surfaces to the human.

## IaC approval and apply (kind: iac phases)

A `decision-required` event tagged `iac-approval` means one terraform layer finished planning and needs the human before anything is applied. The TUI runs this flow itself; the apply is never delegated:

1. Read the event's evidence paths: the rendered table and the plan output it summarizes. Print the table to the human VERBATIM — never summarize it away.
2. Show the human exactly what will run: `cd <absolute pass-dir>` on one line, the apply (or destroy) command on the next. Never a bare relative path, never an implied cwd.
3. Collect the decision. When the table shows "Destroy total to confirm: N" with N above zero, the human approves by typing that exact number; any other answer is a decline. A zero-destroy plan accepts a plain yes.
4. Write `<pass-dir>/iac/approval.json`: `{"approved": true|false, "plan_sha256": "<sha256 of the plan output the human reviewed>", "cwd": "<absolute pass-dir>", "command": "<command shown>", "destroy_total_confirmed": <n>, "by": "human", "at": "<ISO timestamp>"}`. The sha records what the human reviewed — there is no artifact it binds to.
5. On approval, apply DIRECTLY in this pane with a FRESH plan — `cd <pass-dir> && terraform apply 2>&1 | tee <pass-dir>/iac/apply.log` (tofu likewise) — then write `<pass-dir>/iac/apply-receipt.json`: `{"command": "<what ran>", "cwd": "<pass-dir>", "exit_code": <n>, "log_path": "<pass-dir>/iac/apply.log", "applied_at": "<ISO timestamp>"}`. The receipt is the paper trail; the leader validates it and records stage-4 from it. NEVER save a plan to a file and apply that file (`plan -out=<artifact>` then `apply <artifact>`) — that pattern is banned kit-wide; apply always re-plans fresh so what runs is never an opaque saved artifact.
6. Ack the event and re-await. The leader sees the durable files and finishes the phase.
7. On decline, write the approval file with `approved: false`, ack, and expect the phase to end `blocked`.

IaC layers run strictly in order — a later layer's plan is only truthful after the earlier layer's apply. Phases sharing a route `parallel_group` token are the explicit escape hatch (urgent fixes on genuinely independent stacks) and may be started together; the human owns that risk.

## Forward-only — no revert automation

This is a Ralph-style forward loop. Nothing already produced is discarded to go back, and no merged history is rewound. A **true revert** (throwing away merged work) is a major decision — the TUI **STOPs and asks the human**; it is never automated.

## Completion

When every in-scope phase has a passing `phase-result.json` (or the human has resolved a STOP), write a final `run-status.md` summary: the phase order actually run, passes and backtracks per phase with reasons, the run tree root, and the overall verdict. The run tree under `<run-root>/<date>/<slug>/` is the durable record.

After that summary exists, you **MUST** publish the terminal lifecycle fact through the controller:

```bash
just run-session-finish <exact-run-tree> succeeded
```

Use `stopped` instead of `succeeded` for any non-successful terminal outcome. This command is
mandatory and must run before you wait for support panes to close or print the final message. Do
not write `control/run-terminal.json` yourself. If the command fails, report that exact lifecycle
failure and do not claim the workspace is safe to close. The marker is the run's only terminal
authority (any in-flight legacy watchdog also retires from it); quiet panes and completed turns
are never completion authority.
The launcher passes `RUNNER_OWNER_TOKEN_FILE` to this TUI, pointing at the run's hashed 0600 file
under the same-user runtime token directory; do not paste raw owner tokens into shell command lines.

Keep the final TUI message deliberately short. After writing the durable summary, wait a bounded
interval for every managed run pane except this TUI to close. Then use exactly one of these forms:

```text
SUCCESS — Everything succeeded. Safe to close this run's panes.
Details: <absolute run-status.md path>
```

```text
SUCCESS — Everything succeeded. Cleanup is still finishing; leave this run's panes open.
Details: <absolute run-status.md path>
```

```text
STOPPED — This run did not succeed. See: <absolute run-status.md path>
```

"This run's panes" is mode-aware on purpose: in the single-workspace default it means the run's
tabs only — the `upagent` workspace and its `services` tab stay up for the next run — while in
`--separate-workspaces` mode the whole `<slug>` workspace is safe to close. Never tell the human
to close a workspace that still hosts the services.

Do not print a stage-by-stage recap, model list, commit narrative, verification transcript, or
implementation caveats in the final TUI message. Those details belong only in `run-status.md` and
the run tree. The terminal message exists solely to make outcome and close-safety unmistakable.

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`.
2. Canonical plan body stays clean; the route profile owns profiles, agents, accuracy, and budgets.
3. Do not auto-convert at execution time. `/tui-control` only runs an already-runnable `plan.md + route.yaml`.
4. Stage work is done by workers hired through the Recruiter — never by native subagents or Claude team mode. The only exceptions are small, disposable non-stage helpers for watchdog monitoring or static status rendering; they perform no stage work, do not delegate, and return only their alert or artifact path.
5. The run tree files (`phase-result.json`, `result.json`, the `*-status.md` logs) are the source of truth; pane scrollback is live-view only.
6. Stay small: decide re-run/continue and delegate hard calls to the advisor when configured.
7. Do not push, deploy, reset, or revert unless the plan and rollback policy explicitly allow it; a true revert stops for the human.
