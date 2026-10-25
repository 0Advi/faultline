"""Read-only evidence: Jaeger traces, Prometheus span metrics, container logs and
application source (sandboxed to the pinned upstream checkout). Sanitized and size-capped."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time
from urllib.request import urlopen

from controller.observer.endpoints import app_url, jaeger_api, metrics_url
from controller.observer.sanitize import sanitize
from controller.observer.telemetry import Telemetry

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "faultline-sandbox"
UPSTREAM = ROOT / "vendor" / "opentelemetry-demo" / "src"
TELEMETRY = Telemetry(jaeger_api(), metrics_url(), timeout=5)

SPAN_COUNTERS = 'sum by (service_name, span_name, status_code) (traces_span_metrics_calls_total)'
SOURCE_EXT = {".js", ".ts", ".go", ".py", ".rb", ".php", ".java", ".kt", ".cs", ".rs", ".cpp", ".h", ".ex", ".exs"}
SKIP_PARTS = {"node_modules", "genproto", "vendor", "tests", "test", "cypress", "obj", "bin", ".next", "__pycache__"}


_HEALTH: dict = {"at": 0.0, "value": None}


def _check_health() -> dict:
    """Check, in parallel, that the shop, Prometheus and Jaeger answer and that no fault is active
    (file and live flagd state).
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import faults

    def ok(url: str) -> bool:
        """True if the URL answers HTTP 200 within 4 s."""
        try:
            with urlopen(url, timeout=4) as resp:
                return resp.status == 200
        except Exception:
            return False
    checks = {"shop": app_url() + "/", "prometheus": metrics_url() + "/-/ready", "jaeger": jaeger_api() + "/services"}
    with ThreadPoolExecutor(4) as pool:
        futures = {name: pool.submit(ok, url) for name, url in checks.items()}
        clean = pool.submit(lambda: faults.flags_clean() and not faults.active_faults())
        status = {name: f.result() for name, f in futures.items()}
        status["flags_baseline"] = clean.result()
    return status


def health(max_age_s: float = 5) -> dict:
    """Cached, parallel health checks so the dashboard never waits on a slow dependency."""
    if _HEALTH["value"] is None or time.monotonic() - _HEALTH["at"] > max_age_s:
        _HEALTH["value"], _HEALTH["at"] = _check_health(), time.monotonic()
    return _HEALTH["value"]


# ------------------------------------------------------------------ metrics
def span_counters() -> dict:
    """Read all span-metric call counters from Prometheus as {'service|span|status': count}."""
    result = TELEMETRY.query(SPAN_COUNTERS) or []
    return {f'{r["metric"].get("service_name")}|{r["metric"].get("span_name")}|'
            f'{r["metric"].get("status_code")}': float(r["value"][1]) for r in result}


def wait_span_counters(before: dict, settle_s: float = 6, limit_s: float = 30) -> dict:
    """Span metrics arrive in flushes; wait until the delta is non-empty and two reads 6 s apart agree."""
    time.sleep(settle_s)
    deadline, last = time.monotonic() + limit_s, span_counters()
    while time.monotonic() < deadline:
        time.sleep(6)
        now = span_counters()
        delta = service_delta(before, now)
        if delta and delta == service_delta(before, last):
            return now
        last = now
    return last


def service_delta(before: dict, after: dict) -> dict:
    """Per-service calls and errors between two counter snapshots (ignores flagd and tiny deltas)."""
    out: dict[str, dict] = {}
    for key, value in after.items():
        service, _span, status = key.split("|")
        delta = value - before.get(key, 0.0)
        if delta < 0.5 or service in ("None", "flagd"):
            continue
        entry = out.setdefault(service, {"calls": 0, "errors": 0})
        entry["calls"] += round(delta)
        if status == "STATUS_CODE_ERROR":
            entry["errors"] += round(delta)
    return out


def counter_delta(before: dict, after: dict, limit: int = 30) -> list[dict]:
    """List the ERROR span counters that grew between two snapshots, largest first (flagd excluded)."""
    rows = []
    for key, value in after.items():
        delta = value - before.get(key, 0.0)
        service, span, status = key.split("|")
        if delta >= 0.5 and status == "STATUS_CODE_ERROR" and "flagd" not in span:
            rows.append({"service": service, "span": span, "errors": round(delta)})
    rows.sort(key=lambda r: -r["errors"])
    return rows[:limit]


# ------------------------------------------------------------------- traces
def failed_traces(items: list[tuple[str, str]]) -> list[dict]:
    """Fetch each failed request's trace from Jaeger and summarise its error spans, innermost first;
    marks traces that were not found.
    """
    out = []
    for endpoint, trace_id in items:
        summary = TELEMETRY.trace(trace_id)
        if not summary:
            out.append({"endpoint": endpoint, "trace_id": trace_id, "found": False})
            continue
        chain = [{"service": s["service"], "operation": s["operation"], "duration_ms": s["duration_ms"],
                  "rpc_code": s["rpc_code"], "http_status": s["http_status"],
                  "message": (s.get("status_message") or "")[:240]}
                 for s in sorted(summary["error_spans"], key=lambda s: s["duration_ms"])]
        out.append({"endpoint": endpoint, "trace_id": trace_id, "found": True, "services": summary["services"],
                    "span_count": summary["span_count"], "error_chain": chain[:10]})
    return out


# --------------------------------------------------------------------- logs
_NOISE = re.compile(r"flagd|EventStream|otlp|exporter|opamp", re.I)


def service_logs(service: str, since_iso: str, limit: int = 6) -> list[dict]:
    """Distinct warn/error log messages from a sandbox container since the run started."""
    try:
        ids = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}",
             "--filter", f"label=com.docker.compose.service={service}"],
            capture_output=True, text=True, timeout=10, check=True).stdout.split()
        if len(ids) != 1:
            return []
        out = subprocess.run(["docker", "logs", "--since", since_iso, ids[0]],
                             capture_output=True, text=True, timeout=15)
        text = out.stdout + "\n" + out.stderr
    except (OSError, subprocess.SubprocessError):
        return []
    seen: dict[str, dict] = {}
    for line in text.splitlines():
        message, level = _parse(line)
        if not message or level not in ("warn", "warning", "error", "fatal", "critical") or _NOISE.search(message):
            continue
        key = re.sub(r"[0-9a-f]{16,}|\d+", "#", message)[:160]
        entry = seen.setdefault(key, {"level": level, "message": sanitize(message)[:300], "count": 0})
        entry["count"] += 1
    return sorted(seen.values(), key=lambda e: -e["count"])[:limit]


def _parse(line: str) -> tuple[str, str]:
    """Extract (message, level) from one container log line, whether JSON (pino/zap style) or plain
    text.
    """
    try:
        doc = json.loads(line)
        if not isinstance(doc, dict):
            raise ValueError
    except ValueError:
        lowered = line.lower()
        level = next((lv for lv in ("error", "fatal", "warn", "exception") if lv in lowered[:120]), "")
        return line.strip()[:400], {"exception": "error"}.get(level, level)
    level = str(doc.get("level") or doc.get("severity") or doc.get("levelname") or "").lower()
    if level.isdigit():
        level = {"40": "warn", "50": "error", "60": "fatal"}.get(level, "info")
    err = doc.get("err")
    message = str(doc.get("msg") or doc.get("message") or "")
    if isinstance(err, dict) and err.get("message") and err["message"] not in message:
        message = f"{message} {err['message']}".strip()
    return message, level


# ------------------------------------------------------------------- source
def _service_dir(service: str) -> Path:
    """Resolve a service's source directory inside the pinned upstream checkout, refusing anything
    outside it.
    """
    path = (UPSTREAM / service).resolve()
    if UPSTREAM.resolve() not in path.parents or not path.is_dir():
        raise PermissionError(f"no source for {service}")
    return path


def _safe(rel: str) -> Path:
    """Resolve a source file path inside the pinned upstream checkout, refusing paths outside it or
    non-source files.
    """
    path = (UPSTREAM / rel).resolve()
    if UPSTREAM.resolve() not in path.parents or not path.is_file():
        raise PermissionError(f"{rel} is not an application source file")
    if path.suffix not in SOURCE_EXT and path.name not in ("Dockerfile",):
        raise PermissionError(f"{rel} is not a source file")
    return path


def list_source(service: str) -> list[str]:
    """List up to 60 application source files of a service (no tests, generated or vendored code),
    relative to src/.
    """
    root = _service_dir(service)
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in SOURCE_EXT
             and not SKIP_PARTS & set(p.relative_to(root).parts) and "_pb" not in p.name
             and ".pb." not in p.name and p.stat().st_size < 200_000]
    return sorted(str(p.relative_to(UPSTREAM)) for p in files)[:60]


def read_source(rel: str, start: int = 1, lines: int = 120) -> str:
    """Return numbered lines of an application source file (at most 200), with a header showing the
    range.
    """
    text = _safe(rel).read_text(errors="replace").splitlines()
    start = max(1, start)
    chunk = text[start - 1:start - 1 + min(lines, 200)]
    return f"{rel} (lines {start}-{start + len(chunk) - 1} of {len(text)})\n" + \
        "\n".join(f"{start + i}: {line}" for i, line in enumerate(chunk))


def search_source(service: str, pattern: str) -> list[str]:
    """Regex search (case-insensitive) through a service's source files; returns up to 25
    'file:line: text' hits.
    """
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    hits = []
    for rel in list_source(service):
        for n, line in enumerate((UPSTREAM / rel).read_text(errors="replace").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:160]}")
                if len(hits) >= 25:
                    return hits
    return hits
