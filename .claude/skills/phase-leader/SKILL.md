---
name: phase-leader
description: 'Run one canonical plan phase as the phase leader, sent to a cockpit pane by `/tui-control`. Requires `HERDR_ENV=1`. It validates the phase route, runs the dependency/import safety check, then places one blocking work ORDER per stage through the UpAgent Recruiter''s durable ledger. Each fresh worker writes one lease-private result; the Recruiter validates it, verifies pane cleanup, publishes the public result plus receipt, and only then releases the leader. No LLM result poller or pane-text DONE signal is load-bearing. The leader follows the shared five/six-stage worktree lifecycle, deterministic merge point, independent audit, stage backtracking, budget escalation, `phase-status.md`, and authoritative `phase-result.json`. Forward-only: never reverts merged history — a true revert escalates to the TUI.'
argument-hint: --phase <phase-id> --plan <plan.md> --route <route.yaml> --run-root <dir>
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
# /phase-leader

Run one phase as the **phase leader**. This command is sent to a cockpit pane by `/tui-control`, once per phase. The leader runs LLM implementation, audit, verifier, advisor, and consult work by placing work orders with the UpAgent Recruiter — never by spawning a native or nested subagent — and runs ordinary Stage 3 seam checks and Stage 5 finalization as deterministic controller actions with typed evidence. It applies stage-level backtracking and the stage-try budget ladder, maintains `phase-status.md`, and writes `phase-result.json` (the source of truth the TUI reads).

## Invocation

```text
/phase-leader --phase <phase-id> --plan <plan.md> --route <route.yaml> --run-root <dir>
```

All four flags are required. Fail loud on any missing or unreadable path.

- `--phase` identifies one canonical plan phase, for example `phase-0` or `0`.
- `--plan` / `--route` are the run-tree frozen copies. During a run, the run-tree `route.yaml` is the only live route: a human profile addition edits that copy only, never the origin.
- `--run-root` is the run tree root `<run-root>/<date>/<slug>/` that `/tui-control` created; this leader writes under `phases/<phase-id>/`.

## Pre-flight

1. Verify `HERDR_ENV=1`, else stop: `ERROR: /phase-leader must run inside a Herdr-managed pane.`
2. **Inspect controller ownership without making monitoring a work gate.** When `$UPAGENT_PHASE_START_RECEIPT` names a readable `phase-start.json`, record its state (`ready`; the `watchdog` block reads `not-configured` by design — the TUI's blocking `upagent-phase-await` on that same receipt owns liveness, and this leader publishes to its journal via `upagent-phase-publish`). If the variable or receipt is missing or mismatched, append `phase-monitoring: degraded — <reason>` to `phase-status.md` and continue the phase. Do not create, adopt, or repair a watchdog yourself — none exists to repair. Monitoring failure must never become an infinite wait or prevent plan work.
3. `just upagent-up` is optional: it only persists the visible Recruiter status pane and starts no daemon. Request commands self-heal missing service state and import current canonical source. The pane is not a command mailbox. Determine this leader's OWN pane id — the `cockpit_pane` stamped on every order — from `$HERDR_PANE_ID` (or `herdr pane current`). Do **not** infer it from UI focus.
4. Validate this phase's route entry:
   - `lead.llm_profile` and `lead.agent` present;
   - `accuracy` is `medium` (default), `high`, or `max`; if `high` or `max`, `stage-0-alignment` is present, else it is absent; if `max`, stage-2 also names `second_llm_profile` referencing a profile on a different harness or model than the primary auditor;
   - all five base stage entries exist; each stage has `llm_profile` and `agent`;
   - `merge_back_at` is Stage 3, Stage 4, or Stage 5;
   - worktree branch template, green checks, and log checks configured;
   - each referenced profile exists and each named agent resolves in its harness/project context;
   - each profile's `model` is the harness-native id shape — claude: alias or full name (paired with `effort`); codex: bare model id such as `gpt-5.6-sol` (paired with `effort`, passed as `model_reasoning_effort`); pi: `provider/id[:thinking]` (the `:thinking` suffix is pi's effort). A `provider/…` model on a claude/codex profile, or a bare id on a pi profile, is a route error — fail loud, do not guess a translation;
   - Stage 2 (and, when high or max, stage-0's audit) is independent from Stage 1.
5. Determine the current **pass** number: count existing `pass-<p>/` dirs under `phases/<phase-id>/` and use the next one (start at `pass-1`). Read `phase-status.md` if it exists to see where a prior pass left off.
6. Run the pre-flight dependency/import safety check before any code stage (see the shared phase protocol). On a confirmed circular dependency, write a `blocked` `phase-result.json` and stop.
7. Create or select the temporary worktree branch from the route template and record its path, branch, and base commit. This path is the `cwd` on every stage order.

## Stage execution — worker orders plus deterministic controller stages

The leader runs the phase's stages in order — `stage-1` … `stage-5` for `accuracy: medium`, with `stage-0-alignment` first for `accuracy: high` and `max` (and, under `max`, a doubled stage-2 audit — see the five-stage lifecycle below). Stage 1, Stage 2, Stage 0 alignment workers, advisors, consults, and any anomaly verifier are work orders to the Recruiter. Ordinary Stage 3 seam checks and Stage 5 finalization are deterministic controller stages with typed evidence; Stage 4 is not an ordinary shared-environment or deployment verification stage.

When a worker is needed, the leader places exactly one work order to the Recruiter and reads back the worker's `result.json`:

```text
leader:    write pass-<p>/stages/<stage-id>/try-<m>/order.json + instructions.md   (order.cockpit_pane = this leader's pane)
leader:    just upagent-request <order.json path>       # returns only after verified startup
Recruiter: return REQUEST_ACCEPTED <json> with manager/worker addresses + control token
leader:    just upagent-await <order.json path>         # deterministic wait; no LLM polling
Recruiter: return ORDER_RECEIPT <json>, or REQUESTER_DECISION_REQUIRED at a work cap
leader:    read + validate pass-<p>/stages/<stage-id>/try-<m>/result.json
```

Every `instructions.md` the leader authors is a document a fresh worker consumes cold, so write it through the `writing-for-agents` skill: front-load what the worker does, end every step on a checkable and demanding completion criterion, inline what every path needs and point at what only some paths reach, and state the target behavior positively instead of listing prohibitions. The brief must let the worker understand what this leader is trying to convey about the overall agreed work, not just the mechanical step. Protocol-mandated verbatim blocks (result template, phone book, scope leash) are pasted unchanged; the writing rules govern the prose around them.

The public result file is the source of truth; the durable receipt is its wake-up record. `upagent-request` and `upagent-await` call the filesystem ledger directly, so shell keystrokes cannot interleave and no LLM file-polling watchdog is needed. Save the `control_token` from `REQUEST_ACCEPTED`. If `upagent-await` returns `REQUESTER_DECISION_REQUIRED`, inspect the evidence and use `just upagent-respond <order> <control-token> <nonce> extend <milliseconds>` or `... cancel 0`; then call `upagent-await` again after an extension. Only this recorded requester may make that decision. `ORDER ... DONE` pane text is display-only and MUST NOT drive a verdict.


With **more than one order in flight** (stage-0 sequences, an advisor beside a worker), do not stack single awaits — block once over the whole set:

```text
just upagent-await-any <timeout-ms> <cursor-json> <order.json> [<order.json> ...]
```

It returns one `AWAIT_EVENT <json>` tagged with the `request_id` that moved: a terminal receipt (`completed`/`failed`/`blocked`), a `decision-required` work cap, a `worker-warning`/`advisory` mailbox message, or `await-heartbeat` on quiet expiry. Echo the returned `cursor` back on the next call so handled messages never replay, and drop terminal orders from the set before re-awaiting. Requester notifications are durable mailbox events consumed by these awaits — nothing is ever pasted into this leader's pane.

`order.json` carries the contract fields (`order_id`, globally scoped `request_id`, `requester: {id, kind, address}`, `phase_id`, `stage_id`, `harness`, `model`, `agent`, `effort`, `cwd`, `instructions_path`, public `result_path`, `cockpit_pane`, optional `env`, optional positive `timeout_ms`, and optional `completion_policy: "requester_release"`). Use the retained policy only when this managed leader will inspect the worker's checkpoint and own the continue/release loop; ordinary stage orders remain one-shot. An explicit caller-selected duration may be at most 120 minutes, covers both coding and review, and does not reset at a checkpoint. Build `request_id` from the run slug, phase, pass, stage, and try; never use a phase-local name. Set the requester address to this leader's exact Herdr agent name or pane. The Recruiter creates a Dedicated Account Manager, validates its startup, creates a lease-private result path, atomically launches the worker, and proves its process/agent/cwd before reporting healthy. The worker writes only the private result. The Recruiter validates it, closes and verifies the owned panes, then publishes the public `result_path` and `receipt.json` atomically under the lease fence. Its lease records requester, manager, detached runner, worker, workspace, generation, control token, and expiry so a later mutating command can reconcile a dead owner without guessing ownership. The worker's `full_log` points to its harness transcript; it is not the result transport.

The Recruiter-generated final worker brief includes a copy-pasteable result template and its destination. The Recruiter MUST replace the two values below with the literal `order_id` and literal absolute `result_path` (the lease-private path); angle-bracket text is a protocol-writing marker, never text that may appear in a generated instruction. The worker MUST write exactly one result at that path and MUST copy the stated `order_id` exactly. It MUST NOT invent, generate, replace, or otherwise alter the order id.

```text
Write result.json exactly to: <literal result_path from this order.json>
```

```json
{
  "order_id": "<literal order_id from this order.json>",
  "verdict": "passed",
  "full_log": "<worker transcript path or session id>"
}
```

Use only `passed`, `failed`, or `blocked` for `result.json.verdict`; a failed result also includes a non-empty `revisit` list of recognized stage ids. `VERIFICATION_PASSED` is the Stage-2 audit-outcome concept, not a result-file verdict.

Every ordinary generated `instructions.md` also includes this terminal instruction: after `result.json`, `compacted.md`, and the handoff are durably written, exit the session. Do not stop at an idle prompt and wait for another instruction. The sole exception is an order with `completion_policy: "requester_release"`: its worker writes the lease-private checkpoint named by the Recruiter and returns to idle without terminal artifacts. This leader inspects the diff/tests, then uses `just upagent-review-continue <order> <control-token> <checkpoint> <checkpoint-sha256> <feedback-path>` to continue the same worker, or `just upagent-review-release <order> <control-token> <checkpoint> <checkpoint-sha256>` to release it; wait for checkpoints with `just upagent-review-await <order> <after> [timeout-ms]` and preserve the returned digest from inspection through the decision. Only release restores the terminal-artifact-and-exit instruction; do not abandon a retained worker without release or cancellation.

When a stage's route profile sets `scope_leash: true`, the leader copies the scope-discipline block from `.shared-llm/public/llm/pi/common/meta-plan/scope-leash.md` into that stage's `instructions.md` verbatim, before the result template. The flag on the profile decides; the leader never applies or skips the leash by model name.

A missing or malformed `result.json`, or a Recruiter error, is treated as a `blocked` stage — never silently retried as if passed.

Before ordering a stage that has a prior same-role handoff, the leader points the worker at the latest `phases/<phase-id>/handoffs/<role>-vN.md`. On a retry that re-investigates the same unresolved failure signature, the leader additionally requires a specialist consult before the worker forms a new hypothesis: provide the failure signature and prior ruled-out work, and require the worker to record the consult id, its UpAgent request id, and the answer/error path in `result.json`. Reading static specialist docs is not a substitute. Every worker writes its handoff, `compacted.md`, and `result.json` before its pane closes, then exits its session.

**Every stage brief carries the specialist phone book.** Once per phase (and again if the roster changes mid-phase), the leader runs `just upagent-specialists` and pastes its output VERBATIM into every stage's `instructions.md`. That block is the merged roster — the kit's base specialists plus this repo's own — with the mandatory-consult rule and the exact consult mechanics, so a worker never has to discover consulting on its own. Workers record every consult they make in `result.json` under `consults` (`{consult_id, specialist, request_id, answer_path}`; empty list when none applied), and set `requested_by` on each `consult.json` to their own `order_id` — that is what files the consult under the worker so the Recruiter can credit it back.

Consults travel through the UpAgent CLI, never through pane text. A consult is an ordinary UpAgent order: the same Recruiter, the same ledger, the same worker lifecycle as any stage worker. The pasted phone book already spells out the mechanics: write the question to a `consult.json` file, run `just upagent-consult <that file>`, and read the referenced `answer.json` when the command returns — it BLOCKS until the consult is terminal and prints one `CONSULT_RECEIPT` line, and an answer (real or failure) is always written. A brief MUST NOT tell a worker to deliver a consult with `herdr pane run`, `herdr pane send-text`, or any other pane injection: the Recruiter pane is a status surface holding a plain shell, so pasted prose lands in bash and is silently lost with no error back to the sender.

## Talking to the TUI — typed events, never pane text

The TUI waits inside `upagent-phase-await` on this phase's `phase-start.json` receipt. When this leader needs the owner before it can advance, it publishes one typed event into the phase journal instead of printing a plea into scrollback:

```text
just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT needs-input "<one-line question + evidence path>"
just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT blocked "<what cannot advance and why>"
```

The TUI's blocking await returns that event as its next turn — no idle-wait, no pane injection, no watchdog relay. Terminal outcomes still travel through `phase-result.json`; do not double-publish them. `blocked` is terminal: publishing it ends this attempt, so write `phase-result.json` with verdict `blocked` immediately after — the owner decides and replays as a fresh pass. There is no owner→leader answer channel yet (the command contract exists but nothing consumes it), so a question that must be answered before work can continue is a `blocked`, never a `needs-input`.

## Stage-0-alignment (accuracy: high or max)

When `accuracy` is `high` or `max`, before Stage 1 the leader orchestrates a sequence of **three separate non-delegating workers** — it does not hand this to one delegating agent:

1. **mini-research** order — research this phase against the original `research.md`.
2. **mini-plan** order — draft a mini-plan for this phase against the original `plan.md`.
3. **independent audit** order — a worker independent from `stage-1-implementation` audits the mini-plan against the big plan.

Misaligned ⇒ loop stage-0 (redo the mini-plan) within the stage-try budget. Unreconcilable ⇒ `blocked` (escalates). Stage-0 outputs are versioned, never overwritten.

## Five-stage lifecycle and merge timing

The leader runs the shared five-stage worktree lifecycle. It orders workers for LLM implementation/audit work and for anomaly verifiers; it runs deterministic local seam/finalization commands itself.

1. **Stage 1 — LLM implementation plus focused local tests** on the temporary worktree branch (TDD loop, no goal cheating).
2. **Stage 2 — independent semantic/adversarial audit** of Stage 1 on the same branch, including the hard gate for **unused intake / accepted-but-ignored inputs** and the **consult-receipt check**: compare the brief's specialist phone book against the Stage 1 `result.json` `consults` list — deciding in an area a listed specialist owns with no matching consult receipt is a blocking finding. Read `consults_verified` on the phase receipt, not the worker's own list: the Recruiter resolves every claimed entry against its own record of what it brokered, so a verified entry is a Python-checked fact and anything in `consults_unverified` is a claim with nothing behind it. What the stamp cannot decide is whether a consult was OWED — that is this audit's judgement, and a verified consult proves a question was asked, not that it was the right one. A verification-passed audit outcome advances; its worker `result.json` still uses the enum `verdict=passed`. Blocking findings return `verdict=failed`, `revisit=[stage-1-implementation]` with the raw findings; non-blocking notes are recorded. Stage 2 must be run by a worker independent from Stage 1. Under `accuracy: max` the leader places TWO independent stage-2 orders — the primary `llm_profile` and the route's `second_llm_profile` (a different harness or model) — and blocks over both with `upagent-await-any`. Both must return `passed` to advance. On disagreement, consult `finalization_defaults.advisor_profile` as the judge when set (its `result.json.decision`: `continue` accepts the work, `loop` replays stage-1 with both sets of findings, `stop-ask-human` gives the phase up); with no advisor, write `phase-result.json` verdict `blocked` for the human. Each auditor's order reuses the stage-2 `stage_id` with its own try and order id, and both results are recorded in `phase-status.md`.
3. **Stage 3 — ordinary deterministic local seam/contract checks**; merge the worktree back to main here iff `merge_back_at` selects Stage 3. Run only changed-scope local contract, component, hermetic integration, or targeted boundary checks. Hire a fresh verifier through the Recruiter only when a command fails, output is ambiguous, or attribution is unclear. Missing production wiring revisits Stage 1; residual cross-slice production work belongs in an explicit integration-construction phase, not in Stage 3. Non-ordinary variants keep their explicit contracts, including IaC.
4. **Stage 4 — no ordinary shared acceptance/deployment stage**; merge here iff `merge_back_at` selects Stage 4, then record that broad shared acceptance is deferred to the route-owned candidate-level finalization/gate. Do not run per-phase shared-environment, deployment, CI, upstream-DAG, or global acceptance checks here for ordinary phases. Non-ordinary variants keep their explicit contracts, including IaC.
5. **Stage 5 — deterministic finalization**: merge if not already merged, verify main, run exactly the effective route-owned `green_checks`, inspect logs for hidden failures, destroy the temporary worktree/branch, and write final evidence. The leader does not infer or branch on later candidate-level ownership. Route authors decide the command set before execution: when an explicit later candidate-level finalization/gate owns repository-wide test/lint/static-analysis, omit those generic commands from per-phase `green_checks`; otherwise retain the repository's normal green checks. Stage 5 always runs.

The full stage rules, the Stage 2 multi-angle audit detail, and rollback safety live in the shared phase protocol; follow them exactly.

## IaC phases (kind: iac)

When the route marks this phase `kind: iac`, the same ladder runs with terraform meanings:

- Stage-1 writes the terraform; stage-2 audits it adversarially (a different model family is recommended). Every IaC stage brief restricts the worker to `fmt`, `validate`, `init`, `plan`, and `show` — a stage worker never applies, and the terraform persona refuses apply without explicit approval evidence anyway.
- Stage-3 runs `init` and `plan -out <pass-dir>/iac/plan.bin`, saves `terraform show -json` output as `<pass-dir>/iac/plan.json`, builds the human table with `just iac-plan-table <pass-dir>/iac/plan.json > <pass-dir>/iac/plan-table.txt`, and records the artifact's SHA-256 in `phase-status.md`.
- The leader then asks the owner for the apply decision. The durable approval file is the owner→leader answer channel here, so this is a wait, not a `blocked`:

```text
just upagent-phase-publish $UPAGENT_PHASE_START_RECEIPT decision-required "IaC layer <phase-id>: apply approval required (destroy total <n>)" --requested-action iac-approval --evidence <pass-dir>/iac/plan-table.txt --evidence <pass-dir>/iac/plan.bin
```

  Then wait (bounded by the phase timeout) for `<pass-dir>/iac/approval.json` and, when it says approved, `<pass-dir>/iac/apply-receipt.json`. Validate both: `plan_sha256` in each must equal the recorded digest of the plan output the human reviewed, and the receipt's `exit_code` must be 0. `approved: false` ⇒ `phase-result.json` verdict `blocked` (the human declined). Timeout ⇒ `blocked` with the evidence paths. A sha mismatch means the plan went stale — re-run stage-3 as a new try and re-ask.
- Stage-4 is the TUI-performed apply, recorded from the receipt (runner `tui-apply`, receipt path as evidence) — the leader hires no stage-4 worker in an iac phase. The TUI always re-plans fresh at apply time (see /tui-control) rather than applying `plan.bin`, so what runs is never an opaque saved-plan artifact. There is no unattended variant: every apply is TUI-performed against a fresh re-plan, with no worker order permitted to apply a saved plan artifact.
- Stage-5 finalizes as usual: verify outputs/state summary, record evidence, clean the worktree.

## Stage-level backtracking and escalation

Backtracking replays **forward, in order** — nothing is reverted.

```text
stage worker result.json or controller-result.json verdict=failed, revisit=[stage-ids] → record the reason in phase-status.md
leader: replay from the earliest revisit stage-id forward; increment try (try-<m+1>)
  stage_try_budget hit (default 3) →
    advisor_profile set?  place an advisor order via the Recruiter (context = phase-status.md);
                          advisor result.json: verdict=passed, decision = continue | loop | stop-ask-human
    no advisor_profile →  give up: write phase-result.json.verdict=failed, revisit=[phase-ids]
```

The advisor is hired like any worker. Its order **reuses the failing stage's `stage_id`** (an ordinary recognized id) so it passes the order contract — there is no `stage_id: "advisor"`, which the contract would reject; the leader knows it placed an advisor order and reads `result.json.decision`. The advisor worker writes a normal `result.json` with `verdict: passed` **plus** the optional `decision` field — one of the exact tokens `continue`, `loop`, or `stop-ask-human` (the contract's `ADVISOR_DECISIONS`). The leader reads `result.json.decision`, not a special verdict: `continue` = accept the stage and advance; `loop` = another try (reset/extend the try budget); `stop-ask-human` = give up the phase to the TUI (write `phase-result.json.verdict=failed`, `revisit=[phase-ids]`).

When the leader gives up on the phase, it writes `phase-result.json` with `verdict=failed` and `revisit:[phase-ids]` so the TUI can replay from the earliest pointed phase. The leader never reverts merged history; a true revert is escalated to the TUI, which asks the human.

## phase-status.md and phase-result.json

- `phase-status.md` — one line per stage per pass, appended as work happens: `pass<p> <stage-id> try<m> <verdict> — <reason>, revisit <ids>`. This rolling log is how a later pass or try knows where it failed.
- `phase-result.json` — the latest phase verdict the TUI reads. Write and validate it, then as the literal final action before going idle print to this leader pane: `PHASE_RESULT: <phase-id> verdict=<passed|failed|blocked> pass=<n>`. Map `partial` to `blocked` for this completion marker while retaining the detailed file verdict. Include: phase id; pass number; `verdict` (`passed`/`partial`/`blocked`/`failed`); `revisit:[phase-ids]` on any non-passing verdict; lead `llm_profile`/`agent`; `accuracy` and the stage-0 outcome when high; `merge_back_at` and actual merge stage; temporary worktree branch/path and cleanup result; worker-stage evidence (`stage_id`, `llm_profile`, `agent`, `order_id`, tries, final verdict, and `full_log` pointer); deterministic-stage evidence from `controller-result.json` (`stage_id`, `runner: controller` or equivalent marker, commands, exit codes, log/evidence paths or bounded excerpts, tries, and final verdict); advisor status; dependency graph source; Stage 3 seam decision; Stage 4 ordinary-phase deferral or non-ordinary variant result; Stage 5 green-check and log-review result; rollback/cleanup actions.

For deterministic Stage 3/5 evidence, write `controller-result.json`; do not invent a synthetic `order_id`, worker `result.json`, or worker `full_log`. If the leader hires an anomaly verifier, record that verifier as separate worker-stage evidence and link it from the controller result.

Only write `passed` when every required stage passed and Stage 5 cleanup, green checks, and log review passed.

## Hard rules

1. Herdr-only: require `HERDR_ENV=1`.
2. LLM implementation, audit, verifier, advisor, and consult work is a work order to the Recruiter; deterministic Stage 3/5 controller work writes typed evidence directly. Neither path may create a native subagent, team, pane, or nested harness session, and there is no Claude team mode.
3. Workers are terminal and non-delegating; a worker that needs more help returns `blocked`, and a worker that has written its required files exits its session.
4. The route is authoritative and deterministic per stage; the leader resolves `harness`/`model`/`agent` from it and never lets the Recruiter pick.
5. Worker `result.json`, controller `controller-result.json`, and `phase-result.json` are the source of truth; pane scrollback is evidence only.
6. Forward-only: replay forward on `revisit`; never revert merged history from this phase — escalate a true revert to the TUI.
7. Do not close the worker pane until its evidence is persisted; do not reset shared/main branches without checking for unrelated changes and asking the human.
8. Only the Recruiter executes lifecycle actions, and only this requester authorizes extension/cancellation before the hard-deadline grace expires. In the default direct lifecycle there is no Dedicated Account Manager; a roster may opt into `management.mode: dedicated`.
