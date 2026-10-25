# AI Chaos Lab: presenter guide

AI-driven chaos engineering on the OpenTelemetry Astronomy Shop (25 services, Docker Compose).
An AI planner proposes experiments from the dependency map. A guarded engine injects real faults
while synthetic customers use every shop path. LangGraph agents explain the outcome and propose a
validated code fix. Dashboard: http://127.0.0.1:8099 · Shop: http://localhost:8081

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-dev.txt -r demo/requirements.txt
cp .env.example .env     # GEMINI_API_KEYS=key1,key2,...  GEMINI_MODEL=gemini-3.5-flash-lite
make start               # 25-service sandbox (Docker Desktop must be running)
python -m demo.server    # dashboard
```

Before presenting:
- Close heavy apps (browsers with many tabs, other AI desktop apps). On an 8 GB Mac, host
  memory pressure stalls the whole shop and the health gate will (correctly) refuse to inject.
- Check that every header chip is green.
- Run one experiment as a rehearsal.

## Demo flow (about 3 minutes)

1. **Plan**: *Ask AI to plan experiments*. The planner reads the dependency map and past runs and
   ranks experiments, each with a reason, hypothesis and expected blast radius. A safety validator
   rejects anything outside the allowed targets.
2. **Run** a proposal, or *Run top 5 as campaign*. Watch the live path matrix (10 customer paths)
   and the dependency graph light up with the blast radius.
3. **Evidence**: Jaeger error chains, Prometheus error spans, logs, container state.
4. **AI analysis**: ranked weaknesses, recommendations, and a code fix that is syntax-checked and
   refused if it merely deletes the injected fault.
5. **Report**: resilience score per experiment and overall, plus a Markdown findings report.

Pre-recorded alternative: `python scripts/record_demo.py --top 5`, then `python scripts/edit_demo.py`
(≤3-min video) and `python scripts/demo_matrix.py` (results matrix PNG/CSV/MD) in `artifacts/demo-video/`.

## Fault catalog: how each fault is really injected

| Scenario | Mechanism | Where the failure happens |
|---|---|---|
| Payment rejects charges | flag `paymentFailure` | payment `charge.js` throws |
| Checkout cannot reach payment | flag `paymentUnreachable` | checkout dials a bad address |
| Cart fails | flag `cartFailure` | cart `EmptyCart`, which checkout calls **after charging** (add/view cart are unaffected) |
| Product catalog fails for one product | flag with targeting | product-catalog, product `OLJCESPC7Z` only |
| DB lock contention | flag | product-catalog holds an exclusive table lock for a fixed 30 s |
| Recommendation cache failure, ad failure / CPU / GC, intl shipping slowdown, email memory leak | flags | inside each service; intl slowdown only for non-US addresses |
| Slow product images | flag **plus** server-side stalls of image-provider | upstream's flag only slows home-page cards (browser sends a delay header); the stalls make images slow on every page |
| Slow responses | repeated short freezes (pause/unpause) | any allowed service |
| Crash, hang, network partition, CPU starvation | `docker kill` / `pause` / `network disconnect` / CPU quota | any allowed service |
| DB overload | CPU-only SQL loop in PostgreSQL (cannot fill disk) | astronomy-db |
| Traffic spike, spike + crash | concurrent synthetic users (+ `docker kill`) | frontend / chosen service |
| Network latency + jitter, packet loss, corruption | `tc netem` in the target's network namespace | any allowed service |
| Bandwidth limit | `tc tbf` token bucket | any allowed service |
| Silent link failure / connection reset between two services | `iptables DROP` / `REJECT --reject-with tcp-reset` in the caller, matching only the callee's IP | checkout→payment/shipping/email, frontend→cart/catalog/currency/…, cart→valkey, catalog→db |
| DNS outage | `iptables DROP` to Docker's resolver 127.0.0.11 | any allowed service |

Never targeted: frontend-proxy, the telemetry stack and flagd (they would blind or disable the
experiment).

## Safety

- Health gate: no injection unless every path has a steady baseline (waits up to 30 s).
- Guardrails, all configurable: shop down, blast radius spreading to paths that don't depend on
  the target, error rate, median latency on unrelated paths, 60 s hard cap. A breach must persist
  for two checks.
- Every fault is journaled before it is applied and restored on finish, abort, crash, exit or the
  next server start. Flag restores are verified against what flagd actually serves.
- Kill switch (Shift+K) restores everything at once.

## Resilience score (deterministic; the AI explains it, never sets it)

Availability 30% · containment 25% · recovery time 20% · error-handling quality 15% · latency 10%.
Grades A ≥ 90, B ≥ 75, C ≥ 60, D ≥ 40. Campaign score = mean of experiments. See `demo/scoring.py`.

## Known limitations (say them up front)

- One run per experiment (~15–25 s fault window). Scores compare experiments; repeat runs to
  tighten them.
- Laptop load adds noise. Latency uses medians and the health gate refuses unhealthy baselines.
- Auto-fix deploys live only to interpreted services (payment, recommendation, email). Compiled
  services get a saved patch for an image rebuild.
- In the shop UI a failed checkout shows no message (the order call's error is never displayed).
  That is an upstream application weakness the experiments surface, not a lab bug.
