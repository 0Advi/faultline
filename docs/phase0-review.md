# Phase 0 integration review

Reviewed 10 October 2026 against IMPLEMENTATION_PLAN.md. Source branch:
`p0-contracts-and-execution-boundary/10Oct`, commit `1b76363`.
`bipranshu` contains the same implementation tree.

## Accepted foundation

| Task | Evidence |
|---|---|
| P0-01 | Runtime identity, project/service/mount allowlists, positive and negative scope fixtures; current preview rejected |
| P0-02 | Eight versioned record models and exported schemas; fixtures for every record, state transitions and event resumption |
| P0-03 | Pinned healthy flag definitions under deploy/flagd; shared read-only mounts; atomic replacement visible to flagd; exact state/variant restoration tests |
| P0-04 | Typed capabilities, structured unsupported errors, strict proposal inputs and bounded tool registry; execution unavailable |

P0 acceptance covers the foundation. Dedicated runtime approval remains pending.
Testing fault values, active-run writer exclusivity and crash recovery depends on
P1/P2; those requirements are not claimed as demonstrated here.

## Integration corrections

- Restored the project brief and its README link, which the source branch deleted.
- Blocked live runner entry regardless of configuration. The draft runner lacks
  an independent watchdog, fresh measured baseline and protected-path aborts.
- Blocked the verifier's flag-mutation roundtrip before file or Docker access.
- Require literal approval booleans and reject writable collector host exceptions.
- Reject coerced durations and baseline paths outside relative artifacts storage.
- Restore a flag's original ENABLED/DISABLED state as well as its variant;
  compare healthy definitions to pinned upstream values instead of trusting labels.
- Return structured CLI errors and add pinned contract/schema/flag checks to CI.

## Verification

- 93 unit tests passed using requirements-dev.txt; no Docker required.
- All eight generated schema exports match; whitespace checks pass.
- Deployment configuration validation passed for the 25-service selection.
- Static healthy flag definitions and read-only runtime mount checks passed.
- Runtime evaluation returned paymentFailure=0 and paymentUnreachable=false.
- An identical-content atomic flag-file replacement preserved healthy values and
  produced a flagd file-change event. Only flagd and flagd-ui were recreated to
  adopt the reviewed mounts. No application fault was activated.
- Preflight correctly rejected the shared Docker Desktop preview: configured
  context/daemon identity is unapproved and bind paths require runtime validation.
- The attempted final shop smoke check was not executed because automatic approval
  review hit a usage limit. Existing startup evidence remains historical.

## Next milestone

Start P1: fixed synthetic payment workload, observed dependency map, telemetry
freshness and a measured healthy baseline. Resolve dedicated runtime identity and
capacity before P2 execution. The runner's deferred live path requires a complete
P2 review and abort/watchdog/recovery tests before removing the hard block.
The agent model, workbench, diagnosis, patches and replay are later milestones.
