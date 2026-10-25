"""Local controller API (P3-02/P3-03).

Every mutation goes through the same runner validation, gates, run store and
state machine as the CLI; the API adds no execution path of its own.

Access: binds to loopback only; rejects foreign Host headers (DNS rebinding),
cross-origin mutations from origins not on the allowlist, non-JSON mutation
bodies and bodies over 64 KiB. Remote access needs a separate auth design.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import threading
import time
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

from ..agent.tools import ToolRegistry
from ..catalog import Unsupported, load_policy
from ..executor import runner
from ..executor.environment import Environment, LiveEnvironment
from ..executor.limits import RunLimits
from ..report.build import build_report
from ..report.evidence import FILES, export_run, run_dir
from ..report.render import to_markdown
from ..schemas import Baseline, Plan, RunState as S, TERMINAL
from ..store import RunStore

ROOT = Path(__file__).resolve().parents[2]
API_VERSION = "1.0.0"
MAX_BODY = 64 * 1024
STATUS = {
    "INVALID_JSON": 400, "INVALID_ARGUMENTS": 400, "INVALID_PLAN": 400, "INVALID_INPUT": 400,
    "HOST_REJECTED": 403, "ORIGIN_REJECTED": 403,
    "NOT_FOUND": 404, "UNKNOWN_RUN": 404, "UNSUPPORTED_CAPABILITY": 404,
    "DUPLICATE_REQUEST": 409, "RUN_IN_PROGRESS": 409, "NOT_ACTIVE": 409, "JOB_IN_PROGRESS": 409,
    "NOT_FINISHED": 409,
    "PAYLOAD_TOO_LARGE": 413, "UNSUPPORTED_MEDIA_TYPE": 415,
}


class ApiError(Exception):
    """An API failure with a stable error code, a message, an HTTP status and extra details."""
    def __init__(self, code: str, message: str, status: Optional[int] = None, **details):
        """Create the error; the HTTP status defaults from the code (422 when unknown)."""
        super().__init__(message)
        self.code, self.message, self.details = code, message, details
        self.status = status or STATUS.get(code, 422)

    def body(self) -> dict:
        """The JSON error body returned to the client."""
        return {"ok": False, "error": self.code, "message": self.message, **self.details}


def _loopback(host: str) -> bool:
    """True if the host is a loopback address or 'localhost'."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class ControllerAPI:
    """Application state shared by request threads; one instance per process."""

    def __init__(self, *, db_path: Path = runner.DB_PATH, results_dir: Optional[Path] = None,
                 env_factory: Callable[[], Environment] = LiveEnvironment,
                 limits: Optional[RunLimits] = None,
                 gate_check: Optional[Callable] = None, allowed_origins: tuple[str, ...] = (),
                 baseline_runner: Optional[Callable[[], tuple[Baseline, Path]]] = None,
                 baselines_dir: Path = ROOT / "artifacts" / "baselines", root: Path = ROOT,
                 stream_poll: float = 0.25, log: Callable[[str], None] = lambda line: None,
                 provider_factory: Optional[Callable] = None, telemetry_factory: Optional[Callable] = None):
        """Configure the API's run store, results directory, sandbox environment, limits, gates,
        allowed origins, baseline runner, model and telemetry factories.
        """
        self.db_path, self.results_dir = db_path, results_dir or db_path.parent
        self.env_factory, self.limits, self.gate_check = env_factory, limits, gate_check
        self.allowed_origins = set(allowed_origins)
        self.baseline_runner, self.baselines_dir, self.root = baseline_runner, baselines_dir, root
        self.stream_poll, self.log = stream_poll, log
        self.provider_factory, self.telemetry_factory = provider_factory, telemetry_factory
        self.run_threads: dict[str, threading.Thread] = {}
        self.baseline_job: dict = {}
        self.lock = threading.Lock()
        RunStore(db_path)  # create schema once

    def store(self) -> RunStore:
        """A fresh run-store connection (one per request, so SQLite transactions never interleave
        across threads).
        """
        # One SQLite connection per request: transactions must not interleave across threads.
        return RunStore(self.db_path)

    # ------------------------------------------------------------- views
    def run_view(self, run: dict, events=None) -> dict:
        """Summary of a run for API clients: state, stop reason, cleanup status, abort flag, last
        event and links to its sub-resources.
        """
        store_events = events if events is not None else self.store().events(run["run_id"])
        report = build_report(run, store_events, self.root)
        return {"run_id": run["run_id"], "capability_id": run["capability_id"], "state": run["state"],
                "stop_reason": run["stop_reason"], "terminal": S(run["state"]) in TERMINAL,
                "cleanup_status": report.cleanup_status, "abort_requested": bool(run["abort_requested"]),
                "last_seq": run["last_seq"], "result": run["result"], "restorer": run["restorer"],
                "links": {k: f"/api/v1/runs/{run['run_id']}{v}" for k, v in
                          {"self": "", "events": "/events", "stream": "/stream", "report": "/report",
                           "evidence": "/evidence"}.items()}}

    # ------------------------------------------------------------ handlers
    def health(self, _req) -> dict:
        """API health: version, policy version, active runs and runs needing attention."""
        active = self.store().active_runs()
        return {"ok": True, "api_version": API_VERSION, "policy_version": load_policy()["policy_version"],
                "active_runs": [r["run_id"] for r in active],
                "needs_attention": [r["run_id"] for r in active if r["state"] == S.NEEDS_ATTENTION.value]}

    def capabilities(self, _req) -> dict:
        """GET /capabilities: the capability catalog."""
        return self._tool("list_capabilities", {})

    def capability(self, _req, cap_id: str) -> dict:
        """GET /capabilities/<id>: one capability's contract."""
        return self._tool("describe_capability", {"capability_id": cap_id})

    def preflight(self, _req) -> dict:
        """GET /preflight: the sandbox identity and scope report."""
        return {"ok": True, **self.env_factory().sandbox_report().as_dict()}

    def propose(self, req) -> tuple[int, dict]:
        """POST /plans: build a validated user plan for a capability (nothing executes)."""
        body = req.json()
        baseline_ref = body.pop("baseline_ref", None) if isinstance(body, dict) else None
        plan = self._tool("propose_plan", body)["result"]
        plan.update(requested_by="user", baseline_ref=baseline_ref)
        try:
            return 201, {"ok": True, "plan": Plan.model_validate(plan).model_dump(mode="json")}
        except ValidationError as error:
            raise ApiError("INVALID_PLAN", "plan does not match the contract", problems=_problems(error))

    def validate_plan(self, req) -> dict:
        """POST /plans/validate: dry-run a plan through every gate."""
        plan = self._plan(req.json().get("plan"))
        return {"ok": True, **runner.dry_run(plan)}

    def list_baselines(self, _req) -> dict:
        """GET /baselines: saved baselines with health, finish time and blocking issues, newest
        first.
        """
        rows = []
        for path in sorted(self.baselines_dir.glob("baseline-*.json"), reverse=True):
            if path.name.endswith(".observations.jsonl"):
                continue
            try:
                b = Baseline.model_validate_json(path.read_text())
            except (ValueError, OSError):
                continue
            rows.append({"baseline_id": b.baseline_id, "file": str(path.relative_to(self.root))
                         if path.is_relative_to(self.root) else str(path), "healthy": b.healthy,
                         "finished_at": b.finished_at.isoformat(), "blocking": b.blocking[:5],
                         "checkout_p95_ms": b.probe("POST /api/checkout").p95_ms})
        return {"ok": True, "baselines": rows, "job": self._job_view()}

    def start_baseline(self, req) -> tuple[int, dict]:
        """POST /baselines: start a baseline capture job."""
        req.json()  # mutation requests must carry a JSON body (may be {})
        return 202, self.start_baseline_job()

    def start_baseline_job(self) -> dict:
        """Start a background baseline capture unless one is running or a fault run is active."""
        if self.baseline_runner is None:
            from ..cli import live_baseline_runner
            self.baseline_runner = live_baseline_runner
        with self.lock:
            if self.baseline_job.get("state") == "running":
                raise ApiError("JOB_IN_PROGRESS", "a baseline capture is already running")
            if self.store().active_runs():
                raise ApiError("RUN_IN_PROGRESS", "baselines are not captured while a run is active")
            self.baseline_job = {"job_id": f"job-{int(time.time())}", "state": "running"}
        threading.Thread(target=self._baseline_job, daemon=True).start()
        return {"ok": True, "job": self._job_view()}

    def list_runs(self, req) -> dict:
        """GET /runs: the most recent runs."""
        limit = int(req.query.get("limit", ["20"])[0])
        return {"ok": True, "runs": [self.run_view(r) for r in self.store().recent_runs(limit)]}

    def create_run(self, req) -> tuple[int, dict]:
        """POST /runs: dry-run a plan, or start it live when live is true and every gate passes."""
        body = req.json()
        plan = self._plan(body.get("plan"))
        if "live" in body and not isinstance(body["live"], bool):
            raise ApiError("INVALID_INPUT", "live must be a boolean")
        if body.get("live") is not True:
            return 200, {"ok": True, **runner.dry_run(plan)}
        return 202, self.start_run(plan, live=True)

    # ------------------------------------------- controller service (API + agent tools)
    def start_run(self, plan: Plan, live: bool = True) -> dict:
        """The single entry point for live runs, shared by HTTP requests and agent tools."""
        if not live:
            return {"ok": True, **runner.dry_run(plan)}
        with self.lock:
            if self.baseline_job.get("state") == "running":
                raise ApiError("JOB_IN_PROGRESS", "wait for baseline capture to finish")
            try:
                run, cap, params = runner.prepare(plan, env=self.env_factory(), store=self.store(),
                                                  limits=self.limits, gate_check=self.gate_check, log=self.log)
                run_id = run.create(plan)
            except Unsupported as refusal:
                raise ApiError(refusal.code, refusal.message, **refusal.details)
        thread = threading.Thread(target=self._drive, args=(run, run_id, plan, cap, params),
                                  name=f"run-{run_id}", daemon=True)
        self.run_threads[run_id] = thread
        thread.start()
        return {"ok": True, **self.run_view(self.store().get(run_id))}

    def run_summary(self, run_id: str) -> dict:
        """Summary of one run."""
        return self.run_view(self._run(run_id))

    def run_events(self, run_id: str, after_seq: int = 0) -> list[dict]:
        """A run's events after a sequence number."""
        self._run(run_id)
        return [e.model_dump(mode="json") for e in self.store().events(run_id, after_seq)]

    def run_report(self, run_id: str) -> dict:
        """A run's report as a dict."""
        return self.report(None, run_id)["report"]

    def abort_run(self, run_id: str) -> dict:
        """Record an abort request for an active run (the controller or watchdog restores and
        verifies).
        """
        self._run(run_id)
        if not self.store().request_abort(run_id):
            raise ApiError("NOT_ACTIVE", "run is already finished")
        return {"ok": True, "run_id": run_id,
                "note": "abort recorded; the controller or watchdog restores and verifies recovery"}

    # ------------------------------------------------------- P4 diagnosis
    def diagnose(self, req, run_id: str) -> tuple[int, dict]:
        """POST /runs/<id>/diagnosis: diagnose a finished run with the model or the rule baseline."""
        body = req.json()
        diagnoser = body.get("diagnoser", "model")
        if diagnoser not in ("model", "baseline"):
            raise ApiError("INVALID_INPUT", "diagnoser must be model or baseline")
        run = self._run(run_id)
        if not (S(run["state"]) in TERMINAL or run["state"] == S.NEEDS_ATTENTION.value):
            raise ApiError("NOT_FINISHED", "diagnose a run after it has finished restoring", status=409)
        from ..agent.service import diagnose_run
        provider = (self.provider_factory or _default_provider)() if diagnoser == "model" else None
        telemetry = self.telemetry_factory() if self.telemetry_factory else None
        result = diagnose_run(self.store(), run_id, self.results_dir, diagnoser, provider, telemetry)
        status = 502 if result.status == "failed" else 200
        return status, {"ok": result.status != "failed", "diagnosis": result.model_dump(mode="json")}

    def get_diagnosis(self, _req, run_id: str) -> dict:
        """GET /runs/<id>/diagnosis: the saved diagnosis."""
        self._run(run_id)
        path = run_dir(self.results_dir, run_id) / "diagnosis.json"
        if not path.is_file():
            raise ApiError("NOT_FOUND", "no diagnosis yet; POST to this URL to create one")
        return {"ok": True, "diagnosis": json.loads(path.read_text())}

    def get_run(self, _req, run_id: str) -> dict:
        """GET /runs/<id>: one run."""
        return {"ok": True, **self.run_view(self._run(run_id))}

    def events(self, req, run_id: str) -> dict:
        """GET /runs/<id>/events: events after after_seq."""
        self._run(run_id)
        after = int(req.query.get("after_seq", ["0"])[0])
        events = self.store().events(run_id, after)
        return {"ok": True, "run_id": run_id, "after_seq": after,
                "events": [e.model_dump(mode="json") for e in events]}

    def abort(self, req, run_id: str) -> tuple[int, dict]:
        """POST /runs/<id>/abort: request an abort."""
        req.json()
        return 202, self.abort_run(run_id)

    def report(self, req, run_id: str, fmt: str = "json"):
        """GET /runs/<id>/report: the run report as JSON or Markdown (exports evidence first when
        the run has finished).
        """
        run = self._run(run_id)
        if S(run["state"]) in TERMINAL or run["state"] == S.NEEDS_ATTENTION.value:
            export_run(self.store(), run_id, self.results_dir, self.root)
        report = build_report(run, self.store().events(run_id), self.root)
        if fmt == "md":
            return 200, to_markdown(report), "text/markdown; charset=utf-8"
        return {"ok": True, "report": report.model_dump(mode="json")}

    def evidence(self, _req, run_id: str) -> dict:
        """GET /runs/<id>/evidence: the exported evidence files and their links."""
        self._run(run_id)
        directory = run_dir(self.results_dir, run_id)
        files = [name for name in FILES if (directory / name).exists()]
        return {"ok": True, "run_id": run_id, "files": files,
                "links": {n: f"/api/v1/runs/{run_id}/evidence/{n}" for n in files}}

    def evidence_file(self, _req, run_id: str, name: str):
        """GET /runs/<id>/evidence/<name>: one allowlisted evidence file."""
        self._run(run_id)
        if name not in FILES:          # allowlist, so no path can escape the run directory
            raise ApiError("NOT_FOUND", "unknown evidence file")
        path = run_dir(self.results_dir, run_id) / name
        if not path.is_file():
            raise ApiError("NOT_FOUND", "evidence not exported yet")
        ctype = "application/json" if name.endswith(".json") else \
            "text/markdown; charset=utf-8" if name.endswith(".md") else "text/plain; charset=utf-8"
        return 200, path.read_text(), ctype

    # ---------------------------------------------------------- internals
    def _drive(self, run, run_id, plan, cap, params) -> None:
        """Background thread: drive a live run to its end, record controller errors, then export its
        evidence.
        """
        try:
            run.drive(run_id, plan, cap, params)
        except Exception as error:  # surfaced in the journal; restoration is the watchdog's job
            self.store().emit(run_id, "error", message=f"controller thread failed: {error}"[:300])
        finally:
            try:
                export_run(self.store(), run_id, self.results_dir, self.root)
            except Exception as error:
                self.log(f"evidence export failed for {run_id}: {error}")

    def _baseline_job(self) -> None:
        """Background thread: run the baseline capture and record success or failure on the job."""
        try:
            baseline, path = self.baseline_runner()
            self.baseline_job.update(state="succeeded", baseline_id=baseline.baseline_id,
                                     healthy=baseline.healthy, blocking=baseline.blocking[:5],
                                     file=str(path))
        except Exception as error:
            self.baseline_job.update(state="failed", error=str(error)[:300])

    def _job_view(self) -> Optional[dict]:
        """The current baseline job state, or None."""
        return dict(self.baseline_job) or None

    def _run(self, run_id: str) -> dict:
        """Load a run or raise UNKNOWN_RUN."""
        try:
            return self.store().get(run_id)
        except KeyError:
            raise ApiError("UNKNOWN_RUN", f"no run {run_id}")

    def _plan(self, data) -> Plan:
        """Validate a request body's plan against the Plan contract."""
        if not isinstance(data, dict):
            raise ApiError("INVALID_PLAN", "body must contain a plan object")
        try:
            return Plan.model_validate(data)
        except ValidationError as error:
            raise ApiError("INVALID_PLAN", "plan does not match the contract", problems=_problems(error))

    def _tool(self, name: str, args) -> dict:
        """Call one agent tool through the registry, turning its error results into ApiError."""
        out = ToolRegistry().dispatch(name, args if isinstance(args, dict) else {"_": args})
        if not out["ok"]:
            raise ApiError(out["error"], out.get("message", ""),
                           **{k: v for k, v in out.items() if k not in ("ok", "error", "message")})
        return out


def _default_provider():
    """The model provider for API diagnoses (Claude)."""
    from ..agent.providers import AnthropicProvider
    return AnthropicProvider()


def _problems(error: ValidationError) -> list[str]:
    """Readable 'field: message' list from a validation error."""
    return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()]


ID = r"(?P<run_id>[a-z0-9][a-z0-9-]{1,63})"
ROUTES = [
    ("GET", r"/api/v1/health", "health"),
    ("GET", r"/api/v1/capabilities", "capabilities"),
    ("GET", r"/api/v1/capabilities/(?P<cap_id>[a-z0-9-]{1,64})", "capability"),
    ("GET", r"/api/v1/preflight", "preflight"),
    ("POST", r"/api/v1/plans", "propose"),
    ("POST", r"/api/v1/plans/validate", "validate_plan"),
    ("GET", r"/api/v1/baselines", "list_baselines"),
    ("POST", r"/api/v1/baselines", "start_baseline"),
    ("GET", r"/api/v1/runs", "list_runs"),
    ("POST", r"/api/v1/runs", "create_run"),
    ("GET", rf"/api/v1/runs/{ID}", "get_run"),
    ("GET", rf"/api/v1/runs/{ID}/events", "events"),
    ("GET", rf"/api/v1/runs/{ID}/stream", "stream"),
    ("POST", rf"/api/v1/runs/{ID}/abort", "abort"),
    ("GET", rf"/api/v1/runs/{ID}/report", "report"),
    ("GET", rf"/api/v1/runs/{ID}/report\.md", "report_md"),
    ("GET", rf"/api/v1/runs/{ID}/evidence", "evidence"),
    ("POST", rf"/api/v1/runs/{ID}/diagnosis", "diagnose"),
    ("GET", rf"/api/v1/runs/{ID}/diagnosis", "get_diagnosis"),
    ("GET", rf"/api/v1/runs/{ID}/evidence/(?P<name>[a-z.]+)", "evidence_file"),
]
COMPILED = [(m, re.compile(f"^{p}$"), h) for m, p, h in ROUTES]


def make_handler(api: ControllerAPI, port: int):
    """Build the HTTP handler class for this API and port, with Host and Origin checks, security
    headers, routing, errors and event streaming.
    """
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"} | api.allowed_origins

    class Handler(BaseHTTPRequestHandler):
        """HTTP handler for the controller API (and the workbench bridge when attached)."""
        protocol_version = "HTTP/1.1"
        server_version = "faultline-controller"

        def log_message(self, fmt, *args):
            """Send access-log lines to the API's logger."""
            api.log(fmt % args)

        # ------------------------------------------------------- plumbing
        def _send(self, status: int, body, ctype: str = "application/json") -> None:
            """Send a JSON or text response with no-store caching, security headers and CORS for
            allowed origins.
            """
            data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            origin = self.headers.get("Origin")
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)

        def json(self):
            """Parse and cache the request body, which must be a JSON object."""
            if self._json is None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    self._json = json.loads(raw or b"{}")
                except ValueError:
                    raise ApiError("INVALID_JSON", "body is not valid JSON")
                if not isinstance(self._json, dict):
                    raise ApiError("INVALID_JSON", "body must be a JSON object")
            return self._json

        def _guard(self, mutating: bool) -> None:
            """Reject requests not addressed to the loopback controller; mutations also need an
            allowed origin, a JSON content type and a bounded body.
            """
            if self.headers.get("Host") not in hosts:
                raise ApiError("HOST_REJECTED", "requests must address the loopback controller")
            origin = self.headers.get("Origin")
            if mutating:
                if origin is not None and origin not in origins:
                    raise ApiError("ORIGIN_REJECTED", f"origin {origin} may not change controller state")
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    raise ApiError("UNSUPPORTED_MEDIA_TYPE", "mutations require application/json")
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0:
                    raise ApiError("INVALID_INPUT", "negative body length")
                if self.headers.get("Transfer-Encoding"):
                    raise ApiError("INVALID_INPUT", "transfer encoding is unsupported")
                if length > MAX_BODY:
                    raise ApiError("PAYLOAD_TOO_LARGE", f"bodies are limited to {MAX_BODY} bytes")

        def _dispatch(self, method: str) -> None:
            """Route a request to the workbench bridge or the matching API method and send the
            result or a structured error.
            """
            self._json = None
            url = urlparse(self.path)
            self.query = parse_qs(url.query)
            try:
                if hasattr(api, "workbench_bridge") and (url.path.startswith("/api/workbench") or url.path in api.workbench_bridge.assets):
                    self._guard(mutating=method == "POST")
                    return self._send(*api.workbench_bridge.dispatch(method, url.path, self))
                for m, pattern, name in COMPILED:
                    match = pattern.match(url.path)
                    if match and m == method:
                        self._guard(mutating=method == "POST")
                        if name == "stream":
                            return self._stream(**match.groupdict())
                        if name == "report_md":
                            out = api.report(self, fmt="md", **match.groupdict())
                        else:
                            out = getattr(api, name)(self, **match.groupdict())
                        if isinstance(out, tuple):
                            return self._send(*out)
                        return self._send(200, out)
                raise ApiError("NOT_FOUND", f"no route for {method} {url.path}")
            except ApiError as error:
                self._send(error.status, error.body())
            except Unsupported as error:
                self._send(422, {"ok": False, **error.as_dict()})
            except (ValueError, KeyError) as error:
                self._send(400, {"ok": False, "error": "INVALID_INPUT", "message": str(error)[:300]})

            except Exception as error:
                api.log(f"request failed: {type(error).__name__}")
                self._send(500, {"ok": False, "error": "INTERNAL_ERROR", "message": "Controller request failed; check the local log."})

        def do_GET(self):
            """Handle GET requests."""
            self._dispatch("GET")

        def do_POST(self):
            """Handle POST requests."""
            self._dispatch("POST")

        def do_OPTIONS(self):
            """Answer CORS preflight requests for allowed origins only."""
            origin = self.headers.get("Origin")
            if origin not in origins:
                return self._send(403, {"ok": False, "error": "ORIGIN_REJECTED", "message": "origin not allowed"})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # --------------------------------------------------- P3-03 stream
        def _stream(self, run_id: str) -> None:
            """Server-sent events; reconnect with Last-Event-ID (or ?after_seq) to resume.

            Closing the stream never affects the run: it only reads the store.
            """
            api._run(run_id)
            after = int(self.headers.get("Last-Event-ID") or self.query.get("after_seq", ["0"])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            store, last_beat, started = api.store(), time.monotonic(), time.monotonic()
            try:
                while time.monotonic() - started < 3600:
                    for event in store.events(run_id, after):
                        payload = event.model_dump_json()
                        self.wfile.write(f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n".encode())
                        after = event.seq
                    self.wfile.flush()
                    run = store.get(run_id)
                    finished = S(run["state"]) in TERMINAL or run["state"] == S.NEEDS_ATTENTION.value
                    if finished and run["last_seq"] <= after:
                        view = json.dumps(api.run_view(run), default=str)
                        self.wfile.write(f"event: end\ndata: {view}\n\n".encode())
                        self.wfile.flush()
                        return
                    if time.monotonic() - last_beat > 10:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_beat = time.monotonic()
                    time.sleep(api.stream_poll)
            except (BrokenPipeError, ConnectionResetError):
                return                       # client went away; the run continues untouched

    return Handler


def serve(api: ControllerAPI, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create (not start) the server. Refuses any non-loopback bind address."""
    if not _loopback(host):
        raise ValueError("the controller API binds to loopback only; remote access needs an auth design")
    server = ThreadingHTTPServer((host, port), make_handler(api, port))
    server.daemon_threads = True
    if port == 0:   # ephemeral port (tests): rebuild the handler with the real port
        server.RequestHandlerClass = make_handler(api, server.server_address[1])
    return server
