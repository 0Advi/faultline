# Controller API, run evidence and reports (P3)

```bash
make api                      # http://127.0.0.1:8765/api/v1/health
python -m controller.cli serve --allow-origin http://localhost:5173   # add the workbench dev origin
```

The API is a thin layer over the same runner, gates, run store and state
machine as the CLI. It has no separate execution path.

## Endpoints (`/api/v1`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | API/policy version, active and NEEDS_ATTENTION runs |
| GET | `/capabilities`, `/capabilities/{id}` | Typed catalog (adapter config hidden) |
| GET | `/preflight` | Sandbox identity and scope report |
| POST | `/plans` | Propose a validated plan (`capability_id`, `parameters`, `duration_seconds`, `expected_impact`, `baseline_ref`) |
| POST | `/plans/validate` | Validate `{plan}` and return the dry-run actions |
| GET / POST | `/baselines` | List baselines / start one capture job (202) |
| GET | `/runs` | Recent runs |
| POST | `/runs` | `{plan, live:false}` dry run; `{plan, live:true}` → 202 with `run_id` |
| GET | `/runs/{id}` | Authoritative state, `terminal`, `cleanup_status`, result, links |
| GET | `/runs/{id}/events?after_seq=N` | Ordered events after N |
| GET | `/runs/{id}/stream` | Server-sent events; resume with `Last-Event-ID` |
| POST | `/runs/{id}/abort` | Record an abort; restoration follows (202) |
| GET | `/runs/{id}/report`, `/report.md` | Report rebuilt from saved evidence |
| GET | `/runs/{id}/evidence`, `/evidence/{file}` | Exported evidence (allowlisted file names) |

Errors are `{"ok": false, "error": CODE, "message": ...}`. A duplicate
`request_id` or an overlapping run returns 409 with the existing `run_id`;
failed gates return 422 `LIVE_EXECUTION_BLOCKED` with `blockers`, and no run
record is created.

## Access rules

- Binds to loopback only; any other bind address is refused.
- `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding guard).
- POST requires `application/json`, at most 64 KiB, and either no `Origin` or
  an allowlisted one (`--allow-origin`).
- Remote or multi-user access needs a separate authentication design.

## Progress and resume (P3-03)

Events carry contiguous sequence numbers. A client that refreshes or
reconnects reads `GET /runs/{id}`, then streams from its last sequence
number. Closing a stream only stops reading; the run, its restoration and the
watchdog continue. `cleanup_status` is one of `not_started`, `pending`,
`not_required`, `verified` or `needs_attention`, so pending cleanup is never
shown as a completed run.

## Evidence and reports (P3-01)

After every run the controller writes `artifacts/results/<run_id>/`:
`run.json`, `events.jsonl`, `watchdog.log` (if any), `report.json`,
`report.md` and `manifest.json` (sha256 of each file). The report is rebuilt
from `run.json` and `events.jsonl` alone. It has no clock, network or model
input, so `python -m controller.cli report RUN_ID` reproduces it exactly.
Values that could not be measured are listed under missing data. Export
refuses to write fixture card, email or address values.

## Tests (P3-04)

`make test` (and hosted CI) runs everything with fakes: contracts, adapters,
conflicts, duplicate API calls, restart recovery and report consistency. No
Docker, live fault or model credentials are needed. Checks against the
dedicated sandbox are opt-in:

```bash
make test-sandbox
FAULTLINE_SANDBOX_LIVE=1 FAULTLINE_BASELINE_REF=artifacts/baselines/<id>.json make test-sandbox
```

Keep the printed evidence directories as the recorded real-sandbox result.
