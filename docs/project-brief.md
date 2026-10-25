> **Current state (October 2026):** the **AI Chaos Lab** (`demo/`, `python -m demo.server`, http://127.0.0.1:8099) runs live: an AI planner proposes experiments, 20 guarded fault scenarios are injected into the 25-service shop and auto-restored, impact is measured from traces, metrics and logs, and LangGraph agents (free-tier Gemini) produce ranked weaknesses and validated code fixes. See [PRESENTATION.md](PRESENTATION.md). The controller/workbench below is the stricter foundation (P0–P7); its own live execution stays disabled by default.

> The sections below describe the foundation's design and acceptance plan.

# Faultline: project brief

Repository: https://github.com/0Advi/faultline

This brief describes the intended project after the core implementation plan
P0–P7 passes its acceptance checks. It does not claim those tasks are complete.
Current evidence is in [implementation status](../IMPLEMENTATION_STATUS.md);
the task backlog is in [implementation plan](../IMPLEMENTATION_PLAN.md).
Model-provider selection and API-key setup are deliberately omitted.

## The project in one sentence

A sandbox resilience-testing workbench that introduces controlled failures into
a microservice application, records their impact, explains the failure from
evidence, and verifies a reviewed improvement by repeating the experiment.

## Problem and intended value

A checkout can work normally yet handle a dependency failure badly: misleading
messages, excessive waiting, lost cart state or an unclear payment outcome.
Seeing an error does not establish its cause, and changing code does not prove
that resilience improved.

Our project connects the experiment, observations, diagnosis, restoration,
proposed patch and replay under one recorded workflow. A team should be able to
answer: what failed, what customers experienced, why we believe that diagnosis,
whether the system recovered, and whether the change actually helped.

## Application under test

We use the Apache-2.0 OpenTelemetry Astronomy Shop, a public, containerized
microservice application. Its source is pinned to release 3.1.0. The selected
deployment contains 25 application/supporting/observability services.

| Component group | Purpose |
|---|---|
| Frontend and entry proxy | Product browsing and checkout interaction |
| Cart, catalog, currency and recommendation | Shopping state, products, prices and suggestions |
| Checkout | Coordinates preparation, payment and order completion |
| Payment | Validates fake card fixtures and returns a simulated transaction ID |
| Shipping, quote and email | Delivery calculation, order shipping and confirmation |
| PostgreSQL and Valkey | Application data and cart storage |
| Locust | Repeatable application traffic |
| flagd | Built-in controlled failure switches |
| Collector, Prometheus, Jaeger, OpenSearch and Grafana | Collect and inspect metrics, traces and logs |

The important payment path is:

```text
Shop checkout -> Checkout service -> Payment service -> Shipping -> Order response
                     |
                     -> Cart / catalog / currency / shipping quote during preparation
```

There are two distinct interfaces: the existing shop is the application under
test; our planned workbench is where experiment operators inspect and control
the tests. Payments are synthetic. No bank or real payment processor is involved.

## What the repository will contain

This is the intended layout, not a claim that every directory exists today.

```text
faultline/
├── vendor/opentelemetry-demo/    Pinned upstream application
├── deploy/                      Compose configuration and sandbox-owned flags
├── controller/                  Our experiment and improvement workflow
│   ├── schemas/                 Shared records and validated inputs
│   ├── discovery/               Sandbox identity and service dependencies
│   ├── baseline/                Repeatable healthy measurements
│   ├── observer/                Probes and correlated telemetry
│   ├── safety/                  Allowlist, thresholds, locks and deadlines
│   ├── executor/                Bounded supported failure mechanisms
│   ├── watchdog/                Independent restoration after interruption
│   ├── store/                   Run history, event journal and evidence references
│   ├── api/                     Backend interface used by the workbench
│   ├── agent/                   Evidence-based planning, diagnosis and suggestions
│   └── repair/                  Patch tests, sandbox deployment and replay
├── web/                         Experiment workbench
├── config/                      Capability catalog and execution policies
├── patches/                     Reviewed changes against pinned application source
├── scripts/                     Setup, startup and verification helpers
├── tests/                       Controller, recovery and workflow verification
├── evals/                       Held-out diagnostic cases and measured results
├── artifacts/                   Architecture/startup evidence and local run results
├── docs/                        Architecture, experiment design and demonstration
├── IMPLEMENTATION_PLAN.md        Tasks, owners, dependencies and acceptance gates
├── IMPLEMENTATION_STATUS.md      What is actually verified and what remains pending
├── README.md                     Reproducible setup and usage
└── .github/workflows/            Automated repository checks
```

Reuse: the shop, its existing instrumentation, telemetry infrastructure and
initial fault switches. Team contribution: the guarded experiment lifecycle,
correlation and evidence record, diagnostic workflow, workbench and verified
patch/replay process. We preserve upstream attribution and source reproducibility.

## First experiments

| Experiment | Actual mechanism | What it should reveal |
|---|---|---|
| Payment handler failure | `paymentFailure=100%`, numeric value 1 | Charge requests fail while the service process can remain healthy |
| Payment unreachable from checkout | `paymentUnreachable=on` changes the caller's destination | Checkout-to-payment connectivity fails even if payment itself is healthy |
| Invalid-card control | An invalid field in a fake request fixture | A business validation failure that should be distinguished from an outage |

The two infrastructure/application fault scenarios come first. The invalid-card
case is a negative control, not infrastructure chaos. An actual process outage,
latency, lost responses and duplicate-transaction scenarios are later work.

## Experiment workflow

1. **Inspect and plan.** Select a supported capability and show its target,
   mechanism, expected impact, duration, observations and restoration procedure.
2. **Measure normal behavior.** Run repeatable valid checkout requests and
   protected-path probes. Record error rate, latency, health and telemetry
   freshness. Refuse execution if the healthy baseline is missing or stale.
3. **Validate and inject.** Deterministic code checks the sandbox, allowlist,
   single-run lock and policy. Persist the previous state before mutation and
   arm an independent watchdog. Confirm the fault actually took effect.
4. **Observe and record.** Collect timestamped request outcomes, relevant traces,
   sanitized logs, metrics, cart state and protected-path results.
5. **Restore and verify recovery.** Restore the exact previous state after
   completion or an abort. Confirm application requests recover. Keep failed or
   unconfirmed cleanup visible and block another run.
6. **Diagnose and propose.** Explain the failing service/edge and error category
   with evidence references, alternatives and uncertainty. Propose a relevant
   improvement instead of treating every checkout error as a payment decline.
7. **Review, apply and repeat.** Show the exact patch and tests, apply the
   approved revision to the sandbox, then replay the same fault and workload.
   Compare behavior and restore the prior application revision if checks fail.

Ending a fault and improving the application's code are different operations.
Changing a flag back is cleanup; a matched replay establishes whether the code
change improves handling of the same failure.

## What gets recorded

Each run has a unique ID linking:

- Scenario, target identity, policy/workload/source/image versions and timestamps.
- Previous, injected and restored configuration, plus restoration reason.
- Eligible request counts, HTTP/RPC results, latency, cart state and trace/span IDs.
- Sanitized log/metric references, observation freshness and missing measurements.
- Diagnosis, evidence references, uncertainty and recommendation.
- Patch revision, review, test results, deployment/recovery and replay comparison.

Detailed run evidence stays local by default. Publish only deliberately sanitized
exports. Reports must distinguish measured results, predictions and missing data.

## Safety and reliability

Execution remains restricted to the team's verified sandbox and supported
capabilities. The controller enforces a healthy-baseline precondition, exact
target identity, one active fault, a hard deadline, protected-path thresholds,
an emergency restore action and verified cleanup. An independent watchdog covers
controller termination; restoration cannot depend solely on an in-process handler.

During a payment fault, checkout is intentionally affected. Home, catalog, cart
operations and observability are protected. Intended checkout failures must not
be mistaken for protected-path degradation. Policy is derived from measurements,
not copied blindly from another experiment.

Reasoning proposes structured actions; deterministic code owns mutations. Model
failure must not prevent restoration. Invalid requests are rejected before any
fault. Observation loss causes a safe stop. Failed cleanup stays explicit.

## How we establish improvement

Compare baseline, fault and recovery under a documented workload. Then compare
the original application and the reviewed fix under a matched replay.

Useful outcomes include correct error category/message, bounded waiting,
preserved carts, unaffected protected paths and successful recovery. A payment
outage should still prevent payment; we do not count a fabricated success as
resilience. A frontend timeout alone cannot prove that payment failed.

For diagnosis evaluation, hide the injector's manifest and scenario label from
the diagnostic input, then compare its answer with that ground truth afterward.
Measure service/edge/category accuracy, false positives, detection delay and
valid evidence references. Include healthy cases and missing telemetry. This
guards against an agent merely repeating the known injection label.

Payment retries require special care: the current fake payment implementation
has no durable ledger or idempotency key. Lost-response/duplicate-transaction
evaluation needs those additions before retry safety can be demonstrated.

## Planned demonstration

1. Show the public repository and reproducible setup.
2. Show a healthy shop checkout and measured baseline.
3. Open the workbench and inspect a supported payment experiment.
4. Run one fault; show its actual effect, timeline and correlated observations.
5. Show evidence-based diagnosis and verified restoration.
6. Demonstrate a threshold-triggered stop and recovery in a separate bounded
   run; disclose if that acceptance evidence uses a controlled fixture.
7. Review a tested application improvement, apply it in the sandbox and replay
   the original fault.
8. Present the before/after report and remaining limitations.

## Team responsibilities

| Member | Lead responsibility |
|---|---|
| M1 | Sandbox, safety controller, executor and watchdog |
| M2 | Workload, baseline, observations and reports |
| M3 | Planning/diagnosis and held-out evaluation |
| M4 | Backend integration, workbench and user experience |
| M5 | Application fixes, tests, source builds and reproducible delivery |

## Status to communicate honestly

**Today:** the public repository, pinned open-source application, deployment
helpers, static architecture seed, local startup evidence and project plans
exist. Phase 0 adds tested shared schemas, scoped capability tools, sandbox checks
and sandbox-owned flags; live execution remains blocked. Startup verification observed the shop, cart/checkout and telemetry
working. The interface is a concept. A stable healthy baseline, observed map,
guarded live experiment, diagnostic agent, working workbench and verified fix
remain pending. Dedicated runtime isolation still needs verification.

**After P0–P7 acceptance passes:** an evidence-backed end-to-end sandbox prototype
covering the supplied Day 1 guidance and the broader controlled-failure/analysis
objective. Final scoring weights were not supplied, so do not claim a guaranteed
score. Multiple fault categories and production readiness remain outside the
first release.

## Short presentation script

> We are building a controlled resilience-testing workbench for microservices.
> Our target is an open-source shopping application, and our first focus is
> payment failure during checkout. The system will measure healthy behavior,
> introduce one bounded failure, record its impact and restore the application.
> It will use correlated evidence to explain the failure and propose an improvement.
> We will review and test that change, then repeat the same experiment to verify
> whether behavior improves. Our contribution is connecting controlled execution,
> evidence, recovery and verified improvement in a reproducible workflow.
> Today the application and telemetry startup are verified; the experiment and
> improvement workflow is the next implementation milestone.

After implementation, replace the final status sentence with specific completed
checks and measured results from the demonstrated run.
