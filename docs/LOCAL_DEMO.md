# Run the local payment demo (workbench)

> This runs the earlier operator workbench on port 8095. For the AI Chaos Lab used in the
> presentation, see [PRESENTATION.md](PRESENTATION.md).

Start Docker Desktop, then run these commands from the repository with its Python environment active:

```sh
make start smoke
python scripts/local_workbench.py
```

The connected workbench uses port 8095. On this machine, `deploy/demo.local.env`
sets the shop port to 8081; Grafana, Jaeger, Locust and feature flags are under
that same shop origin. Prometheus uses port 9090.

`make start` compiles a static TCP payment health probe and requires Go 1.20+.
It uses the Docker daemon architecture, including Apple Silicon. The health
probe avoids loading payment's Node instrumentation every five seconds.
Checkout and product catalog each have a 128 MiB sandbox memory limit so their
service processes and health probes have room to run together. Changing these
budgets requires new baselines and a newly derived payment policy.

Host-specific approval is stored in ignored `config/sandbox.local.yaml` and
`config/execution_policy.local.yaml`. The launcher supplies these explicitly
through `FAULTLINE_SANDBOX_SPEC` and `FAULTLINE_EXECUTION_POLICY`. Repository defaults
remain disabled. A different Docker daemon, unrelated containers, missing
services, stale baseline, changed flags, or missing measured policy blocks a run.

For CLI operations with the same local configuration:

```sh
export FAULTLINE_SANDBOX_SPEC="$PWD/config/sandbox.local.yaml"
export FAULTLINE_EXECUTION_POLICY="$PWD/config/execution_policy.local.yaml"
make preflight
```

After restarting or changing the runtime, check health and capture two fresh
baselines with `make baseline`. Each baseline records 30 valid checkouts,
protected home/catalog/cart requests, invalid-card controls, real traces,
indexed logs, metrics and container state. Derive thresholds with:

```sh
make derive-policy BASELINES="artifacts/baselines/FIRST.json artifacts/baselines/SECOND.json"
```

The two measurements must be healthy and reproducible. Keep background Locust
traffic stopped for this measured demo profile. This machine's local environment
sets `LOCUST_AUTOSTART=false`; the Locust interface can start traffic separately
for a later load experiment, followed by new baselines.

In the workbench, refresh readiness, choose the enabled payment capability,
validate the plan, then start it. Observe checkout failures and protected paths,
automatic flag restoration, recovery checks, the report and free rule diagnosis.
Use the emergency restore control to abort an active run. Evidence is stored in
`artifacts/results/<run-id>/`; measured baselines are under `artifacts/baselines/`.

A real fault run proves injection, observation and restoration for that tested
scenario. Preparing a source patch is available; automatic image deployment and
measured replay remain separate unfinished work and must not be claimed as live.
