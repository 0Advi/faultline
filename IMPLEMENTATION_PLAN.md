# Implementation plan: payment chaos agent and user workbench

Updated: 10 October 2026. Repository: `0Advi/faultline`.

This is a task backlog, not a record of implemented capabilities. Unchecked
tasks remain pending. Existing startup evidence is described in
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). The application-specific
findings and proposed experiments are in
[docs/payment-failure-plan.md](docs/payment-failure-plan.md).

## 1. Intended outcome

A user selects or describes a supported payment failure. The system proposes a
bounded experiment, validates it, injects the fault in a verified sandbox,
records correlated evidence, restores the application, diagnoses the failure,
and proposes a solution. The user can inspect the exact patch before applying
it in the sandbox. The system then repeats the same experiment to establish
whether the change improves behavior.

The initial application is the local OpenTelemetry Astronomy Shop. Payments use
fake fixtures; the inspected implementation does not contact a bank. A payment
outage should still prevent checkout. A successful fix can mean accurate error
messages, retained carts, bounded latency and verified recovery.

First release scope:

- Deterministic `paymentFailure=100%`, followed by `paymentUnreachable=on`.
- A valid checkout control and an invalid-card negative control.
- Baseline, fault, restoration and recovery measurements.
- Evidence-based diagnosis, a readable report and a functional workbench.
- One demonstrated application fix, applied after review and verified by replay.

Later scope: actual process outage, payment delay, lost responses, idempotency,
duplicate-transaction evaluation and additional service/task capabilities.
Production execution and unrestricted model access to the host are outside this
plan's initial scope.

## 2. Starting point and constraints

| Component | Existing evidence | Remaining work |
|---|---|---|
| Repository and application | Public repo; upstream release 3.1.0 pinned | Preserve reproducibility and attribution |
| Local shop | Timestamped 25-service startup and synthetic checkout passed | Stabilize runtime and measure a healthy baseline |
| Observability | Logs, traces and metrics present at startup | Freshness checks, correlation and experiment queries |
| Dependency map | Static source-derived seed | Confirm observed payment edges |
| Payment fault mechanisms | Two upstream flags available | Isolated storage, guarded execution and recovery |
| Controller | Responsibilities documented | Implement controller, journal and independent watchdog |
| Model integration | No chaos-agent model integrated or evaluated | Provider adapter, tools, context and evaluations |
| Workbench | Interactive interface concept only | Working UI and controller API |
| Fix application | Design only | Patch review, tests, source builds, deployment and replay |

The current Compose project/network separation does not establish dedicated
runtime isolation. Verify that before executing faults. Earlier startup traffic
had timeouts; do not treat startup success as a healthy baseline.

The early disabled recommendation experiment contract has been removed; payment capabilities live in `config/capabilities.yaml`.
It must not silently become the payment execution policy. Its proposed
two-second request timeout is unsuitable without payment baseline measurements;
the recorded successful checkout took approximately 2.81 seconds.

## 3. Five-person responsibility split

Use these role labels until actual names are assigned. Each owner leads the
implementation and supplies evidence; the reviewer checks completion.

| Member | Primary responsibility | Initial tasks | Reviews |
|---|---|---|---|
| M1: controller and sandbox | Isolation, policy, lifecycle, injector, watchdog | P0-01, P0-03, P2-01–P2-04 | API mutation boundaries and deployments |
| M2: workload and observability | Fixtures, baseline, probes, traces, metrics, reports | P1-01–P1-04, P3-01 | Experiment impact and replay measurements |
| M3: agent and evaluation | Model adapter, bounded tools, context, diagnosis, evaluations | P0-04, P4-01–P4-04 | Evidence grounding and suggestion quality |
| M4: API and user interface | API contracts, workbench, event stream, history, review UX | P0-02, P3-02–P3-03, P5-01–P5-04 | User flow and error states |
| M5: application fixes and integration | Payment error handling, patch pipeline, CI, release demo | P3-04, P6-01–P6-04, P7-01–P7-03 | Recovery tests, reproducibility and final acceptance |

Before P2 enables execution, M2 and M5 independently review its safeguards.
Before P6 applies a patch, M1 reviews deployment/recovery and M3 reviews whether
the proposed change addresses the observed failure.

## 4. Proposed architecture and repository layout

Keep the controller in Python to reuse the existing project tooling. Use a
Python HTTP API, a TypeScript web UI, SQLite for local run metadata and an
append-only event journal, plus files for larger sanitized evidence. These are
implementation choices, not installed dependencies. Pin dependencies and commit
lock files during implementation. Use one model behind a provider adapter first.

```text
User workbench -> Controller API -> Durable workflow + policy validation
                                     |             |
                                     |             -> Model: plans/diagnoses/patch proposals
                                     -> Bounded tools
                                         -> Observer and workload runner
                                         -> Injector + independent watchdog
                                         -> Patch tests/build/deployment/replay
                                     -> Run journal + evidence + reports
```

The model proposes actions. Deterministic code validates and executes them.
Assign logical planning, diagnostic and remediation roles through separate
inputs and calls; multiple autonomous agents are not required for the first
release. The upstream shopping agent is not a chaos controller.

Proposed additions:

```text
controller/
  schemas/          Plan, capability, observation, diagnosis, patch contracts
  discovery/        Runtime identity and observed dependencies
  baseline/         Fixed workload and baseline comparison
  observer/         Probes, telemetry adapters and sanitized evidence
  safety/           Policy, lock, deadlines and recovery decisions
  executor/         Allowlisted fault adapters
  watchdog/         Independent restoration and restart reconciliation
  store/            SQLite metadata, event journal and artifact references
  api/              Experiment, report and patch endpoints
  agent/            Provider adapter, tool registry, context and prompts
  repair/           Patch isolation, tests, builds and sandbox deployment
web/                Functional workbench
config/             Versioned capability catalog and execution policies
deploy/             Sandbox-owned flags and controller deployment overlay
patches/            Reviewed application patches against pinned upstream
tests/              Contract, recovery, API and workflow tests
evals/              Held-out cases, expected classifications and evaluation runner
docs/               Operator guide, contribution guide and demo walkthrough
artifacts/results/  Ignored local run evidence; explicitly sanitized exports only
```

### Shared contracts

- **Capability:** ID, supported targets, typed parameters, adapter version,
  prerequisites, duration limits, observations, protected paths, restoration
  procedure and success criteria.
- **Plan:** capability ID, verified sandbox identity, proposed duration,
  baseline reference, expected impact and applicable policy version.
- **Run:** run ID, source/image/config revisions, workload version, exact prior,
  applied and restored state, actual timestamps, events, evidence references,
  cleanup status and recovery outcome.
- **Observation:** request/trace/span references, timestamp, HTTP/RPC outcome,
  latency, cart state, metrics/log references and freshness/completeness flags.
- **Diagnosis:** failing service or edge, category, affected paths, cited
  observations, alternatives, uncertainty and suggested next action.
- **Patch:** base revision, diff/hash, affected files, rationale, approval of
  that exact revision, tests, built image digests and deployment rollback data.
- **Comparison:** matched baseline/fault/recovery and original/fixed runs,
  attempts and missing samples, behavior differences and remaining limitations.

Fault lifecycle:
`DRAFT -> BASELINING -> READY -> ARMED -> RUNNING -> RESTORING -> VERIFYING_RECOVERY -> RECOVERED`.
Restoration follows cancellation, timeout, observation loss and failure as well
as normal completion. Record `ABORTED` or `FAILED` with verified cleanup; if
cleanup cannot be confirmed, retain `NEEDS_ATTENTION` and block new runs.
Model analysis and the separate patch/replay lifecycle follow recovery.

## 5. Phased task backlog

### P0 — Contracts and execution boundary

- [x] **P0-01 — Specify the dedicated sandbox.** Owner M1; reviewer M5.
  Resolve Docker context/daemon identity, project labels, exact service/container
  IDs and allowed mounts. Document how to provision a dedicated environment on
  a suitably resourced machine. Reject unrelated runtimes and targets.
  **Done when:** scope checks have positive and negative fixtures; the current
  shared preview cannot be mistaken for approved isolation.

- [x] **P0-02 — Define schemas and API contracts.** Owner M4; reviewers M1/M3.
  Implement versioned shared contracts and example payloads from section 4.
  Specify resumable event sequences and state transition rules.
  **Done when:** controller, UI and agent fixtures validate against the same
  schemas; malformed plans and illegal transitions are rejected.

- [x] **P0-03 — Create sandbox-owned flag configuration.** Owner M1; reviewer M2.
  Copy the pinned flag definitions under `deploy/`, mount the same directory
  into flagd and its viewer, and make the runner the sole writer during a run.
  Disable random scheduling and conflicting UI edits. Verify file replacement
  is visible inside flagd and runtime evaluation matches the chosen value.
  **Done when:** no automated upstream edits are needed; baseline settings and
  exact state snapshots are reproducible.

- [x] **P0-04 — Define the capability and agent boundaries.** Owner M3; reviewer M1.
  Convert the proposed payment catalog into typed capability contracts while
  keeping execution disabled. Specify tool inputs, outputs, errors and limits.
  **Done when:** unsupported capabilities return a structured explanation;
  model proposals cannot create arbitrary commands, paths or targets.

**Review:** Phase 0 foundation accepted with 93 passing tests and read-only flag
mount/reload verification. See [Phase 0 review](docs/phase0-review.md). The
dedicated runtime remains unapproved; fault-value evaluation and sole-writer
enforcement during active runs must be exercised with the P2 safety gates.

**Gate:** agreed contracts and sandbox preparation; no live fault enabled yet.

### P1 — Repeatable workload, observations and healthy baseline

- [ ] **P1-01 — Build synthetic checkout fixtures.** Owner M2; reviewer M5.
  Reuse valid upstream fake data, fixed products/currency and synthetic baggage.
  Give each attempt a fresh cart/session. Add invalid-card controls and ensure
  logs/artifacts exclude raw card, CVV, address and email payloads.
  **Done when:** healthy checkout, cleared cart after success and retained cart
  after validation failure have recorded outcomes.

- [ ] **P1-02 — Implement the observer.** Owner M2; reviewer M1.
  Collect bounded checkout and protected-path probes, trace/span references,
  sanitized logs, RPC status and applicable metrics. Check freshness and record
  missing observations. Count all attempts; the payment success counter alone
  is not a failure-rate denominator.
  **Done when:** an attempt can be traced through checkout/payment, and missing
  or stale telemetry produces explicit uncertainty rather than invented data.

- [ ] **P1-03 — Stabilize and measure the baseline.** Owner M2; reviewer M5.
  Start with a proposed 30 valid attempts over roughly three minutes; adjust
  workload to measured machine capacity without disguising failures. Record
  failures, latency distribution, resources, health and telemetry freshness.
  Separate warmup from measurement and repeat the baseline for reproducibility.
  **Done when:** a documented healthy envelope is met; otherwise publish the
  blocking measurements and leave execution disabled.

- [ ] **P1-04 — Derive payment-specific policy.** Owner M2; reviewer M1.
  Measure the payment path and label runtime edges. Derive probe timeouts,
  deadlines, protected-path thresholds, observation age and minimum samples.
  Checkout is intentionally affected; home/catalog/cart/observability are
  protected. Do not copy recommendation thresholds into payment experiments.
  **Done when:** a versioned baseline and policy explain every execution gate.

**Gate:** fresh, repeatable baseline and correlated payment observations.
Dependencies: P0-01–P0-03. Observation adapters can begin from P0-02 fixtures.

### P2 — Guarded injection and independent restoration

- [ ] **P2-01 — Implement the durable runner.** Owner M1; reviewer M5.
  Add policy validation, one-run lock, bounded tool parameters, persisted
  pre-mutation recovery journal and monotonic hard deadlines. Recheck the
  baseline, sandbox identity and configuration before applying a change.
  **Done when:** duplicate/concurrent requests, stale baselines, invalid targets
  and excessive durations are rejected without mutation.

- [ ] **P2-02 — Implement payment flag adapters.** Owner M1; reviewer M2.
  First support `paymentFailure` with variant `100%` / value `1`; then support
  `paymentUnreachable=on` / value `true`. Change only the selected flag, verify
  evaluation and request impact, and restore its exact previous value.
  Detect configuration conflicts; do not silently overwrite external changes.
  **Done when:** a controlled run records actual fault effect and restoration.

- [ ] **P2-03 — Add watchdog and restart recovery.** Owner M1; reviewer M5.
  Run restoration supervision outside the controller process. Use persisted
  state to recover after hard termination and reconcile unfinished runs on
  restart. Verify recovery with successful application requests, not only flag
  values. Report failed cleanup and prevent another experiment.
  **Done when:** killing the controller during injection still leads to timely
  restoration, or an explicit blocked recovery state if the environment fails.

- [ ] **P2-04 — Validate all stop paths.** Owner M1; reviewers M2/M5.
  Exercise completion, manual abort, deadline, protected-path degradation,
  observer loss, concurrent edits and hard process termination. Verify unrelated
  services remain untouched. Proposed first run: 15 seconds, hard cap at most
  30 seconds; insufficient samples must remain an inconclusive result.
  **Done when:** recovery tests pass with evidence before enabling each adapter
  in the verified sandbox.

**Gate:** reproducible inject/observe/restore/recover cycle without an LLM.
Dependencies: P0 and P1. This is the critical path for live execution.

### P3 — Experiment record, controller API and integration tests

- [ ] **P3-01 — Persist evidence and generate reports.** Owner M2; reviewer M3.
  Store structured events and sanitized artifact references under one run ID.
  Generate JSON and Markdown comparisons of baseline/fault/recovery, including
  cleanup, sample counts, missing data and limitations.
  **Done when:** a report can be reproduced from saved evidence without an LLM.

- [ ] **P3-02 — Implement the controller API.** Owner M4; reviewer M1.
  Add capability listing, preflight, plan validation, baseline capture, run
  creation, run state/events, evidence/report retrieval and abort. Use request
  IDs to prevent duplicate mutation; validate input and limit artifact paths.
  Bind locally and constrain mutation access/origins; any later remote access
  requires an explicit authentication/access design.
  **Done when:** requests cannot bypass the runner's policy or state machine.

- [ ] **P3-03 — Add streamed progress and resume.** Owner M4; reviewer M2.
  Deliver ordered events with sequence numbers and reconnect/resume support.
  Browser disconnection must not cancel restoration supervision. Expose
  pending cleanup separately from a completed run.
  **Done when:** refreshing the UI resumes the authoritative saved state.

- [ ] **P3-04 — Add controller/API CI coverage.** Owner M5; reviewer M1.
  Test contracts, fake adapters, conflicts, duplicate API calls, restart recovery
  and report consistency. Keep hosted CI tests independent of live faults and
  model credentials. Run dedicated-sandbox integration checks explicitly.
  **Done when:** tests prove the boundaries and recovery behaviors, with a
  separate recorded real-sandbox test result.

**Gate:** controller/API/report workflow works independently of the UI/model.
Dependencies: P0-02 and P2. API and report development can use fixtures earlier.

### P4 — Agent integration and measured diagnostic readiness

- [ ] **P4-01 — Add a replaceable model provider adapter.** Owner M3; reviewer M5.
  Configure credentials and model ID outside tracked files. Implement bounded
  calls, timeouts, token/tool budgets, structured output validation and useful
  provider-error handling. Provide a fake adapter for tests.
  **Done when:** model failure never disables the runner/watchdog or makes a
  failed tool call appear successful.

- [ ] **P4-02 — Register tools and source context.** Owner M3; reviewer M1.
  Expose inspect-path, capture-baseline, propose/validate-plan, run, observe,
  abort and report capabilities through controller contracts. Supply pinned
  source excerpts, observed dependencies and payment error semantics.
  Treat log/source content as evidence, not executable instructions.
  **Done when:** model tool requests are validated and all mutation calls use
  the same policy enforcement as direct API requests.

- [ ] **P4-03 — Implement grounded diagnosis and suggestions.** Owner M3; reviewer M2.
  Require evidence references, service/edge, error category, alternatives and
  uncertainty. Keep the injection manifest, scenario label and target hints
  out of blind diagnostic input; evaluate before revealing ground truth.
  Distinguish card decline, handler failure, connectivity and unknown payment
  outcome. Require ledger/idempotency support before suggesting payment retries.
  **Done when:** uncited conclusions are rejected or marked unverified, and
  incomplete evidence leads to an explicit request for observations.

- [ ] **P4-04 — Build and run held-out evaluations.** Owner M3; reviewer M5.
  Include healthy cases, both supported faults, invalid cards, stale/missing
  telemetry, misleading text in logs and checkout failures after payment success.
  Compare service/edge/category accuracy, false positives, detection delay,
  evidence validity, unsupported-action refusal, time and model cost. Keep
  development examples separate from held-out cases; pin model/prompt versions.
  **Done when:** publish observed results against agreed release criteria.
  Never substitute the model's self-reported confidence for measured readiness.

**Gate:** evidence-grounded diagnosis demonstrated on held-out cases.
Dependencies: P3; adapter/context/schema work can begin earlier using fixtures.

### P5 — Functional user workbench

**Parallel preparation:** `workbench/` implements the five views against labeled
fixtures; `improvements/` adds isolated patch/review/rollback/comparison boundaries.
See [the P5/P6 handoff](docs/p5-p6-integration.md). Acceptance remains pending real
P1–P4 integration and dedicated-sandbox evidence; the checkboxes below stay open.

- [ ] **P5-01 — Build planning and readiness screens.** Owner M4; reviewer M1.
  Translate the concept into real UI: sandbox identity, capability selection,
  natural-language request, proposed scope/duration and execution prerequisites.
  Clearly distinguish unsupported, disabled, preview and live capabilities.
  **Done when:** the UI accurately reflects API readiness and cannot enable a
  blocked execution path.

- [ ] **P5-02 — Build progress, evidence and history.** Owner M4; reviewer M2.
  Show plan/injection/restoration/diagnosis stages, an ordered timeline, linked
  observations, protected-path status, uncertainty and saved run history. Keep
  emergency restore available while a live fault is active.
  **Done when:** reconnect, abort, stale data and failed cleanup are usable
  states; no illustrative data is presented as measured evidence.

- [ ] **P5-03 — Build patch review and comparison views.** Owner M4; reviewer M5.
  Show exact affected files/diff, rationale, tests, approved revision, deployment
  outcome and matched replay. Separate ending a fault from applying a code fix.
  **Done when:** applying a changed patch requires approval of its new revision;
  incomplete tests/recovery cannot be displayed as a verified fix.

- [ ] **P5-04 — Verify the complete user flow.** Owner M4; reviewer M3.
  Exercise keyboard/mobile layouts, refresh/resume, unsupported requests,
  provider failures and API failures. Use deterministic fixtures in UI tests and
  complete a recorded real-sandbox end-to-end check after backend gates pass.
  **Done when:** a new user can inspect a plan, run an enabled fault, understand
  evidence, stop it and review a proposed fix without terminal access.

**Gate:** working UI backed by actual controller state and evidence.
Dependencies: P3 and P4 for real agent behavior. UI implementation may proceed
against clearly labeled fixtures as soon as P0-02 is agreed.

### P6 — Application improvement and controlled patch application

- [ ] **P6-01 — Confirm and implement the first payment fix.** Owner M5; reviewer M2.
  First reproduce the source-predicted error-mapping problem. Preserve typed
  card-decline/service-failure/unknown-outcome categories through payment,
  checkout and frontend; verify cart handling. If observations contradict the
  prediction, choose the fix supported by actual evidence.
  **Done when:** focused tests distinguish categories and match runtime findings.

- [ ] **P6-02 — Implement isolated patch preparation.** Owner M5; reviewer M3.
  Extend agent tools with propose-patch and test-patch. Work in an isolated
  checkout/branch, reject changes outside allowed application paths, and record
  base revision and diff hash. Preserve the pinned upstream checkout; store
  reviewed patches and build from an explicitly prepared source tree.
  **Done when:** an exact patch, test result and reproducible source-build input
  are available for user review.

- [ ] **P6-03 — Implement approved sandbox deployment and rollback.** Owner M5;
  reviewer M1. Bind approval to the exact patch/hash, tests and target sandbox.
  Record old/new image digests and configuration; replace only affected services
  between experiments. Verify startup and roll back on failed checks.
  **Done when:** no model suggestion or stale approval deploys code implicitly,
  and a failed deployment restores the prior application revision.

- [ ] **P6-04 — Replay and evaluate the fix.** Owner M5; reviewer M2.
  Repeat the original capability with matched workload and recorded revisions.
  Compare correct message/category, response bounds, cart state, protected paths,
  cleanup and recovery. Keep a persistent outage visibly unsuccessful at payment.
  **Done when:** observed results substantiate the claimed improvement, or the
  patch is recorded as ineffective/unverified and is not promoted.

**Gate:** one reviewed fix applied and verified through the same experiment.
Dependencies: P2–P5 and source builds. P6-01 can start once P2 yields evidence.

### P7 — Reproducible GitHub delivery and demo

Integration update (10 October 2026): P0–P4 branches are combined with P5/P6;
connected UI actions use P3. Packaging/CI/offline evaluation and simulated demo
are provided. **P7-02 live acceptance remains open**, as do measured P5/P6
acceptance criteria. See [release status and remaining tasks](docs/release-and-demo.md).
Preparation and fake tests do not close runtime acceptance checkboxes.

- [x] **P7-01 — Package setup and runtime configuration.** Owner M5; reviewer M1.
  Add pinned dependencies, example configuration without secrets, controller/UI
  startup instructions and commands for tests/evaluations. Update README,
  Makefile, workflow validation and implementation status. Document machine
  resource limitations and how to provision the dedicated runtime.
  **Done when:** a fresh clone can reproduce setup and detect missing prerequisites.

- [ ] **P7-02 — Publish sanitized evidence and run the demo.** Owner M5; reviewers
  M2/M4. Export selected evidence intentionally, preserving trace references,
  revision IDs, model/prompt versions and limitations. Demonstrate healthy
  checkout, fault, diagnosis, verified restoration, reviewed fix and replay.
  **Done when:** the demo and report agree with actual measurements.

- [x] **P7-03 — Review, merge and push completed work.** Owner M5; reviewer is the
  relevant domain owner. Use small task-focused branches/PRs and required CI.
  Preserve upstream license notices; exclude credentials, local databases and
  unsanitized evidence. Mark tasks complete only after their acceptance evidence.
  **Done when:** GitHub contains working code, passing checks and accurate docs.

**Gate:** first release is reproducible from GitHub with an honest readiness report.

## 6. Additional injections and other agent tasks

Add capability coverage incrementally; do not promise readiness for arbitrary
injections. Each new capability needs a tested adapter, observation semantics,
policy, recovery procedure and evaluation cases before it becomes executable.

- [ ] **X-01 — Payment process outage.** M1/M2; after P2.
  Stop/start only the validated existing sandbox payment container. Prove
  identity checks, termination recovery and restored checkout.
- [ ] **X-02 — Payment delay.** M1/M5; after P7 first release.
  Add a scoped test hook/proxy with request correlation, bounded delay and exact
  cleanup. Measure deadline behavior and protected-path impact.
- [ ] **X-03 — Persistent fake ledger and idempotency.** M5; after P7.
  Add logical order/request keys, durable transaction lookup, concurrent request
  deduplication and explicit outcome states. Test crash/restart behavior.
- [ ] **X-04 — Lost response and duplicate-transaction evaluation.** M2/M5;
  after X-02/X-03. Lose a response after recorded simulated success; evaluate
  reconciliation and retries against the durable ledger. Do not infer duplicate
  charges from UUIDs or the existing success counter alone.
- [ ] **X-05 — Other services and read-only tasks.** M1/M3; after P7.
  Add adapters for selected service failures and separate capabilities for
  architecture inspection, telemetry explanation, report comparison and test
  generation. Assign each its own schema, permissions and completion checks.
- [ ] **X-06 — Broader autonomous fixes.** M3/M5; after measured P4/P6 success.
  Expand allowed application paths and fix categories only with evaluation cases
  and demonstrated recovery. Fine-tuning is optional future work if evaluations
  show a specific gap that context, tooling and prompt improvements do not solve.

## 7. Scheduling and coordination

Critical path:
`P0 -> P1 -> P2 -> P3 -> P4 -> integrated P5/P6 -> P7`.

Parallel development is possible without parallel live experiments:

- After P0-02, M4 builds UI/API fixtures while M1 prepares the runner and M2
  implements probes/baseline. M3 prepares provider and diagnostic contracts.
- M5 builds recovery test fixtures and CI before fault execution, then starts
  the application fix when P2 supplies confirmed evidence.
- Integrate against the shared contracts after every phase. Reserve live
  experiment slots through the enforced single-run lock.
- Track tasks using the IDs above; each PR lists its task IDs, acceptance checks,
  evidence and dependencies. Update this file and status documentation together.

Assign delivery dates only after the dedicated runtime and baseline are measured.
Source builds, machine memory, telemetry reliability and provider availability
are known schedule dependencies. A missing gate remains a blocker, not a reason
to silently relax execution requirements.

## 8. First release acceptance checklist

- [ ] Fresh clone and pinned dependencies reproduce the documented setup.
- [ ] Dedicated sandbox identity and a measured healthy baseline are established.
- [ ] Both payment flags have real injection/impact/restoration evidence.
- [ ] Normal completion, manual abort, observation loss and controller termination
  restore the prior state and verify application recovery.
- [ ] Invalid targets, excessive durations and overlapping runs are rejected.
- [ ] Every run links requests, telemetry, configuration and cleanup under one ID.
- [ ] Blind diagnostic evaluations publish actual results and missing-data limits.
- [ ] The workbench accurately displays readiness, progress, evidence and recovery.
- [ ] One reviewed application patch passes tests, deploys to the sandbox and
  demonstrates improved behavior under a matched replay.
- [ ] GitHub CI passes; docs distinguish completed, experimental and unsupported
  capabilities; published evidence is sanitized.

Immediate next task: **P1 repeatable workload, telemetry freshness and healthy-baseline work.**
No task in this plan authorizes enabling faults before those execution gates pass.
