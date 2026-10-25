"""P2-01 durable runner and P2-02 flag adapters (fake sandbox, real flag file + SQLite)."""
from datetime import datetime, timedelta, timezone
import json
import shutil
import sqlite3

import pytest

from controller.catalog import Unsupported
from controller.executor import runner
from controller.executor.flag_store import FLAG_FILE, FlagConflict, FlagStore
from controller.schemas import Baseline, ProbeStats, RunState as S, SandboxRef, validate_event_sequence
from controller.store import RunStore
from tests.fake_env import FakeEnvironment, fast_limits, make_plan


@pytest.fixture
def flag_file(tmp_path):
    """Fixture: a temp copy of the committed flag file."""
    path = tmp_path / "flagd" / "demo.flagd.json"
    path.parent.mkdir()
    shutil.copy(FLAG_FILE, path)
    return path


@pytest.fixture
def store(tmp_path):
    """Fixture: a temp run store."""
    return RunStore(tmp_path / "runs.sqlite3")


def execute(plan, env, store, **limits):
    """Execute a live run with fast limits, open gates and no logging."""
    return runner.execute(plan, live=True, env=env, store=store, limits=fast_limits(**limits),
                          gate_check=lambda p, c: [], log=lambda line: None)


# ------------------------------------------------------------- P2-02 flags
def test_prepare_writes_nothing_and_undo_restores_exact_bytes(flag_file):
    """Preparing a change writes nothing, and undoing it restores the exact bytes."""
    store, original = FlagStore(flag_file), flag_file.read_bytes()
    change = store.prepare("paymentFailure", "100%")
    assert flag_file.read_bytes() == original and change.value == 1 and change.prior_value == 0
    store.apply(change)
    assert store.state_of(change) == "applied"
    assert store.undo(change) == "restored" and flag_file.read_bytes() == original
    assert store.undo(change) == "already_prior"                         # idempotent


def test_external_edits_are_conflicts_never_overwritten(flag_file):
    """External edits make apply and undo fail with a conflict instead of overwriting."""
    store = FlagStore(flag_file)
    change = store.prepare("paymentUnreachable", "on")
    store.apply(change)
    doc = json.loads(flag_file.read_text())
    doc["flags"]["cartFailure"]["defaultVariant"] = "10%"
    flag_file.write_text(json.dumps(doc))
    with pytest.raises(FlagConflict):
        store.undo(change)
    assert json.loads(flag_file.read_text())["flags"]["cartFailure"]["defaultVariant"] == "10%"
    with pytest.raises(FlagConflict):
        store.apply(change)


@pytest.mark.parametrize("flag, variant, error", [
    ("cartFailure", "100%", PermissionError), ("paymentFailure", "200%", ValueError),
    ("paymentUnreachable", "off", ValueError)])                       # already off: nothing to inject
def test_prepare_rejects_unsafe_changes(flag_file, flag, variant, error):
    """Unknown flags, disallowed variants and no-op changes are rejected."""
    with pytest.raises(error):
        FlagStore(flag_file).prepare(flag, variant)


# ------------------------------------------------------------- P2-01 runner
@pytest.mark.parametrize("cap, params", [("payment-unreachable", {}),
                                         ("payment-handler-failure", {"failure_rate": "100%"})])
def test_completed_run_injects_observes_restores_and_recovers(flag_file, store, cap, params):
    """A run injects the fault, observes impact, restores the flag and ends RECOVERED."""
    original = flag_file.read_bytes()
    result = execute(make_plan(cap, 1, **params), FakeEnvironment(flag_file), store)
    assert result["state"] == "RECOVERED" and result["stop_reason"] == "completed"
    assert flag_file.read_bytes() == original
    r = result["result"]
    assert r["conclusive"] and r["impact_observed"] and r["affected"]["failures"] == r["affected"]["samples"]
    assert r["protected"]["failures"] == 0
    events = store.events(result["run_id"])
    assert validate_event_sequence(events) == S.RECOVERED
    kinds = [(e.type, e.state.value) for e in events]
    armed = kinds.index(("state_changed", "ARMED"))
    assert armed < kinds.index(("action_applied", "RUNNING")) < kinds.index(("action_restored", "RESTORING"))
    assert events[armed].data["expected_applied_sha256"]


def test_recovery_journal_is_persisted_before_mutation(flag_file, store):
    """The recovery journal is stored before the flag file is changed."""
    result = execute(make_plan(), FakeEnvironment(flag_file), store)
    change = store.get(result["run_id"])["recovery"]["change"]
    assert change["flag"] == "paymentUnreachable" and change["prior_text"].encode() == FLAG_FILE.read_bytes()


def test_duplicate_request_rejected_without_mutation(flag_file, store):
    """Reusing a request id is rejected without touching the flag file."""
    plan = make_plan()
    first = execute(plan, FakeEnvironment(flag_file), store)
    before = flag_file.read_bytes()
    with pytest.raises(Unsupported) as refusal:
        execute(plan, FakeEnvironment(flag_file), store)
    assert refusal.value.code == "DUPLICATE_REQUEST" and refusal.value.details["run_id"] == first["run_id"]
    assert flag_file.read_bytes() == before and len(store.db.execute("SELECT * FROM runs").fetchall()) == 1


def test_concurrent_run_rejected_without_mutation(flag_file, store):
    """A second run while one is active is rejected without touching the flag file."""
    store.create("run-other-active", make_plan(), 999999)              # active, dead controller pid
    before = flag_file.read_bytes()
    with pytest.raises(Unsupported) as refusal:
        runner.Runner(store, FakeEnvironment(flag_file), fast_limits(), lambda l: None).run(
            make_plan(), *runner.validate(make_plan(), runner.Catalog(), runner.load_policy()))
    assert refusal.value.code == "RUN_IN_PROGRESS" and flag_file.read_bytes() == before


def test_restart_reconciles_orphaned_run_before_starting(flag_file, store):
    """An orphaned run from a dead controller is reconciled before a new run starts."""
    store.create("run-orphan", make_plan(), 999999)                    # DRAFT, controller gone
    result = execute(make_plan(), FakeEnvironment(flag_file), store)
    assert store.get("run-orphan")["state"] == "ABORTED"
    assert result["state"] == "RECOVERED"


@pytest.mark.parametrize("cap, params, duration, code", [
    ("payment-process-outage", {}, 5, "CAPABILITY_DISABLED"),          # invalid/disabled target
    ("payment-handler-failure", {"failure_rate": "33%"}, 5, "PARAMETER_OUT_OF_BOUNDS"),
])
def test_invalid_requests_rejected_before_any_state(flag_file, store, cap, params, duration, code):
    """Invalid capabilities, parameters or durations are rejected before any state is created."""
    plan = make_plan("payment-handler-failure", 5).model_copy(
        update={"capability_id": cap, "parameters": params, "duration_seconds": duration})
    with pytest.raises(Unsupported) as refusal:
        execute(plan, FakeEnvironment(flag_file), store)
    assert refusal.value.code == code and store.active_runs() == []


def test_duration_above_hard_cap_blocked(flag_file, store):
    """A duration above the hard cap is refused."""
    with pytest.raises(Unsupported) as refusal:
        execute(make_plan(duration=10), FakeEnvironment(flag_file), store, fault_hard_cap_s=5)
    assert any("hard cap" in b for b in refusal.value.details["blockers"])


def _baseline_file(root, finished_at, flag_file):
    """Write a baseline file finished at a given time and return its artifacts/ reference."""
    import hashlib
    stats = [ProbeStats(probe=p, attempts=30, failures=0, failure_rate=0, p95_ms=100.0, p99_ms=120.0)
             for p in ("POST /api/checkout", "GET /", "GET /api/products", "POST /api/cart", "GET /api/cart")]
    b = Baseline(baseline_id="baseline-test", started_at=finished_at, finished_at=finished_at,
                 sandbox=SandboxRef(docker_context="default", daemon_id="", compose_project="faultline-sandbox"),
                 upstream_revision="dedc0178918e260823323b8d95005a8cb924b007", flag_sha256=hashlib.sha256(flag_file.read_bytes()).hexdigest(),
                 workload={"attempts":30,"duration_seconds":180,"warmup_attempts":5}, probes=stats, controls=[], telemetry={}, resources={}, health={}, criteria={},
                 healthy=True, blocking=[], observations_file="x", limitations=[])
    path = root / "artifacts" / "baselines" / "baseline-test.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(b.model_dump_json())
    return "artifacts/baselines/baseline-test.json"


def _baseline_blockers(tmp_path, flag_file, age):
    """Gate blockers for a plan whose baseline is a given age."""
    ref = _baseline_file(tmp_path, datetime.now(timezone.utc) - age, flag_file)
    plan = make_plan().model_copy(update={"baseline_ref": ref})
    return runner.live_gates(plan, runner.Catalog().get(plan.capability_id), runner.load_policy(),
                             runner.sandbox.load_spec(), FakeEnvironment(flag_file), root=tmp_path)


def test_stale_baseline_blocks_live_run(flag_file, tmp_path):
    """A stale baseline blocks a live run; a fresh one does not."""
    assert any("baseline" in b and "stale" in b for b in _baseline_blockers(tmp_path, flag_file, timedelta(days=3)))
    assert not any(b.startswith("baseline:") for b in _baseline_blockers(tmp_path, flag_file, timedelta(0)))


def test_unhealthy_app_before_injection_never_arms(flag_file, store):
    """If the shop is unhealthy before injection the run never arms or changes the flag."""
    before = flag_file.read_bytes()
    result = execute(make_plan(), FakeEnvironment(flag_file, unhealthy_before=True), store)
    assert (result["state"], result["stop_reason"]) == ("ABORTED", "precondition_failed")
    assert "ARMED" not in [e.state.value for e in store.events(result["run_id"])]
    assert flag_file.read_bytes() == before


def test_flag_that_never_takes_effect_fails_and_restores(flag_file, store):
    """A flag change that flagd never serves fails the run and restores the file."""
    original = flag_file.read_bytes()
    result = execute(make_plan(), FakeEnvironment(flag_file, propagate=False), store)
    assert (result["state"], result["stop_reason"]) == ("FAILED", "adapter_error")
    assert flag_file.read_bytes() == original


def test_fixture_capability_runs_under_the_same_lifecycle(flag_file, store):
    """Fixture-based capabilities go through the same lifecycle and recover."""
    result = execute(make_plan("checkout-duplicate-submit", 0, concurrency=2), FakeEnvironment(flag_file), store)
    assert result["state"] == "RECOVERED" and result["result"]["fixture_results"] == 1
    assert any(e.data.get("fixture") == "checkout-duplicate-submit" for e in store.events(result["run_id"]))


def test_event_journal_is_append_only(flag_file, store):
    """Events cannot be updated or deleted in the database."""
    run_id = execute(make_plan(), FakeEnvironment(flag_file), store)["run_id"]
    for sql in ("UPDATE events SET body = '{}' WHERE run_id = ?", "DELETE FROM events WHERE run_id = ?"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.db.execute(sql, (run_id,))


def test_dry_run_never_touches_store_or_flags(flag_file):
    """A dry run only describes actions and touches nothing."""
    out = runner.execute(make_plan(), live=False)
    assert out["mode"] == "dry_run" and out["actions"]
