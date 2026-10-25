# Integrated release and demonstration

> **Current state (October 2026):** the **AI Chaos Lab** (`demo/`, `python -m demo.server`, http://127.0.0.1:8099) runs live: an AI planner proposes experiments, 20 guarded fault scenarios are injected into the 25-service shop and auto-restored, impact is measured from traces, metrics and logs, and LangGraph agents (free-tier Gemini) produce ranked weaknesses and validated code fixes. See [PRESENTATION.md](PRESENTATION.md). The controller/workbench below is the stricter foundation (P0–P7); its own live execution stays disabled by default.

P1–P4 teammate branches were reviewed through their aggregate `bipranshu-base`
(`a89f62b`), which contains P0 `1b76363`, P1 `4611225`, P2 `fadd6ad`, P3
`2bf9352` and P4 `2d5607b`. The integration preserves these commits and the P5/P6
preparation `048ea93`, with merge `b764c51` followed by connection and review fixes.
The original branches remain available.

## Reproduce from GitHub

```sh
git clone --recurse-submodules https://github.com/0Advi/faultline.git
cd faultline
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
make test
make test-workbench # Node 20+, CI uses 22
python -m controller.schemas.export --check
make release-check
make eval-baseline # curated held-out fixtures, free, no provider key
make workbench     # connected UI + controller at http://127.0.0.1:8095/
```

`make release-check` verifies packaging without Docker. Its runtime acceptance
result remains false until the missing live work is performed; it does not replace
`make preflight`. Start the shop with `make start` after configuring Docker through
[the sandbox guide](sandbox.md). Fresh clones use shop port 8080; this machine's
ignored `deploy/demo.local.env` uses 8081. Prometheus uses 9090. The source deployment
has 25 services. This laptop has 8 GB RAM and a 4 GB Docker allocation; healthy
baseline performance must be measured, not assumed from startup smoke checks.

For visual-only sample history, use `make workbench-fixture` on port 8096.
Its sample records cannot execute faults or deploy a patch.

## Portable demonstration without live mutation

```sh
python scripts/demo_controller.py
```

This explicitly simulated demonstration uses temporary copied flags, fake application
responses, the real SQLite journal, runner, independent watchdog, restoration,
evidence exporter and free rule diagnosis. It leaves the real flag file unchanged.
The output is labeled `simulated_controller_demo`, with `live_acceptance: false`.
It demonstrates integration, not live payment reliability or patch improvement.

The connected UI supports real history, plan validation, event resume, abort,
healthy-baseline capture, free diagnosis, report/evidence links and isolated candidate
preparation. A stale fixture selection is cleared when the real history is empty.
Run creation uses durable request identities: a repeated action returns the same
run; a changed plan with the same identity is rejected.

## Live demonstration sequence still required

1. Provision a dedicated Docker daemon and record its exact identity, network and
   services in `config/sandbox.yaml`. Review approval and run `make preflight`.
   Docker Desktop mount translation currently needs reconciliation with the scope
   checker. Do not broaden mount roots or mark a shared environment approved just
   to make the gate pass.
2. Capture two baselines using the approved 30-attempt, 180-second workload with
   five warmup attempts. Preserve invalid-card controls, protected probes and
   trace/log/metric coverage. Run `make derive-policy BASELINES="a.json b.json"`.
3. Explicitly enable one supported capability in the execution policy. Demonstrate
   the normal fault lifecycle, manual abort and independent recovery after killing
   the controller. Run `make test-sandbox` and preserve sanitized evidence.
4. Diagnose using observed signals without exposing the injection label to the
   diagnoser. Missing telemetry must produce uncertainty or insufficient evidence.
5. Reproduce the payment error classification defect. Complete typed payment,
   checkout and frontend handling and tests. The current two-line checkout candidate
   preserves the RPC status; it is provisional and is not the full verified fix.
6. Implement a real deployment operator with a durable exclusive lock shared with
   experiment creation, identity checks, immutable new/rollback image digests,
   bounded health checks and crash supervision. Record exact-revision approval.
   HTTP clients cannot supply a fake passing test receipt or enable this operator.
7. Replay the same workload/fault against the changed source images. Check error
   category, retained cart, latency and protected-path regressions. An unavailable
   payment service must still fail payment. Report measured improvement only when
   matching evidence and verified recovery support it.

## What each component does

| Location | Responsibility |
|---|---|
| `vendor/opentelemetry-demo/` | Licensed pinned shop source; unchanged by integration |
| `deploy/` | Selected 25-service runtime, healthy sandbox-owned flags and local environment examples |
| `config/` | Typed capability catalog, disabled-by-default execution policy and sandbox/baseline criteria |
| `controller/baseline/`, `observer/` | Synthetic workload, healthy envelope and correlated telemetry |
| `controller/executor/`, `watchdog/`, `store/` | Bounded injection, independent restore, single active run and append-only events |
| `controller/api/`, `report/` | Loopback API, event stream, reproducible reports and sanitized evidence manifest |
| `controller/agent/`, `evals/` | Blind evidence diagnosis, deterministic grounding and held-out evaluation; free rules are the default UI path |
| `workbench/` | Same-origin operator UI connected through a thin P3 adapter; separate sample-only server |
| `improvements/`, `patches/` | Isolated candidate preparation, exact-revision gates and backend-neutral deployment/rollback/replay logic |
| `tests/`, `.github/workflows/` | Automated contract, failure, API, integration and delivery checks |

## Evidence and claims

`docs/evidence/` contains intentionally selected fixture evaluation and simulated
integration evidence. Existing `artifacts/startup_verification.json` is an older
single startup snapshot, not a baseline or fault result. Runtime databases,
unsanitized captures, patch workspaces, derived host policy and credentials stay
ignored. Upstream licenses and notices remain in the pinned submodule.

The free held-out rule evaluation covers 13 curated diagnosis cases. It does not
measure arbitrary injection readiness, live diagnosis quality, paid model behavior
or autonomous repair performance. No model API credentials are required here.

P7 packaging and branch delivery are implemented. P7's full live demo acceptance
remains open while the real P5/P6 acceptance work above is incomplete.
