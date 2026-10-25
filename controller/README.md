# Faultline controller boundary

Implemented so far:

- **P0:** versioned shared records and JSON Schemas, lifecycle rules, typed payment
  capabilities, bounded agent tools and sandbox scope checks.
- **P1:** synthetic checkout fixtures, a telemetry-correlating observer, healthy
  baseline measurement and derived payment policy (`observer/`, `baseline/`).
- **P2:** a durable runner with a SQLite run store, journaled exact-bytes flag
  changes, an independent watchdog and restart reconciliation (`executor/`,
  `store/`, `watchdog/`). See [runner](../docs/runner.md).
- **P3:** evidence export with reproducible JSON/Markdown reports (`report/`) and a
  loopback controller API with resumable event streams (`api/`). See [API](../docs/api.md).

Live execution stays blocked by code gates: an approved sandbox, a healthy fresh
baseline, a derived payment policy and the capability listed in the execution
policy. By review rule, a capability is listed only after its stop paths have
recorded evidence from the dedicated sandbox (`make test-sandbox`).
No external model or API key is used.

## Remaining responsibilities

| Phase | Responsibility | Required evidence |
|---|---|---|
| P1 | Repeatable synthetic checkout, observations, fresh telemetry and measured baseline | Healthy workload and observed dependencies |
| P2 | Approved runtime, single-run lock, thresholds, deadline, manual abort and independent watchdog | Invalid-target refusal and recovery through process termination |
| P4 | Evidence-grounded diagnosis and suggestion tools | Blind evaluations and missing-data handling |
| P5–P7 | Workbench, reviewed application fixes and matched replay | Demonstrated recovery and measured improvement |

The current preview is not approved isolation. See [sandbox setup](../docs/sandbox.md)
and [Phase 0 review](../docs/phase0-review.md). Run tests with the pinned dependencies
in `requirements-dev.txt`; no running Docker daemon is required for unit tests.
