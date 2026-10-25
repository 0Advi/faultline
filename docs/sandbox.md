# Dedicated sandbox (P0-01)

Faults may only run on a Docker daemon that hosts nothing except the
`faultline-sandbox` Compose project. A laptop running other containers, or
the shared preview, is never approved isolation.

## Provision

1. Use a dedicated VM or machine (suggested starting capacity: 8 vCPU, 16 GB RAM,
   40 GB disk; measure actual requirements) with
   Docker Engine and Compose 2.24.4+. Do not add other projects to that daemon.
2. Clone the repository, then `make bootstrap validate start verify`.
3. Record the identity:

   ```bash
   docker context show
   docker info --format '{{.ID}}'
   ```

4. Put both values in `config/sandbox.yaml` (`docker_context`, `daemon_id`).
5. Run `make preflight`. Before approval, expect `isolation_approved: false`
   and a nonzero exit. Only when `scope_ok: true` with no identity/scope errors
   may a reviewer (M5) approve a PR that sets `approved: true`. The warning about
   pending approval is not a scope error. Rerun preflight after approval.

Approval records the sandbox identity; it does not enable execution. In P0,
the CLI live runner and flag roundtrip command are blocked independently of
configuration. P1/P2 baseline, observer, watchdog and recovery checks must be
implemented and reviewed before that implementation gate can be removed.

## What the preflight checks

| Check | Rejects |
|---|---|
| Docker context and daemon ID | Any other daemon or context |
| Dedicated daemon | Containers from other Compose projects |
| Exact services | Missing, unexpected or scaled (≠1) services |
| Container settings | Privileged containers, host networking, wrong network |
| Bind mounts | Sources outside `vendor/opentelemetry-demo` and `deploy` |
| Targets | Resolving any container that is not in `mutable_services` |

The upstream `otel-collector` mounts `/` and the Docker socket read-only. These
binds are accepted only for that service and always printed as warnings. They
remain a known isolation limitation.

`config/sandbox.yaml` is committed with `approved: false`, so a fresh clone or
the shared preview fails the preflight by design.
