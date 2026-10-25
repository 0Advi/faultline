"""P1-03 baseline measurement/verdict and P1-04 policy derivation (fake shop)."""
from datetime import datetime, timedelta, timezone
import json
import shutil
import time

import pytest
import yaml

from controller.baseline import check, policy
from controller.baseline.measure import Environment, load_criteria, measure
from controller.executor.flag_store import FLAG_FILE
from controller.observer.fixtures import SECRETS
from controller.observer.http import Shop
from controller.observer.sanitize import assert_clean
from controller.observer.telemetry import Telemetry
from controller.schemas import SandboxRef
from tests.fake_shop import FakeRuntime, FakeShop

SANDBOX = SandboxRef(docker_context="dedicated", daemon_id="DAEMON-1", compose_project="faultline-sandbox")


@pytest.fixture
def criteria():
    """Fixture: baseline criteria with shortened waits for tests."""
    c = load_criteria()
    c["workload"]["trace_wait_seconds"] = 2
    return c


@pytest.fixture
def fake():
    """Fixture: a running fake shop."""
    with FakeShop() as shop:
        yield shop


def make_env(fake, tmp_path, logs=lambda tid: {"hits": 1, "excerpts": []}, runtime=None):
    """A baseline environment pointing at the fake shop, with fake logs and runtime."""
    flag = tmp_path / "demo.flagd.json"
    if not flag.exists():
        shutil.copy(FLAG_FILE, flag)
    return Environment(shop=Shop(fake.url, 5),
                       telemetry=Telemetry(fake.url + "/jaeger/ui/api", fake.url, timeout=2, log_search=logs),
                       sandbox=SANDBOX, runtime=runtime or FakeRuntime(), upstream_revision="test",
                       flag_file=flag, out_dir=tmp_path / "baselines", log=lambda line: None)


def run(env, criteria, attempts=6):
    """Measure a short baseline."""
    return measure(env, criteria, attempts=attempts, duration_seconds=0.3, warmup=1)


def test_healthy_baseline_is_measured_and_saved(fake, tmp_path, criteria):
    """A healthy shop gives a healthy, saved baseline with stats per probe."""
    baseline, path = run(make_env(fake, tmp_path), criteria)
    assert baseline.healthy, baseline.blocking
    checkout = baseline.probe("POST /api/checkout")
    assert (checkout.attempts, checkout.failures) == (6, 0) and checkout.p95_ms is not None
    assert all(p.attempts == 6 for p in baseline.probes[1:])          # protected probes per attempt
    assert [c["matched"] for c in baseline.controls] == [True, True, True]
    assert baseline.telemetry["trace_coverage"] == 1.0
    assert len(baseline.workload["warmup_results"]) == 1               # warmup kept separate
    saved = check.load(path)
    assert saved == baseline
    lines = (tmp_path / "baselines" / f"{baseline.baseline_id}.observations.jsonl").read_text().splitlines()
    assert len(lines) == 6 + 3 + 6 * 4
    assert_clean(lines, SECRETS)


def test_failures_are_counted_and_block_execution(fake, tmp_path, criteria):
    """Failed requests are counted and make the baseline blocking."""
    fake.fail_every = 3
    baseline, path = run(make_env(fake, tmp_path), criteria)
    assert not baseline.healthy and path.exists()                       # published anyway
    checkout = baseline.probe("POST /api/checkout")
    assert checkout.failures >= 1 and checkout.attempts == 6
    assert any("valid checkout failure rate" in b for b in baseline.blocking)


def test_missing_telemetry_blocks_instead_of_passing(fake, tmp_path, criteria):
    """Missing traces, metrics or logs block the baseline instead of passing."""
    fake.drop_traces, fake.metrics_missing = True, True
    baseline, _ = run(make_env(fake, tmp_path, logs=lambda tid: None), criteria, attempts=2)
    assert any("trace coverage" in b for b in baseline.blocking)
    assert any("metric" in b and "not found" in b for b in baseline.blocking)
    assert baseline.telemetry["missing_counts"]["trace"] >= 2


def test_stale_metrics_and_restarts_block(fake, tmp_path, criteria):
    """Stale metrics and container restarts block the baseline."""
    fake.metric_age = 900
    baseline, _ = run(make_env(fake, tmp_path, runtime=FakeRuntime(restarts_after=1)), criteria, attempts=2)
    assert any("s old" in b for b in baseline.blocking)
    assert any("payment restarted" in b for b in baseline.blocking)


def test_leaked_card_data_is_never_written(fake, tmp_path, criteria):
    """Card data in log excerpts aborts the baseline before anything is written."""
    leaky = lambda tid: {"hits": 1, "excerpts": [{"body": "charged 4432-8015-6152-0454"}]}
    with pytest.raises(ValueError, match="sanitization failed"):
        run(make_env(fake, tmp_path, logs=leaky), criteria, attempts=1)
    assert not list((tmp_path / "baselines").glob("*.json"))


def test_baseline_usability_gates(fake, tmp_path, criteria):
    """A baseline is unusable when stale, for another revision or flag file, or tampered with."""
    env = make_env(fake, tmp_path)
    _, path = run(env, criteria, attempts=2)
    assert check.usable_blockers(path, "DAEMON-1", env.flag_file, 24) == []
    assert "different Docker daemon" in check.usable_blockers(path, "OTHER", env.flag_file, 24)[0]
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    assert "stale" in check.usable_blockers(path, "DAEMON-1", env.flag_file, 24, now=later)[0]
    doc = json.loads(env.flag_file.read_text())
    doc["flags"]["paymentUnreachable"]["defaultVariant"] = "on"
    env.flag_file.write_text(json.dumps(doc))
    assert any("flag configuration changed" in b for b in check.usable_blockers(path, "DAEMON-1", env.flag_file, 24))
    assert "not found" in check.usable_blockers(tmp_path / "nope.json", "DAEMON-1", env.flag_file, 24)[0]


def two_baselines(fake, tmp_path, criteria):
    """Measure two baselines for policy derivation."""
    env = make_env(fake, tmp_path)
    a, pa = run(env, criteria)
    time.sleep(1.1)                                                    # distinct baseline IDs
    b, pb = run(env, criteria)
    return [a, b], [pa, pb]


def test_policy_derived_from_reproducible_baselines(fake, tmp_path, criteria):
    """Policy gates are derived from two reproducible healthy baselines."""
    baselines, paths = two_baselines(fake, tmp_path, criteria)
    criteria["reproducibility"]["max_checkout_p95_ratio"] = 100        # loopback latencies are noisy
    derived = policy.derive(baselines, criteria, paths)
    assert derived.policy_version.startswith("payment-")
    for name, gate in derived.gates.items():
        assert gate.rule and gate.inputs["baselines"] == [b.baseline_id for b in baselines], name
    g = derived.gates
    assert g["checkout_probe_timeout_seconds"].value >= 0.5
    assert g["fault_hard_cap_seconds"].value == 30
    assert 15 <= g["recommended_fault_duration_seconds"].value <= 30
    assert derived.affected_probes == ["POST /api/checkout"]
    written = policy.write(derived, tmp_path / "payment_policy.yaml")
    assert policy.load(written) == derived
    assert "recommendation" not in yaml.safe_dump(derived.model_dump(mode="json"))


def test_policy_refuses_single_unhealthy_or_irreproducible_input(fake, tmp_path, criteria):
    """Policy derivation refuses one baseline, unhealthy ones or ones that disagree."""
    baselines, paths = two_baselines(fake, tmp_path, criteria)
    with pytest.raises(policy.PolicyError, match="at least two"):
        policy.derive(baselines[:1], criteria, paths[:1])
    criteria["reproducibility"]["max_checkout_p95_ratio"] = 1.0
    slow = baselines[1].model_copy(update={"probes": [
        baselines[1].probes[0].model_copy(update={"p95_ms": baselines[0].probes[0].p95_ms * 3 + 1}),
        *baselines[1].probes[1:]]})
    with pytest.raises(policy.PolicyError, match="p95 differs"):
        policy.derive([baselines[0], slow], criteria, paths)
    sick = baselines[1].model_copy(update={"healthy": False, "blocking": ["x"]})
    with pytest.raises(policy.PolicyError, match="not healthy"):
        policy.derive([baselines[0], sick], criteria, paths)


def test_future_or_under_sampled_baseline_cannot_enable_live_execution(fake, tmp_path, criteria):
    """Future-dated or under-sampled baselines cannot enable live runs."""
    env = make_env(fake, tmp_path)
    baseline, path = measure(env, criteria, attempts=2, duration_seconds=0, warmup=0)
    # The short capture is useful for diagnostics, but cannot authorize the live profile.
    reasons = check.usable_blockers(path, 'DAEMON-1', env.flag_file, 24, profile=criteria['workload'])
    assert any('approved profile' in r for r in reasons)
    future = baseline.model_copy(update={'finished_at': datetime.now(timezone.utc)+timedelta(hours=1)})
    path.write_text(future.model_dump_json())
    assert any('future' in r for r in check.usable_blockers(path, 'DAEMON-1', env.flag_file, 24))
