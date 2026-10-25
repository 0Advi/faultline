"""One bounded, guarded, self-restoring chaos experiment against the local sandbox.

baseline -> (health gate) -> inject -> fault (guardrails checked every second)
-> restore -> restoring (until every healthy path works again) -> recovery -> evidence

Restoration runs in `finally`, from signal/exit handlers, and on the next server
start via the fault journal, so the shop is never left broken.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import threading
import time
from typing import Callable
from uuid import uuid4

from . import evidence, faults, network, scoring, topology
from .catalog import CONTAINER_TARGETS, ENDPOINTS, SCENARIOS, scenario_view
from .netem import LINK_CALLER
from .workload import Workload

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "artifacts" / "demo-runs"

DEFAULT_GUARDRAILS = {"max_fault_s": 60, "max_unexpected_endpoints": 2, "max_error_rate": 85,
                      "max_unrelated_p95_ms": 6000}
RECOVERY_LIMIT_S = 90
PROPAGATION_S = 2


class Aborted(Exception):
    """Raised to stop a run early: operator kill switch, a tripped guardrail or a failed health
    gate.
    """
    pass


def _now() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


class Experiment:
    """One guarded, self-restoring chaos experiment: drives the workload, injects and restores the
    fault, checks guardrails, measures recovery and collects evidence.
    """
    def __init__(self, params: dict, emit: Callable[[str, dict], None]):
        """Validate the scenario, target and intensity, clamp durations and guardrails to the safety
        limits, and prepare the injection and synthetic workload.
        """
        sid = params.get("scenario", "payment-failure")
        if sid not in SCENARIOS:
            raise ValueError(f"unknown scenario {sid}")
        spec = scenario_view(sid, params.get("target") or None)
        variant = params.get("variant") or spec["default"]
        self.guard = {**DEFAULT_GUARDRAILS, **{k: float(v) for k, v in (params.get("guardrails") or {}).items()
                                               if k in DEFAULT_GUARDRAILS}}
        self.guard["max_fault_s"] = min(self.guard["max_fault_s"], 60)
        self.durations = {"baseline": max(6, min(int(params.get("baseline_s", 10)), 30)),
                          "fault": max(8, min(int(params.get("fault_s", 25)), int(self.guard["max_fault_s"]))),
                          "recovery": max(5, min(int(params.get("recovery_s", 10)), 30))}
        self.injection = faults.Injection(sid, spec["target"], variant)
        self.spec, self.variant, self.emit = spec, variant, emit
        self.run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S-") + uuid4().hex[:4]
        self.stop = threading.Event()
        self.end_fault = threading.Event()   # presenter's "End fault now": restore, run counts as completed
        self._strike = False
        self.timeline: dict[str, float] = {}
        self.label = params.get("label")
        self.parent = params.get("parent")  # run this is a re-run of (after a fix)
        self.proposed_by = params.get("proposed_by")
        self.workload = Workload(lambda row: emit("req", row))
        # Network evidence comes from the namespace the fault acts in: the caller for link faults.
        self.net_service = LINK_CALLER[spec["target"]] if spec.get("net") in ("blackhole", "reset") \
            else spec["target"]
        self.net = network.RunNetwork(self.net_service) if self.net_service in CONTAINER_TARGETS else None

    # ------------------------------------------------------------- helpers
    def mark(self, name: str, **extra):
        """Record when a phase starts (seconds since the workload began) and emit a 'phase' event."""
        self.timeline[name] = self.workload.t()
        self.emit("phase", {"name": name, "t": self.timeline[name], **extra})

    def wait(self, seconds: float, check: Callable[[], None] | None = None, endable: bool = False):
        """Sleep for a phase in 1 s steps, raising Aborted on the kill switch, running the guardrail
        check if given, and returning early on 'End fault now' when endable.
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop.is_set():
                raise Aborted("operator abort (kill switch)")
            if endable and self.end_fault.is_set():
                self.emit("log", {"level": "info", "message": "Fault ended early by the presenter"})
                return
            if check:
                check()
            time.sleep(1)

    def params(self) -> dict:
        """The parameters needed to re-run this exact experiment (used by auto-fix)."""
        return {"scenario": self.spec["id"], "target": self.spec["target"], "variant": self.variant,
                "baseline_s": self.durations["baseline"], "fault_s": self.durations["fault"],
                "recovery_s": self.durations["recovery"], "guardrails": self.guard}

    # ----------------------------------------------------------- guardrails
    def check_guardrails(self):
        """Evaluated every second. A breach must persist for two consecutive checks before the
        run is stopped, so a one-off host stall cannot abort an experiment."""
        window = [r for r in self.workload.window(6) if r["phase"] == "fault"]
        if len(window) < 6:
            return
        breaches = []
        home = [r for r in window if r["endpoint"] == "home"]
        if len(home) >= 3 and sum(r["ok"] for r in home) / len(home) <= 0.2:
            breaches.append("guardrail: shop is down (home page failing)")
        error_rate = 100 * sum(not r["ok"] for r in window) / len(window)
        if error_rate > self.guard["max_error_rate"]:
            breaches.append(f"guardrail: error rate {error_rate:.0f}% > {self.guard['max_error_rate']:.0f}%")
        recent = topology.endpoint_stats([r for r in self.workload.window(10) if r["phase"] == "fault"], "fault")
        recent = {k: v for k, v in recent.items() if v["n"] >= 3}
        expected = {k for k, v in ENDPOINTS.items() if self.spec["target"] in v["services"]}
        unexpected = sorted(set(topology.impacted_endpoints(self.base_stats, recent)) - expected)
        if len(unexpected) > self.guard["max_unexpected_endpoints"]:
            breaches.append(f"guardrail: blast radius exceeded ({', '.join(unexpected)} degraded "
                            "although they do not depend on the target)")
        for ep, st in recent.items():
            if ep not in expected and st["p50"] and st["p50"] > self.guard["max_unrelated_p95_ms"]:
                breaches.append(f"guardrail: {ENDPOINTS[ep]['label']} median latency {st['p50']:.0f} ms > "
                                f"{self.guard['max_unrelated_p95_ms']:.0f} ms")
                break
        if breaches and self._strike:
            raise Aborted(breaches[0])
        if breaches:
            self.emit("log", {"level": "warn", "message": f"Guardrail warning (stops if it persists): {breaches[0]}"})
        self._strike = bool(breaches)

    def recovery_progress(self, since: float) -> tuple[int, int]:
        """(paths healthy again, paths that were healthy in the baseline) since the restore."""
        rows = [r for r in self.workload.records if r["t"] >= since]
        wanted = [ep for ep, base in self.base_stats.items() if base["n"] and base["success"] >= 90]
        healthy = 0
        for ep in wanted:
            mine = [r for r in rows if r["endpoint"] == ep]
            healthy += bool(mine and mine[-1]["ok"])
        return healthy, len(wanted)

    def healthy_again(self, since: float) -> bool:
        """True once every path that was healthy in the baseline has succeeded on its latest request
        since the restore.
        """
        healthy, wanted = self.recovery_progress(since)
        return healthy == wanted

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        """Run the whole experiment: baseline, health gate, inject, guarded fault window, restore,
        measure recovery, then collect evidence, score and save the run record.
        """
        spec = self.spec
        self.record = {"run_id": self.run_id, "scenario": spec["id"], "title": spec["title"],
                       "category": spec["category"], "kind": spec["kind"], "target": spec["target"],
                       "variant": self.variant, "hypothesis": spec["hypothesis"], "params": self.params(),
                       "guardrails": self.guard, "durations": self.durations, "label": self.label,
                       "parent": self.parent, "proposed_by": self.proposed_by,
                       "started_at": _now(), "status": "running"}
        self.emit("run", self.record)
        graph = topology.graph()
        spans_before = evidence.span_counters()
        outcome, reason, restore_results, applied = "completed", None, [], False
        self.workload.start()
        try:
            self.mark("baseline")
            self._net_sample("baseline")
            self.wait(self.durations["baseline"])
            # Health gate: every path needs >= 3 samples at >= 80 % success. A host stall can
            # leave a path with one slow sample, so extend the baseline (up to 30 s) before refusing.
            extra = 0
            while True:
                self.base_stats = topology.endpoint_stats(self.workload.records, "baseline")
                sick = [ENDPOINTS[k]["label"] for k, v in self.base_stats.items()
                        if v["n"] < 3 or v["success"] < 80]
                if not sick:
                    break
                if extra >= 30:
                    raise Aborted(f"health gate: baseline unhealthy ({', '.join(sick)}); not injecting")
                if extra == 0:
                    self.emit("log", {"level": "info", "message": "Baseline not steady yet "
                                      f"({', '.join(sick)}); extending the baseline..."})
                self.wait(5)
                extra += 5

            # Applying a fault (e.g. a flagd config push) briefly stalls services by itself; those
            # requests are labelled "propagation" and kept out of the fault statistics.
            self.workload.phase = "propagation"
            self._net_sample("inject")
            self.mark("inject", description=self.injection.description)
            applied = True
            self.injection.apply()
            started = time.monotonic()
            if spec["kind"] == "net" and self.net:
                self.net.capture_rules(next((u.get("dev") for u in self.injection.undos
                                             if u.get("kind") == "net"), None))
                rules = (self.net.rules or {})
                shown = "; ".join(rules.get("qdisc", []) + rules.get("iptables", [])) or "none found"
                self.emit("log", {"level": "info" if rules.get("qdisc") or rules.get("iptables") else "warn",
                                  "message": f"Verified in {self.net_service}'s network namespace: {shown}"})
            self.wait(max(0.0, PROPAGATION_S - (time.monotonic() - started)))
            self.workload.phase = "fault"
            self.mark("fault")
            self.wait(self.durations["fault"], self.check_guardrails, endable=True)
        except Aborted as error:
            outcome, reason = "aborted", str(error)
            self.emit("log", {"level": "warn", "message": str(error)})
        except Exception as error:
            outcome, reason = "error", f"{type(error).__name__}: {error}"
            self.emit("log", {"level": "error", "message": reason})
        finally:
            if applied:
                self._net_sample("fault_end")
            restore_results = self.injection.restore()
            if applied:
                self.workload.phase = "restoring"
                self.mark("restore", results=restore_results)

        ttr = None
        if applied:
            restored_at = self.timeline["restore"]
            deadline = time.monotonic() + RECOVERY_LIMIT_S
            last_note = 0.0
            while time.monotonic() < deadline and not self.stop.is_set():
                time.sleep(0.5)
                if self.healthy_again(restored_at):
                    ttr = round(self.workload.t() - restored_at, 2)
                    break
                if time.monotonic() - last_note > 3:   # clients may need a while to reconnect
                    last_note = time.monotonic()
                    healthy, wanted = self.recovery_progress(restored_at)
                    self.emit("log", {"level": "info", "message": f"Fault removed; waiting for the shop to "
                                      f"heal ({healthy}/{wanted} customer paths healthy again)"})
            self.mark("recovered" if ttr is not None else "not_recovered", ttr=ttr)
            self.workload.phase = "recovery"
            try:
                self.wait(self.durations["recovery"])
            except Aborted:
                pass
        self.workload.halt()

        self.mark("evidence")
        self.emit("log", {"level": "info", "message": "Collecting traces, metrics, logs and container state..."})
        spans_after = evidence.wait_span_counters(spans_before)
        self.record.update(self.summarize(outcome, reason, restore_results, applied, ttr, graph,
                                          spans_before, spans_after))
        self.save()
        self.emit("evidence", self.record)
        return self.record

    def summarize(self, outcome, reason, restore_results, applied, ttr, graph, before, after) -> dict:
        """Build the run's results: per-phase and per-path stats, blast radius, resilience score,
        time to impact and recovery, traces, logs, container state and span-metric deltas.
        """
        records = self.workload.records
        stats = {p: topology.endpoint_stats(records, p) for p in ("baseline", "fault", "restoring", "recovery")}
        span_errors = evidence.service_delta(before, after)
        blast = topology.blast_radius(self.spec["target"], graph["edges"], stats["baseline"], stats["fault"],
                                      span_errors)
        fault_rows = [r for r in records if r["phase"] == "fault"]
        first_fail = min((r["t0"] for r in records if r["phase"] in ("propagation", "fault") and not r["ok"]),
                         default=None)
        totals = {}
        for phase in ("baseline", "propagation", "fault", "restoring", "recovery"):
            rows = [r for r in records if r["phase"] == phase]
            totals[phase] = {"n": len(rows), "ok": sum(r["ok"] for r in rows),
                             "success": round(100 * sum(r["ok"] for r in rows) / len(rows), 1) if rows else None}
        failed = sorted((r for r in fault_rows if not r["ok"] and r["trace_id"]),
                        key=lambda r: (r["endpoint"] not in blast["expected_endpoints"], r["t"]))
        picked, seen = [], set()
        for r in failed:
            if r["endpoint"] not in seen:
                picked.append(r)
                seen.add(r["endpoint"])
        log_services = [self.spec["target"]] + [s for s, v in sorted(span_errors.items(), key=lambda kv: -kv[1]["errors"])
                                                if v["errors"] and s != self.spec["target"]][:3]
        return {
            "status": outcome, "abort_reason": reason, "injected": applied,
            "injection": self.injection.description, "restore_results": restore_results,
            "finished_at": _now(), "timeline": self.timeline, "totals": totals, "endpoint_stats": stats,
            "time_to_first_failure_s": round(first_fail - self.timeline["inject"], 2)
            if first_fail is not None and "inject" in self.timeline else None,
            "time_to_recover_s": ttr, "blast_radius": blast,
            "resilience": scoring.score(records, blast, ttr, outcome == "aborted") if applied else None,
            "span_errors": span_errors, "span_detail": evidence.counter_delta(before, after),
            "traces": evidence.failed_traces([(r["endpoint"], r["trace_id"]) for r in picked[:3]]),
            "logs": {svc: evidence.service_logs(svc, self.record["started_at"]) for svc in dict.fromkeys(log_services)},
            "containers": {svc: faults.container_state(svc) for svc in dict.fromkeys(log_services)},
            "requests": records, "journeys": self.workload.journeys,
            "flags_clean": faults.flags_clean(), "active_faults_left": len(faults.active_faults()),
            "network": {**self.net.summary(),
                        "hops": network.edge_timing(records, {self.spec["target"], self.net_service})}
            if self.net and applied else None,
        }

    def _net_sample(self, name: str):
        """Sample the fault container's TCP counters (no-op when it has no observable namespace)."""
        if self.net:
            self.net.sample(name)

    def save(self):
        """Write the run record to artifacts/demo-runs/<run_id>.json."""
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        (RUNS_DIR / f"{self.run_id}.json").write_text(json.dumps(self.record, default=str))


def wait_until_healthy(emit: Callable[[str, dict], None], limit_s: float = 120) -> bool:
    """Gate between experiments: every customer path succeeds and flagd serves only 'off'."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        probe = Workload(lambda row: None, pause_s=0.3)
        probe.start()
        time.sleep(4)
        probe.halt()
        latest = {}
        for r in probe.records:
            latest[r["endpoint"]] = r["ok"]
        sick = [ENDPOINTS[k]["label"] for k in ENDPOINTS if not latest.get(k)]
        if not sick and faults.flags_clean():
            return True
        emit("log", {"level": "info", "message": f"Waiting for steady state before the next experiment "
                                                 f"({', '.join(sick) or 'flags'} not healthy yet)..."})
        time.sleep(5)
    return False


def install_restore_handlers():
    """Make sure every journaled fault is restored when the process exits or receives Ctrl-C /
    SIGTERM.
    """
    def handler(signum, _frame):
        """Signal handler: restore all faults, then exit with the conventional signal exit code."""
        faults.restore_all()
        raise SystemExit(128 + signum)
    atexit.register(faults.restore_all)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
