"""Loopback web server for the AI chaos-engineering dashboard.

  GET  /                        dashboard
  GET  /api/status              health, catalog, endpoints, LLM, saved runs, active faults/fixes
  GET  /api/topology[?run=id]   dependency graph (+ blast radius of a run)
  GET  /api/events              server-sent events for the active job
  GET  /api/runs/<id>           saved run (+ analysis, comparison)
  GET  /api/plan                latest AI plan
  GET  /api/campaigns/<id>      saved campaign
  GET  /api/report.md?campaign=<id>|run=<id>   findings report (Markdown download)
  POST /api/plan                ask the planner agent for ranked experiments
  POST /api/run                 run one experiment (+ analyst agent)
  POST /api/campaign            run several experiments, analyse each, rank weaknesses
  POST /api/analyse             re-run the analyst on a saved run
  POST /api/autofix             deploy a run's validated fix, re-run the experiment, compare
  POST /api/rollback            remove a deployed fix
  POST /api/abort               kill switch: stop and restore every active fault now
  POST /api/end-fault           end the current fault early (restored; the run still completes)
  GET  /api/shop-view           where and how a scenario shows in the shop tab
  GET  /api/network[?fresh=1]   sandbox network: subnet, container IPs, ports, traffic, active rules

Usage: python -m demo.server [--port 8099]
"""
from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import traceback
from urllib.parse import parse_qs

from controller.observer.endpoints import app_url

from . import agent, autofix, evidence, faults, network, report, topology
from .catalog import CONTAINER_TARGETS, ENDPOINTS, EXCLUDED_TARGETS, SCENARIOS, shop_view
from .experiment import RUNS_DIR, Experiment, install_restore_handlers, wait_until_healthy

STATIC = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[1]
PLAN_FILE = RUNS_DIR / "plan-latest.json"


def load_env():
    """Load API keys and model settings from the repository's .env file if present."""
    env = ROOT / ".env"
    if env.exists():
        from dotenv import load_dotenv
        load_dotenv(env)


class Session:
    """Single active job; events are replayable by sequence number."""

    def __init__(self):
        """Start with no events and no running job."""
        self.lock = threading.Condition()
        self.events: list[dict] = []
        self.busy = False
        self.job = None
        self.experiment: Experiment | None = None
        self.stop = threading.Event()

    def emit(self, kind: str, data: dict):
        """Append an event with the next sequence number and wake up all event-stream listeners."""
        with self.lock:
            self.events.append({"seq": len(self.events) + 1, "kind": kind, "at": time.time(), "data": data})
            self.lock.notify_all()

    def after(self, seq: int, timeout: float = 15) -> list[dict]:
        """Return the events after a sequence number, waiting up to the timeout for new ones."""
        with self.lock:
            if len(self.events) <= seq:
                self.lock.wait(timeout)
            return self.events[seq:]

    def start(self, name: str, target, *args) -> bool:
        """Start a background job unless one is already running (clears the event log); returns
        False when busy.
        """
        with self.lock:
            if self.busy:
                return False
            self.busy, self.job, self.events = True, name, []
            self.stop.clear()
            self.lock.notify_all()

        def runner():
            """Run the job, report any error to the dashboard, then mark the session idle and emit
            'done'.
            """
            try:
                target(*args)
            except Exception as error:
                traceback.print_exc()
                self.emit("log", {"level": "error", "message": f"{type(error).__name__}: {error}"})
            finally:
                self.experiment = None
                self.busy, self.job = False, None
                self.emit("done", {"job": name})
        threading.Thread(target=runner, daemon=True).start()
        return True


S = Session()


# ------------------------------------------------------------------ storage
def _load(name: str):
    """Load a JSON file from artifacts/demo-runs, or None."""
    path = RUNS_DIR / name
    return json.loads(path.read_text()) if path.exists() else None


def _save(name: str, doc):
    """Write a JSON document into artifacts/demo-runs."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / name).write_text(json.dumps(doc, default=str))


def saved_runs(limit: int = 40) -> list[dict]:
    """Summaries of the most recent injected runs (newest first) for the dashboard history."""
    rows = []
    for path in sorted(RUNS_DIR.glob("run-*.json"), reverse=True):
        if path.name.count(".") > 1:
            continue
        try:
            doc = json.loads(path.read_text())
        except ValueError:
            continue
        if "resilience" not in doc or not doc.get("injected"):
            continue  # unfinished, legacy, or stopped before any fault was injected
        rows.append({"run_id": doc["run_id"], "title": doc.get("title"), "scenario": doc.get("scenario"),
                     "target": doc.get("target"), "variant": doc.get("variant"), "status": doc.get("status"),
                     "category": doc.get("category"), "started_at": doc.get("started_at"),
                     "score": doc["resilience"]["score"], "grade": doc["resilience"]["grade"],
                     "label": doc.get("label"), "parent": doc.get("parent"),
                     "analysed": (RUNS_DIR / f"{doc['run_id']}.analysis.json").exists()})
        if len(rows) >= limit:
            break
    return rows


def saved_campaigns() -> list[dict]:
    """Summaries of the most recent campaigns."""
    out = []
    for path in sorted(RUNS_DIR.glob("campaign-*.json"), reverse=True)[:10]:
        doc = json.loads(path.read_text())
        out.append({"id": doc["id"], "started_at": doc.get("started_at"), "runs": doc.get("runs"),
                    "overall_score": (doc.get("report") or {}).get("overall_score")})
    return out


# --------------------------------------------------------------------- jobs
def job_plan():
    """Background job: run the planner agent on recent history and save the plan."""
    history = saved_runs(20)
    plan = agent.plan_experiments(S.emit, history)
    plan["created_at"] = datetime.now().isoformat()
    _save(PLAN_FILE.name, plan)


def _analyse(record: dict) -> dict | None:
    """Run the analyst agent on a run that injected a fault, save its analysis and fix patch; errors
    are reported to the dashboard instead of raised.
    """
    if not record.get("injected"):
        S.emit("agent", {"agent": "analyst", "kind": "error",
                         "text": f"Nothing to analyse: no fault was injected ({record.get('abort_reason')})."})
        return None
    try:
        result = agent.analyse(record, S.emit)
        _save(f"{record['run_id']}.analysis.json", result)
        if result.get("fix"):
            autofix.save_patch(record["run_id"], result["fix"])
        return result
    except Exception as error:
        traceback.print_exc()
        S.emit("agent", {"agent": "analyst", "kind": "error", "text": f"{type(error).__name__}: {error}"})
        return None


def _experiment(params: dict) -> dict:
    """Run one experiment as the session's current experiment (so the kill switch and End fault now
    can reach it).
    """
    exp = Experiment(params, S.emit)
    S.experiment = exp
    if S.stop.is_set():
        exp.stop.set()
    try:
        return exp.run()
    finally:
        S.experiment = None


def job_run(params: dict):
    """Background job: run one experiment, then analyse it unless analysis was turned off."""
    record = _experiment(params)
    if params.get("analyse", True):
        _analyse(record)


def job_campaign(experiments: list[dict]):
    """Background job: run several experiments with a health gate between them, analyse each, then
    produce the campaign report.
    """
    cid = datetime.now().strftime("campaign-%Y%m%d-%H%M%S")
    doc = {"id": cid, "started_at": datetime.now().isoformat(), "runs": [], "experiments": experiments}
    S.emit("campaign", {"id": cid, "experiments": experiments, "index": 0})
    runs, analyses = [], {}
    for i, exp in enumerate(experiments):
        if S.stop.is_set():
            break
        S.emit("campaign", {"id": cid, "experiments": experiments, "index": i})
        if i and not wait_until_healthy(S.emit):
            S.emit("log", {"level": "warn", "message": "Shop not healthy after 120 s; stopping the campaign safely."})
            break
        record = _experiment({**exp, "baseline_s": exp.get("baseline_s", 8), "recovery_s": exp.get("recovery_s", 8),
                              "proposed_by": "planner"})
        runs.append(record)
        doc["runs"].append(record["run_id"])
        _save(f"{cid}.json", doc)
        analysis = _analyse(record)
        if analysis:
            analyses[record["run_id"]] = analysis
    if runs:
        try:
            doc["report"] = agent.campaign_report(runs, analyses, S.emit)
        except Exception as error:
            traceback.print_exc()
            S.emit("agent", {"agent": "campaign", "kind": "error", "text": f"{type(error).__name__}: {error}"})
            scores = [r["resilience"]["score"] for r in runs if r.get("resilience")]
            doc["report"] = {"overall_score": round(sum(scores) / len(scores)) if scores else None, "report": None}
        _save(f"{cid}.json", doc)
        S.emit("campaign_done", doc)


def job_analyse(run_id: str):
    """Background job: re-run the analyst on a saved run."""
    record = _load(f"{run_id}.json")
    S.emit("evidence", record)
    _analyse(record)


def job_autofix(run_id: str):
    """Background job: deploy a run's validated fix, re-run the same experiment and save the
    before/after comparison.
    """
    before = _load(f"{run_id}.json")
    analysis = _load(f"{run_id}.analysis.json")
    fix = (analysis or {}).get("fix")
    if not fix:
        raise RuntimeError("this run has no proposed fix")
    S.emit("autofix", {"stage": "deploying", "file": fix["file"], "title": fix.get("title")})
    deployed = autofix.apply(run_id, fix)
    S.emit("autofix", {"stage": "deployed", **deployed})
    S.emit("log", {"level": "info", "message": f"Fix live in {deployed['service']}; re-running the same experiment..."})
    params = {**before["params"], "label": "after fix", "parent": run_id}
    after = _experiment(params)
    comparison = autofix.compare(before, after)
    comparison["fix"] = {"file": fix["file"], "title": fix.get("title"), "service": deployed["service"]}
    _save(f"{after['run_id']}.comparison.json", comparison)
    _save(f"{run_id}.comparison.json", comparison)
    S.emit("comparison", comparison)


# ------------------------------------------------------------------ handler
class Handler(BaseHTTPRequestHandler):
    """HTTP handler for the dashboard page, its JSON API and the server-sent event stream."""
    def log_message(self, fmt, *args):
        """Silence the default per-request logging."""
        pass

    def _send(self, status: int, data: bytes, ctype: str, extra: dict | None = None):
        """Send a response with the given status, body and content type (never cached)."""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, body):
        """Send a JSON response."""
        self._send(status, json.dumps(body, default=str).encode(), "application/json")

    def _body(self) -> dict:
        """Parse the JSON request body ({} when empty)."""
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        """Serve the dashboard files and the read endpoints (status, topology, shop view, plan,
        runs, campaigns, report download, event stream).
        """
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in parse_qs(query).items()}
        try:
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path in ("/app.js", "/style.css"):
                return self._send(200, (STATIC / path[1:]).read_bytes(),
                                  "text/javascript" if path.endswith(".js") else "text/css")
            if path == "/api/status":
                return self._json(200, {
                    "health": evidence.health(), "llm": agent.provider_info(), "busy": S.busy, "job": S.job,
                    "scenarios": SCENARIOS, "endpoints": ENDPOINTS, "targets": CONTAINER_TARGETS,
                    "excluded": EXCLUDED_TARGETS, "runs": saved_runs(), "campaigns": saved_campaigns(),
                    "active_faults": faults.active_faults(), "active_fixes": autofix.active(),
                    "shop_url": app_url().replace("127.0.0.1", "localhost")})
            if path == "/api/topology":
                graph = topology.graph()
                run = _load(f"{q['run']}.json") if q.get("run", "").startswith("run-") else None
                graph["blast"] = (run or {}).get("blast_radius")
                return self._json(200, graph)
            if path == "/api/shop-view":
                if q.get("scenario") not in SCENARIOS:
                    return self._json(404, {"error": "unknown scenario"})
                return self._json(200, shop_view(q["scenario"], q.get("target") or None))
            if path == "/api/network":
                return self._json(200, network.inventory(0 if q.get("fresh") else 120))
            if path == "/api/plan":
                return self._json(200, _load(PLAN_FILE.name) or {})
            if path.startswith("/api/runs/"):
                rid = path.rsplit("/", 1)[-1]
                run = _load(f"{rid}.json") if rid.startswith("run-") else None
                if not run:
                    return self._json(404, {"error": "unknown run"})
                return self._json(200, {"run": run, "analysis": _load(f"{rid}.analysis.json"),
                                        "comparison": _load(f"{rid}.comparison.json")})
            if path.startswith("/api/campaigns/"):
                cid = path.rsplit("/", 1)[-1]
                doc = _load(f"{cid}.json") if cid.startswith("campaign-") else None
                return self._json(200, doc) if doc else self._json(404, {"error": "unknown campaign"})
            if path == "/api/report.md":
                text, name = report.markdown(q.get("campaign"), q.get("run"))
                return self._send(200, text.encode(), "text/markdown; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/api/events":
                return self._events(int(q.get("after", 0)))
        except Exception as error:
            traceback.print_exc()
            return self._json(500, {"error": f"{type(error).__name__}: {error}"})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        """Handle the action endpoints (run, plan, campaign, analyse, autofix, end fault, abort,
        rollback) after Host and Origin checks.
        """
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):   # blocks DNS-rebinding pages
            return self._json(403, {"error": "unexpected Host header"})
        if self.headers.get("Origin") not in (None, f"http://{host}"):
            return self._json(403, {"error": "cross-origin request refused"})
        body = self._body()
        if self.path == "/api/end-fault":
            if S.experiment:
                S.experiment.end_fault.set()
                return self._json(200, {"ok": True})
            return self._json(409, {"error": "no experiment is running"})
        if self.path == "/api/abort":
            S.stop.set()
            if S.experiment:
                S.experiment.stop.set()
                # Let the experiment run its own restore first (it owns any live stall/traffic
                # loops), then sweep whatever is still journaled.
                deadline = time.monotonic() + 20
                while S.experiment and time.monotonic() < deadline:
                    time.sleep(0.25)
            results = faults.restore_all()
            return self._json(200, {"restored": results, "flags_clean": faults.flags_clean(),
                                    "active_faults": faults.active_faults()})
        if self.path == "/api/rollback":
            if S.busy or faults.active_faults():
                return self._json(409, {"error": "finish or abort the running experiment before rolling back"})
            try:
                return self._json(200, autofix.rollback(body["service"]))
            except Exception as error:
                return self._json(500, {"error": str(error)})
        jobs = {
            "/api/plan": ("plan", job_plan, ()),
            "/api/run": ("experiment", job_run, (body,)),
            "/api/campaign": ("campaign", job_campaign, (body.get("experiments") or [],)),
            "/api/analyse": ("analysis", job_analyse, (body.get("run_id", ""),)),
            "/api/autofix": ("autofix", job_autofix, (body.get("run_id", ""),)),
        }
        if self.path not in jobs:
            return self._json(404, {"error": "not found"})
        name, fn, args = jobs[self.path]
        if self.path in ("/api/analyse", "/api/autofix") and not (RUNS_DIR / f"{args[0]}.json").exists():
            return self._json(404, {"error": "unknown run"})
        if self.path == "/api/run" and body.get("scenario") not in SCENARIOS:
            return self._json(400, {"error": "unknown scenario"})
        if not S.start(name, fn, *args):
            return self._json(409, {"error": f"busy: {S.job} is running"})
        return self._json(202, {"ok": True, "job": name})

    def _events(self, after: int):
        """Stream session events to the browser as server-sent events, with keep-alives, until the
        client disconnects.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                batch = S.after(after)
                for event in batch:
                    self.wfile.write(f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n".encode())
                    after = event["seq"]
                if not batch:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    """Start the dashboard: load .env, take the single-instance lock, restore any leftover faults,
    install restore handlers and serve on 127.0.0.1.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    load_env()
    # One dashboard per sandbox: a second instance would "restore" the first one's live fault.
    import fcntl
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(RUNS_DIR / ".server.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another AI Chaos Lab dashboard is already running for this sandbox.")
    leftover = faults.restore_all()
    if leftover:
        print(f"Restored faults left by a previous session: {leftover}")
    install_restore_handlers()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    info = agent.provider_info()
    print(f"AI chaos lab on http://127.0.0.1:{args.port}  (LLM: {info['provider'] or 'NOT CONFIGURED'} "
          f"{info['model'] or ''})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
