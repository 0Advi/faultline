"""Network view of the sandbox and network evidence for experiments (read-only).

  inventory()        subnet, gateway, every container's IP / MAC, listening TCP ports and traffic
  RunNetwork         TCP counters of the fault's container sampled per phase (retransmissions,
                     connect failures, resets, RTT) and the tc / iptables rules seen while active
  edge_timing()      per hop (caller -> callee) network + transport time from Jaeger traces:
                     client span duration minus the matching server span duration
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import re
import statistics
import subprocess
import time

from . import netem
from .evidence import TELEMETRY
from .faults import NETWORK, PROJECT, container

_CACHE: dict = {"at": 0.0, "value": None}


def _docker(*args: str, timeout: float = 30) -> str:
    """Run a docker command and return stdout (raises on failure)."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:300])
    return result.stdout


def _service(name: str) -> str:
    """Compose service name from a container name like faultline-sandbox-cart-1."""
    return re.sub(rf"^{re.escape(PROJECT)}-|-\d+$", "", name.lstrip("/"))


def inventory(max_age_s: float = 120) -> dict:
    """The sandbox network: subnet, gateway and per-container IP, MAC, listening ports and bytes
    received / sent (cached; reading ports takes one short helper run per container)."""
    if _CACHE["value"] is not None and time.monotonic() - _CACHE["at"] < max_age_s:
        return {**_CACHE["value"], "active_rules": active_fault_rules()}
    doc = json.loads(_docker("network", "inspect", NETWORK))[0]
    ipam = (doc.get("IPAM") or {}).get("Config") or [{}]
    rows = []
    for cid, c in (doc.get("Containers") or {}).items():
        if not c.get("Name", "").startswith(PROJECT):
            continue                                  # short-lived helpers, not sandbox services
        rows.append({"service": _service(c["Name"]), "container": cid[:12],
                     "ip": (c.get("IPv4Address") or "").split("/")[0], "mac": c.get("MacAddress")})
    with ThreadPoolExecutor(8) as pool:
        ports = dict(zip([r["service"] for r in rows],
                         pool.map(lambda r: _safe(netem.listening_ports, r["container"]) or [], rows)))
    try:
        io = netem.parse_netio(_docker("stats", "--no-stream", "--format", "{{.Name}} {{.NetIO}}", timeout=40))
    except Exception:
        io = {}
    for r in rows:
        r["ports"] = ports.get(r["service"], [])
        rx_tx = next((v for k, v in io.items() if _service(k) == r["service"]), None)
        r["rx_bytes"], r["tx_bytes"] = (int(rx_tx[0]), int(rx_tx[1])) if rx_tx else (None, None)
    value = {"network": NETWORK, "driver": doc.get("Driver"), "subnet": ipam[0].get("Subnet"),
             "gateway": ipam[0].get("Gateway"), "containers": sorted(rows, key=lambda r: r["service"]),
             "read_at": time.strftime("%H:%M:%S")}
    _CACHE.update(at=time.monotonic(), value=value)
    return {**value, "active_rules": active_fault_rules()}


def active_fault_rules() -> list[dict]:
    """Network faults currently applied, from the fault journal (what, where, which rule)."""
    from .faults import active_faults
    return [{"service": f["service"], "mechanism": f["net"], "peer": f.get("peer"), "dev": f.get("dev"),
             "iptables": [" ".join(r) for r in f.get("iptables", [])]}
            for f in active_faults() if f.get("kind") == "net"]


def _safe(fn, *args):
    """Call fn and return None instead of raising (observation must never break a run)."""
    try:
        return fn(*args)
    except Exception:
        return None


# --------------------------------------------------------------- run evidence
class RunNetwork:
    """TCP behaviour of the container a fault acts on, sampled at baseline start, injection and
    fault end, plus the rules verified in its namespace while the fault was active."""

    def __init__(self, service: str):
        """Observe one sandbox service (the fault's target, or the caller for link faults)."""
        self.service = service
        self.cid = _safe(container, service)
        self.samples: dict[str, dict | None] = {}
        self.rules: dict | None = None

    def sample(self, name: str):
        """Take a named TCP sample (skipped quietly if the container cannot be read)."""
        self.samples[name] = _safe(netem.tcp_sample, self.cid) if self.cid else None

    def capture_rules(self, dev: str | None = None):
        """Record the fault rules present in the namespace right now (verifies the injection)."""
        self.rules = _safe(netem.active_rules, self.cid, dev) if self.cid else None

    def summary(self) -> dict:
        """Baseline vs fault TCP windows, the verified rules and the change in retransmissions."""
        s = self.samples
        base = netem.tcp_window(s.get("baseline"), s.get("inject"))
        fault = netem.tcp_window(s.get("inject"), s.get("fault_end"))
        return {"service": self.service, "baseline": base, "fault": fault, "rules": self.rules}


# ------------------------------------------------------------------ traces
def _tag(span: dict, key: str):
    """Value of a Jaeger span tag (None if absent)."""
    return next((t.get("value") for t in span.get("tags", []) if t.get("key") == key), None)


def hop_times(trace: dict) -> list[dict]:
    """Network + transport time for every cross-service hop of one Jaeger trace: for each server
    span whose parent is a client span in another service, client duration minus server duration."""
    procs = {pid: p.get("serviceName") for pid, p in (trace.get("processes") or {}).items()}
    spans = {s["spanID"]: s for s in trace.get("spans", [])}
    out = []
    for span in spans.values():
        if _tag(span, "span.kind") != "server":
            continue
        parent = next((spans.get(r["spanID"]) for r in span.get("references", [])
                       if r.get("refType") == "CHILD_OF"), None)
        if not parent or _tag(parent, "span.kind") != "client":
            continue
        caller, callee = procs.get(parent["processID"]), procs.get(span["processID"])
        if not caller or not callee or caller == callee:
            continue
        out.append({"edge": f"{caller}->{callee}",
                    "network_ms": round(max(0, parent["duration"] - span["duration"]) / 1000, 2),
                    "server_ms": round(span["duration"] / 1000, 2)})
    return out


def _raw_trace(trace_id: str) -> dict | None:
    """The raw Jaeger trace document (None if Jaeger does not have it)."""
    status, body = TELEMETRY._get(f"{TELEMETRY.jaeger_api}/traces/{trace_id}")
    return body["data"][0] if status == 200 and body and body.get("data") else None


def edge_timing(records: list[dict], services: set[str], per_phase: int = 8) -> list[dict]:
    """Median network vs server time per hop touching the given services, baseline vs fault,
    from a sample of each phase's traced requests."""
    picks = {}
    for phase in ("baseline", "fault"):
        rows = [r for r in records if r["phase"] == phase and r.get("trace_id")]
        step = max(1, len(rows) // per_phase)
        picks[phase] = [r["trace_id"] for r in rows[::step][:per_phase]]
    with ThreadPoolExecutor(6) as pool:
        traces = {p: [t for t in pool.map(lambda i: _safe(_raw_trace, i), ids) if t] for p, ids in picks.items()}
    hops: dict[str, dict[str, list]] = {}
    for phase, docs in traces.items():
        for doc in docs:
            for h in hop_times(doc):
                caller, callee = h["edge"].split("->")
                if caller in services or callee in services:
                    hops.setdefault(h["edge"], {}).setdefault(phase, []).append(h)
    rows = []
    for edge, by in hops.items():
        def med(phase, key):
            """Median of one field for this hop in a phase (None if no sample)."""
            vals = [h[key] for h in by.get(phase, [])]
            return round(statistics.median(vals), 2) if vals else None
        rows.append({"edge": edge, "samples": {p: len(by.get(p, [])) for p in ("baseline", "fault")},
                     "baseline_network_ms": med("baseline", "network_ms"),
                     "fault_network_ms": med("fault", "network_ms"),
                     "baseline_server_ms": med("baseline", "server_ms"),
                     "fault_server_ms": med("fault", "server_ms")})
    rows.sort(key=lambda r: -((r["fault_network_ms"] or 0) - (r["baseline_network_ms"] or 0)))
    return rows[:10]
