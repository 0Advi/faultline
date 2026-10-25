# Implementation status

> **Current state (October 2026):** the **AI Chaos Lab** (`demo/`, `python -m demo.server`, http://127.0.0.1:8099) runs live: an AI planner proposes experiments, 20 guarded fault scenarios are injected into the 25-service shop and auto-restored, impact is measured from traces, metrics and logs, and LangGraph agents (free-tier Gemini) produce ranked weaknesses and validated code fixes. See [docs/PRESENTATION.md](docs/PRESENTATION.md). The controller/workbench below is the stricter foundation (P0–P7); its own live execution stays disabled by default.

Updated 10 October 2026 following review and integration of all teammate P0–P4
branches with P5/P6. See [release and demo instructions](docs/release-and-demo.md).

| Phase | Implemented and checked | Acceptance remaining |
|---|---|---|
| P0 | Typed contracts, capability registry, pinned source and sandbox boundary | Dedicated daemon identity/approval |
| P1 | Workload, observer, baseline measurement and derived policy | Two fresh reproducible healthy runtime baselines |
| P2 | Durable guarded executor, bounded stop paths, restore and independent watchdog | Live normal/abort/controller-kill recovery |
| P3 | Loopback API, resumable event stream, sanitized reports and evidence | Dedicated-sandbox integration acceptance |
| P4 | Blind evidence, rule/model adapters, grounding and held-out fixtures | Live quality evaluation; optional model evaluation is outside current scope |
| P5 | Connected planning/readiness, real history/events, abort, free diagnosis, baseline capture, evidence and candidate review | Complete live user flow and measured comparison |
| P6 | Isolated candidate, source integrity, approval/test/image gates, backend-neutral rollback/replay | Full typed payment fix, real image operator/shared lock/crash supervisor, approved deployment and matched replay |
| P7 | Reproducible setup, CI, offline readiness inventory, simulated demo/evaluation evidence and branch integration | Full live demonstration acceptance depends on remaining P1/P2/P6 work |

## Verification

- Combined Python/controller/UI bridge suite: 294 passed; 6 opt-in live sandbox
  tests deselected. They have not passed against a real dedicated sandbox.
- Six browser-independent workbench state/transport tests pass; 12 generated
  contract schemas match the committed files.
- Held-out free rule diagnosis: all 13 curated cases match their expected category;
  reported applicable criteria pass. Action/model/cost criteria are not measured.
- Current local home, catalog, recommendations and Prometheus smoke probes pass.
- Connected browser shows the real readiness blockers and disables injection.
- Isolated checkout candidate's existing Go application tests pass. These do not
  prove the full payment fix, deployment or replay outcome.

## Review fixes made during integration

The runner waits for independent watchdog acknowledgement before any fault is
armed. Watchdog startup failure aborts without applying a fault. Baseline capture
and run creation are mutually excluded in the API. Execution flags require actual
booleans. Invalid/unsupported HTTP requests receive structured errors. Future,
undersampled, wrong-revision and wrong-sandbox baselines cannot gate a live run.
Untrusted log mentions of idempotency/ledger cannot authorize payment retries.
Baseline report reads are confined to the artifact directory. Workbench actions
use the existing controller and cannot assert passing application tests or bypass
unimplemented deployment prerequisites.

## Runtime state and remaining work

This machine is a shared preview: `approved: false`, daemon identity unset,
execution disabled and no derived healthy-baseline policy. Current Docker context
and Docker Desktop collector mount aliases fail the reviewed scope check. No live
fault or code-image deployment was attempted during integration. The pinned
upstream source and healthy flags remain unchanged.

Complete the ordered seven-step live demonstration in
[docs/release-and-demo.md](docs/release-and-demo.md). Current status is **an integrated,
tested development platform with guarded execution**, with live end-to-end payment
repair and final acceptance still pending. Do not present sample or simulated
records as measured shop behavior.
