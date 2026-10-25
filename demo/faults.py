"""Reversible fault adapters with a crash-safe journal.

Each adapter records how to undo itself in artifacts/demo-runs/.active-faults.json
*before* changing anything, so `restore_all()` (called on finish, abort, exit,
signals and the next server start) can always put the sandbox back.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from controller.executor.flag_store import FLAG_FILE
from controller.observer.endpoints import app_url

from . import netem
from .catalog import CONTAINER_TARGETS, SCENARIOS

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "artifacts" / "demo-runs" / ".active-faults.json"
PROJECT = "faultline-sandbox"
NETWORK = "faultline-sandbox"
_lock = threading.Lock()
_LIVE: set = set()          # running StallCycle / TrafficSpike loops; restore_all halts them first


# ------------------------------------------------------------------ journal
def _journal_read() -> list[dict]:
    """Read the list of active faults (their undo records); an empty list if there is none."""
    try:
        text = JOURNAL.read_text()
    except OSError:
        return []
    try:
        return json.loads(text)
    except ValueError:
        # Never silently drop pending undos: keep the bad file for inspection and report it.
        JOURNAL.replace(JOURNAL.with_suffix(".corrupt"))
        print(f"WARNING: fault journal was corrupt; moved to {JOURNAL.with_suffix('.corrupt')}", flush=True)
        return []


def _journal_write(entries: list[dict]):
    """Atomic: write a temp file in the same directory, then rename over the journal."""
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    if not entries:
        JOURNAL.unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=JOURNAL.parent, prefix=".journal-", suffix=".json")
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(entries, indent=1))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, JOURNAL)


def _journal_add(entry: dict):
    """Append one undo record to the journal (thread-safe)."""
    with _lock:
        _journal_write(_journal_read() + [entry])


def _journal_remove(entry_id: str):
    """Remove one undo record from the journal by id (thread-safe)."""
    with _lock:
        _journal_write([e for e in _journal_read() if e["id"] != entry_id])


def active_faults() -> list[dict]:
    """The undo records of all faults currently applied."""
    return _journal_read()


# ------------------------------------------------------------------- docker
def docker(*args: str, timeout: float = 30, check: bool = True) -> str:
    """Run a docker CLI command and return its stdout; raises with the error text if it fails
    (unless check=False).
    """
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def container(service: str) -> str:
    """Return the container id of a sandbox service (exactly one expected)."""
    ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}",
                 "--filter", f"label=com.docker.compose.service={service}").split()
    if len(ids) != 1:
        raise RuntimeError(f"expected one {service} container, found {len(ids)}")
    return ids[0]


def container_state(service: str) -> dict:
    """Summarise a service container: status, running/paused, OOM kill, exit code, restarts, health,
    start time, memory limit and networks.
    """
    try:
        doc = json.loads(docker("inspect", container(service)))[0]
    except Exception as error:
        return {"service": service, "error": str(error)}
    state = doc["State"]
    return {"service": service, "status": state.get("Status"), "running": state.get("Running"),
            "paused": state.get("Paused"), "oom_killed": state.get("OOMKilled"),
            "exit_code": state.get("ExitCode"), "restart_count": doc.get("RestartCount"),
            "health": (state.get("Health") or {}).get("Status"),
            "started_at": state.get("StartedAt"),
            "memory_limit_mb": round((doc["HostConfig"].get("Memory") or 0) / 2**20) or None,
            "networks": list((doc["NetworkSettings"].get("Networks") or {}).keys())}


def _write_flags(text: str):
    """Overwrite the flag file in place. A rename would make flagd's watcher see the file
    vanish (on Docker Desktop bind mounts it then stops following updates)."""
    with open(FLAG_FILE, "r+", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def live_flags() -> dict | None:
    """Variants flagd is actually serving (not just what the file says), via the proxy route."""
    from urllib.request import Request, urlopen
    body = json.dumps({"context": {"product_id": "OLJCESPC7Z"}}).encode()
    try:
        req = Request(app_url() + "/flagservice/flagd.evaluation.v1.Service/ResolveAll", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            flags = json.loads(resp.read())["flags"]
        return {k: v.get("variant") for k, v in flags.items()}
    except Exception:
        return None


def _served(flag: str) -> str | None:
    """The variant flagd currently serves for one flag."""
    return (live_flags() or {}).get(flag)


def _wait_served(flag: str, variant: str, limit_s: float = 10) -> bool:
    """Poll flagd until it serves the given variant for the flag, or the time limit passes."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if _served(flag) == variant:
            return True
        time.sleep(0.5)
    return False


# ----------------------------------------------------------------- adapters
def _apply_flag(flag: str, variant: str) -> dict:
    """Turn a flagd fault on: journal the flag's prior definition, rewrite the flag file (targeting
    rules included) and verify flagd serves the new variant.
    """
    doc = json.loads(FLAG_FILE.read_text())
    entry = doc["flags"][flag]
    if variant not in entry["variants"]:
        raise ValueError(f"{flag} has no variant {variant}")
    prior = json.loads(json.dumps(entry))
    if entry.get("targeting"):
        # productCatalogFailure: the upstream rule returns "off" for its one product.
        rule = entry["targeting"]["if"]
        entry["targeting"] = {"if": [rule[0], variant, "off"]}
    else:
        entry["defaultVariant"] = variant
    entry["state"] = "ENABLED"
    undo = {"id": uuid4().hex, "kind": "flag", "flag": flag, "prior_entry": prior}
    _journal_add(undo)
    text = json.dumps(doc, indent=2) + "\n"
    _write_flags(text)
    if not _wait_served(flag, variant):
        _write_flags(text)
        if not _wait_served(flag, variant):
            raise RuntimeError(f"flagd did not start serving {flag}={variant}; not measuring")
    return undo


def _undo_flag(undo: dict) -> str:
    """Restore a flag's prior definition and verify flagd serves it; rewrites the file, then
    restarts flagd as a last resort if it does not pick the change up.
    """
    doc = json.loads(FLAG_FILE.read_text())
    doc["flags"][undo["flag"]] = undo["prior_entry"]
    text = json.dumps(doc, indent=2) + "\n"
    _write_flags(text)
    prior = undo["prior_entry"]["defaultVariant"]
    if _wait_served(undo["flag"], prior):
        return f"{undo['flag']} restored (verified in flagd)"
    _write_flags(text)
    if _wait_served(undo["flag"], prior):
        return f"{undo['flag']} restored (verified in flagd after rewrite)"
    # Self-heal: flagd lost its file watch; a restart reloads the restored file.
    docker("restart", container("flagd"), timeout=60)
    if _wait_served(undo["flag"], prior, 30):
        return f"{undo['flag']} restored (flagd restarted to reload)"
    raise RuntimeError(f"flagd still serves {undo['flag']}={_served(undo['flag'])}")


def _container_fault(kind: str, service: str, variant: str) -> dict:
    """Apply a container fault (kill, pause, network disconnect or CPU quota) to an allowed service,
    journaling how to undo it first.
    """
    if service not in CONTAINER_TARGETS:
        raise PermissionError(f"{service} is not an allowed fault target")
    cid = container(service)
    undo = {"id": uuid4().hex, "kind": kind, "service": service, "container": cid}
    if kind == "kill":
        _journal_add(undo)
        docker("kill", cid)
    elif kind == "pause":
        _journal_add(undo)
        docker("pause", cid)
    elif kind == "partition":
        info = json.loads(docker("inspect", cid))[0]["NetworkSettings"]["Networks"][NETWORK]
        undo["aliases"] = [a for a in (info.get("Aliases") or []) if a] or [service]
        undo["ip"] = info.get("IPAddress")
        _journal_add(undo)
        docker("network", "disconnect", NETWORK, cid)
    elif kind == "cpu":
        host = json.loads(docker("inspect", cid))[0]["HostConfig"]
        undo["prior_quota"] = host.get("CpuQuota") or -1
        undo["prior_period"] = host.get("CpuPeriod") or 100000
        _journal_add(undo)
        share = float(variant.rstrip("%")) / 100
        docker("update", "--cpu-period", "100000", "--cpu-quota", str(int(100000 * share)), cid)
    else:
        raise ValueError(kind)
    return undo


def _undo_container(undo: dict) -> str:
    """Undo a container fault; a container that was replaced in the meantime counts as already
    restored.
    """
    try:
        return _undo_container_inner(undo)
    except RuntimeError as error:
        if "No such container" in str(error):
            # The container was recreated (e.g. compose up): the new one carries none of this fault.
            return f"{undo['service']} was replaced; {undo['kind']} no longer applies"
        raise


def _undo_container_inner(undo: dict) -> str:
    """Undo one container fault: start, unpause, reconnect with the original aliases (reporting an
    IP change) or restore the CPU quota.
    """
    cid = undo["container"]
    if undo["kind"] == "kill":
        docker("start", cid, timeout=60)
    elif undo["kind"] == "pause":
        if json.loads(docker("inspect", cid))[0]["State"].get("Paused"):
            docker("unpause", cid)
    elif undo["kind"] == "partition":
        networks = json.loads(docker("inspect", cid))[0]["NetworkSettings"]["Networks"] or {}
        if NETWORK not in networks:
            args = ["network", "connect"]
            for alias in undo["aliases"]:
                args += ["--alias", alias]
            docker(*args, NETWORK, cid)
            ip = (json.loads(docker("inspect", cid))[0]["NetworkSettings"]["Networks"].get(NETWORK) or {}).get("IPAddress")
            if undo.get("ip") and ip != undo["ip"]:
                return (f"{undo['service']} reconnected with a NEW IP ({undo['ip']} -> {ip}); "
                        "callers may need a few seconds to reconnect")
    elif undo["kind"] == "cpu":
        quota = undo["prior_quota"] if undo["prior_quota"] > 0 else -1
        docker("update", "--cpu-period", str(undo["prior_period"]), "--cpu-quota", str(quota), cid)
    return f"{undo['service']} {undo['kind']} undone"


def container_ip(service: str) -> str:
    """A service container's IP address on the sandbox network."""
    networks = json.loads(docker("inspect", container(service)))[0]["NetworkSettings"]["Networks"] or {}
    ip = (networks.get(NETWORK) or {}).get("IPAddress")
    if not ip:
        raise RuntimeError(f"{service} has no address on {NETWORK}")
    return ip


def _apply_net(mechanism: str, target: str, variant: str) -> tuple[dict, str]:
    """Apply a network fault. Link faults (blackhole / reset) run in the target's caller and match
    only packets to the target; the others run in the target itself. The undo record is journaled
    before any rule is added."""
    where = netem.LINK_CALLER[target] if mechanism in ("blackhole", "reset") else target
    if where not in CONTAINER_TARGETS:
        raise PermissionError(f"{where} is not an allowed fault target")
    peer_ip = container_ip(target) if mechanism in ("blackhole", "reset") else None
    netem.ensure_image()
    undo = {"id": uuid4().hex, "kind": "net", "net": mechanism, "service": where, "container": container(where),
            "peer": target if peer_ip else None, "peer_ip": peer_ip}
    if mechanism in ("delay", "loss", "corrupt", "bandwidth"):
        undo["dev"] = netem.interface(undo["container"])
    else:
        undo["iptables"] = netem.iptables_rules(mechanism, peer_ip)
    _journal_add(undo)
    rule = netem.apply(undo, mechanism, variant, peer_ip)
    return undo, rule


DB_MARK = "chaos_demo_overload"


def _psql(cid: str, sql: str) -> str:
    """Run one SQL statement in the astronomy-db container as postgres and return the unaligned
    output.
    """
    return docker("exec", cid, "psql", "-U", "postgres", "-tAc", sql)


def _heavy_count(cid: str) -> int:
    """Number of running database sessions started by the DB-overload fault (identified by a marker
    comment).
    """
    out = _psql(cid, f"SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%{DB_MARK}%' "
                     "AND pid <> pg_backend_pid()")
    return int(out.strip() or 0)


def _apply_db_overload(variant: str) -> dict:
    """Saturate astronomy-db: cap it to one CPU (journaled, so it is restored) and run N CPU-bound
    loops in it, so the shop's catalog queries compete for that CPU. Each loop also has a
    statement timeout, so it ends on its own even if every restore path failed."""
    cid = container("astronomy-db")
    count = int(variant.split()[0])
    undo = {"id": uuid4().hex, "kind": "db_overload", "service": "astronomy-db", "container": cid}
    _journal_add(undo)
    # Pure CPU loop: no rows are materialised, so it cannot fill memory or disk.
    query = (f"/* {DB_MARK} */ DO $$ DECLARE x float8 := 0; BEGIN "
             "FOR i IN 1..2000000000 LOOP x := x + sqrt(i); END LOOP; END $$;")
    for _ in range(count):
        docker("exec", "-d", cid, "psql", "-U", "postgres", "-c", "SET statement_timeout = '90s'", "-c", query)
    deadline = time.monotonic() + 8
    while _heavy_count(cid) < count and time.monotonic() < deadline:
        time.sleep(0.5)
    if _heavy_count(cid) < count:
        raise RuntimeError(f"only {_heavy_count(cid)} of {count} heavy queries started")
    return undo


def _undo_db_overload(undo: dict) -> str:
    """Cancel, then terminate, until no heavy query is left (verified)."""
    cid = undo["container"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _heavy_count(cid) == 0:
            return "heavy queries stopped (verified)"
        _psql(cid, f"SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE query LIKE '%{DB_MARK}%' "
                   "AND pid <> pg_backend_pid()")
        time.sleep(1)
        _psql(cid, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%{DB_MARK}%' "
                   "AND pid <> pg_backend_pid()")
        time.sleep(0.5)
    raise RuntimeError("heavy queries still running")


LOCK_SQL = "FROM pg_stat_activity WHERE query LIKE 'LOCK TABLE catalog.products%' AND pid <> pg_backend_pid()"


def _release_catalog_lock() -> str:
    """product-catalog's lock-contention fault holds ACCESS EXCLUSIVE for a fixed 30 s even after the
    flag is off: end that transaction now. Then watch one full ticker period (10 s): a product-catalog
    whose flag client kept a stale 'on' value (seen after it was restarted) re-locks every 10 s, so
    restart it to force a fresh flag read, and verify the lock stays gone."""
    try:
        db = container("astronomy-db")
        released = int(_psql(db, f"SELECT count(pg_terminate_backend(pid)) {LOCK_SQL}").strip() or 0)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            time.sleep(2)
            if int(_psql(db, f"SELECT count(*) {LOCK_SQL}").strip() or 0):
                docker("restart", container("product-catalog"), timeout=90)
                _psql(db, f"SELECT count(pg_terminate_backend(pid)) {LOCK_SQL}")
                return (f"released {released + 1} catalog table lock(s); product-catalog kept re-locking "
                        "(stale flag value) and was restarted")
        return f"released {released} catalog table lock(s); verified no re-lock for 12 s"
    except Exception as error:
        return f"could not release catalog lock: {error}"


class StallCycle:
    """Server-side slowness for every client: freeze a container for `stall_s`, let it run for
    `run_s`, repeat. Any request arriving during a freeze waits for it, whichever page or client
    sent it. Journaled as a pause, so a crash leaves at worst a paused container that restore_all
    unpauses."""

    def __init__(self, service: str, stall_s: float, run_s: float = 0.5):
        """Prepare a pause/unpause cycle for an allowed service and its undo record."""
        if service not in CONTAINER_TARGETS:
            raise PermissionError(f"{service} is not an allowed fault target")
        self.service, self.stall_s, self.run_s = service, stall_s, run_s
        self.cid = container(service)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.undo = {"id": uuid4().hex, "kind": "pause", "service": service, "container": self.cid}

    def _loop(self):
        """Background loop: freeze the container for stall_s, let it run for run_s, repeat until
        stopped.
        """
        while not self.stop.is_set():
            docker("pause", self.cid, check=False)
            self.stop.wait(self.stall_s)
            docker("unpause", self.cid, check=False)
            self.stop.wait(self.run_s)

    def start(self) -> dict:
        """Journal the cycle (as a pause), register it as a live loop and start it."""
        _journal_add(self.undo)
        _LIVE.add(self)
        self.thread.start()
        return self.undo

    def halt(self):
        """Stop the cycle, wait for the thread and always unpause the container."""
        self.stop.set()
        self.thread.join(timeout=self.stall_s + 5)
        _LIVE.discard(self)
        docker("unpause", self.cid, check=False)   # never leave it frozen, even if the loop raced


class TrafficSpike:
    """Concurrent synthetic users hammering browse paths until stopped."""

    def __init__(self, users: int):
        """Prepare a spike of the given number of concurrent synthetic users."""
        from urllib.request import urlopen
        self.users, self.stop, self.sent, self.urlopen = users, threading.Event(), 0, urlopen
        self.threads: list[threading.Thread] = []

    def _loop(self):
        """One synthetic user: request home, product list, recommendations and ads in a closed loop
        until stopped.
        """
        paths = ["/", "/api/products?currencyCode=USD", "/api/recommendations?productIds=66VCHSJNUP&currencyCode=USD",
                 "/api/data?contextKeys=telescopes"]
        i = 0
        while not self.stop.is_set():
            try:
                with self.urlopen(app_url() + paths[i % len(paths)], timeout=10) as resp:
                    resp.read()
            except Exception:
                pass
            self.sent += 1
            i += 1

    def start(self):
        """Register the spike as a live loop and start one thread per user."""
        _LIVE.add(self)
        self.threads = [threading.Thread(target=self._loop, daemon=True) for _ in range(self.users)]
        for t in self.threads:
            t.start()

    def halt(self):
        """Stop all users and wait for their threads."""
        self.stop.set()
        for t in self.threads:
            t.join(timeout=12)
        _LIVE.discard(self)


# ------------------------------------------------------------------ facade
class Injection:
    """Apply one scenario (possibly composite); `restore()` undoes everything in reverse."""

    def __init__(self, scenario: str, target: str, variant: str):
        """Validate the scenario, target and intensity against the catalog before anything is
        applied.
        """
        spec = SCENARIOS[scenario]
        if variant not in spec["variants"]:
            raise ValueError(f"{scenario} has no intensity {variant}")
        if "targets" in spec and target not in spec["targets"]:
            raise PermissionError(f"{target} is not an allowed target for {scenario}")
        self.spec, self.scenario, self.target, self.variant = spec, scenario, target, variant
        self.undos: list[dict] = []
        self.spike: TrafficSpike | None = None
        self.stall: StallCycle | None = None
        self.description = ""
        self.rule = ""

    def apply(self):
        """Apply the fault. If anything fails half-way, undo every journal entry this apply created
        before re-raising, so a failed injection can never leave a partial fault behind."""
        before = {e["id"] for e in _journal_read()}
        try:
            self._apply()
        except BaseException:
            for loop in (self.stall, self.spike):
                if loop:
                    loop.halt()
            for entry in reversed([e for e in _journal_read() if e["id"] not in before]):
                undo_entry(entry)
            self.undos = []
            raise
        self.owned = {e["id"] for e in _journal_read()} - before

    def _apply(self):
        """Apply the scenario's mechanism(s) (flag, container fault, stall cycle, DB overload,
        traffic spike, or a combination), keeping each undo record and a readable description.
        """
        kind = self.spec["kind"]
        if kind == "flag":
            self.undos.append(_apply_flag(self.spec["flag"], self.variant))
            self.description = f"flagd: {self.spec['flag']} = {self.variant}"
            if self.spec.get("stall_target"):
                # The upstream flag only slows clients that send Envoy's delay header (the home-page
                # product cards); also slow the service itself so every page and client sees it.
                seconds = float(self.variant.rstrip("sec"))
                self.stall = StallCycle(self.spec["stall_target"], max(0.5, seconds - 0.5))
                self.undos.append(self.stall.start())
                self.description += f" + {self.spec['stall_target']} server-side stalls ({self.variant})"
        elif kind == "stall":
            seconds = float(self.variant.rstrip("s"))
            self.stall = StallCycle(self.target, seconds)
            self.undos.append(self.stall.start())
            self.description = f"{self.target} responds slowly (stalls up to {self.variant})"
        elif kind in ("kill", "pause", "partition", "cpu"):
            self.undos.append(_container_fault(kind, self.target, self.variant))
            verb = {"kill": "docker kill", "pause": "docker pause", "partition": "network disconnect",
                    "cpu": f"CPU quota {self.variant}"}[kind]
            self.description = f"{verb} {self.target}"
        elif kind == "net":
            undo, rule = _apply_net(self.spec["net"], self.target, self.variant)
            self.undos.append(undo)
            self.rule = rule
            where = undo["service"] + (f" -> {undo['peer']} ({undo['peer_ip']})" if undo.get("peer") else "")
            self.description = f"{where}: {rule}"
        elif kind == "db_overload":
            self.undos.append(_container_fault("cpu", "astronomy-db", "100%"))   # 1 CPU: queries must compete
            self.undos.append(_apply_db_overload(self.variant))
            self.description = f"{self.variant} heavy SQL on astronomy-db (capped to 1 CPU)"
        elif kind in ("traffic", "combined"):
            if kind == "combined":
                self.undos.append(_container_fault("kill", self.target, "kill"))
            self.spike = TrafficSpike(int(self.variant.split()[0]))
            self.spike.start()
            self.description = f"{self.variant} concurrent users" + \
                (f" + docker kill {self.target}" if kind == "combined" else "")
        else:
            raise ValueError(kind)

    def restore(self) -> list[str]:
        """Undo everything this injection applied, in reverse order, plus any post-restore clean-up
        the scenario needs; returns one message per step.
        """
        results = []
        if self.stall:
            self.stall.halt()
            results.append(f"{self.stall.service} stall cycle stopped")
        if self.spike:
            self.spike.halt()
            results.append(f"traffic stopped after {self.spike.sent} requests")
        for undo in reversed(self.undos):
            results.append(undo_entry(undo))
        # Belt and braces: anything this injection journaled that is somehow still recorded.
        for entry in reversed([e for e in _journal_read() if e["id"] in getattr(self, "owned", set())]):
            results.append(undo_entry(entry))
        self.undos = []
        if self.spec.get("post_restore") == "release_table_lock":
            results.append(_release_catalog_lock())
        elif self.spec.get("post_restore") == "restart_checkout":
            try:
                docker("restart", container("checkout"), timeout=90)
                results.append("checkout restarted (drops the connections leaked while payment was unreachable)")
            except Exception as error:
                results.append(f"could not restart checkout: {error}")
        return results


def undo_entry(undo: dict) -> str:
    """Undo one journaled fault and remove it from the journal; returns a message (a failed undo is
    reported and stays journaled).
    """
    try:
        if undo["kind"] == "flag":
            result = _undo_flag(undo)
        elif undo["kind"] == "db_overload":
            result = _undo_db_overload(undo)
        elif undo["kind"] == "net":
            result = netem.undo(undo)
        else:
            result = _undo_container(undo)
    except Exception as error:
        return f"FAILED to undo {undo['kind']}: {error}"
    _journal_remove(undo["id"])
    return result


def restore_all() -> list[str]:
    """Stop every live background loop, then undo every journaled fault (used on exit, signals, the
    kill switch and server start).
    """
    results = []
    for loop in list(_LIVE):            # stop re-pausing / traffic before undoing the journal
        loop.halt()
        results.append(f"stopped {type(loop).__name__}")
    return results + [undo_entry(e) for e in reversed(_journal_read())]


def flags_clean() -> bool:
    """Clean only if both the file and flagd's live state have every flag off."""
    flags = json.loads(FLAG_FILE.read_text())["flags"]
    for entry in flags.values():
        if entry["defaultVariant"] != "off":
            return False
        rule = (entry.get("targeting") or {}).get("if")
        if rule and any(v != "off" for v in rule[1:]):
            return False
    served = live_flags()
    return served is not None and all(v == "off" for v in served.values())


def wait_running(service: str, limit_s: float = 60) -> bool:
    """Wait until a service's container is running, unpaused and healthy (if it has a health check)."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        state = container_state(service)
        if state.get("running") and not state.get("paused") and state.get("health") in (None, "healthy"):
            return True
        time.sleep(1)
    return False
