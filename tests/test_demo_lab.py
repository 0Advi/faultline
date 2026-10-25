"""AI Chaos Lab (demo/): catalog, fault journal/restore, blast radius, scoring and fix guard.

No Docker, network or LLM is needed: flagd verification and docker calls are stubbed.
"""
import json
from pathlib import Path

import pytest

from demo import catalog, faults, fixcheck, scoring, topology

UPSTREAM = Path(__file__).resolve().parents[1] / "vendor" / "opentelemetry-demo" / "src"


# --------------------------------------------------------------- catalog
def test_every_scenario_is_well_formed():
    """Every lab scenario has valid variants, targets, kind and expected endpoints."""
    for sid, spec in catalog.SCENARIOS.items():
        assert spec["default"] in spec["variants"], sid
        assert set(spec.get("targets", [])) <= set(catalog.CONTAINER_TARGETS), sid
        assert spec["target"] not in catalog.EXCLUDED_TARGETS, sid
        if "stall_target" in spec:
            assert spec["stall_target"] in catalog.CONTAINER_TARGETS, sid
        assert spec["kind"] in {"flag", "kill", "pause", "partition", "cpu", "stall", "db_overload",
                                "traffic", "combined", "net"}, sid
        if spec["kind"] == "net":
            assert spec["net"] in {"delay", "loss", "bandwidth", "corrupt", "blackhole", "reset", "dns"}, sid


def test_observability_and_control_plane_are_never_targets():
    """The proxy, collector, Prometheus, Jaeger and flagd can never be fault targets."""
    for name in ("frontend-proxy", "otel-collector", "prometheus", "jaeger", "flagd"):
        assert name not in catalog.CONTAINER_TARGETS


def test_every_endpoint_service_is_on_the_graph():
    """Every service an endpoint uses appears on the dependency graph."""
    for ep in catalog.ENDPOINTS.values():
        assert set(ep["services"]) <= set(catalog.LAYOUT), ep


# ---------------------------------------------------- fault journal / restore
@pytest.fixture
def sandbox_flags(tmp_path, monkeypatch):
    """Fixture: a temp copy of the flag file that the lab writes to instead of the real one."""
    src = Path(__file__).resolve().parents[1] / "deploy" / "flagd" / "demo.flagd.json"
    flag_file = tmp_path / "demo.flagd.json"
    flag_file.write_text(src.read_text())
    monkeypatch.setattr(faults, "FLAG_FILE", flag_file)
    monkeypatch.setattr(faults, "JOURNAL", tmp_path / ".active-faults.json")
    monkeypatch.setattr(faults, "_wait_served", lambda *a, **k: True)   # flagd "serves" instantly
    return flag_file


def test_flag_fault_is_journaled_before_and_fully_restored(sandbox_flags):
    """A flag fault is journaled before it is applied and restored exactly."""
    before = json.loads(sandbox_flags.read_text())
    injection = faults.Injection("payment-failure", "payment", "50%")
    injection.apply()
    applied = json.loads(sandbox_flags.read_text())
    assert applied["flags"]["paymentFailure"]["defaultVariant"] == "50%"
    assert [e["flag"] for e in faults.active_faults()] == ["paymentFailure"]
    injection.restore()
    assert json.loads(sandbox_flags.read_text()) == before
    assert faults.active_faults() == []


def test_restore_all_recovers_a_fault_left_by_a_crashed_run(sandbox_flags):
    """restore_all undoes a fault left behind by a crashed run."""
    before = json.loads(sandbox_flags.read_text())
    faults.Injection("cart-failure", "cart", "100%").apply()   # "crash": never restored by the run
    assert faults.active_faults()
    faults.restore_all()                                       # what the server does on start/exit
    assert json.loads(sandbox_flags.read_text()) == before
    assert faults.active_faults() == []


def test_targeted_flag_is_enabled_only_for_its_product(sandbox_flags):
    """A product-targeted flag is enabled only for that product."""
    injection = faults.Injection("product-catalog-failure", "product-catalog", "on")
    injection.apply()
    rule = json.loads(sandbox_flags.read_text())["flags"]["productCatalogFailure"]["targeting"]["if"]
    assert rule[1] == "on" and rule[2] == "off"
    injection.restore()


def test_disallowed_targets_and_variants_are_refused():
    """Disallowed targets and variants raise PermissionError."""
    with pytest.raises(PermissionError):
        faults.Injection("service-crash", "prometheus", "kill")
    with pytest.raises(ValueError):
        faults.Injection("payment-failure", "payment", "33%")


# ------------------------------------------------------------ blast radius
def stats(success=100.0, p50=50.0, p95=80.0, n=10):
    """Endpoint statistics for evidence tests."""
    return {"n": n, "ok": round(n * success / 100), "success": success, "p50": p50, "p95": p95, "errors": {}}


def test_error_drop_and_median_slowdown_count_as_impact():
    """A success drop or a large median slowdown counts as impact."""
    base = {"checkout": stats(), "catalog": stats(), "home": stats()}
    fault = {"checkout": stats(success=50), "catalog": stats(p50=900, p95=1500), "home": stats()}
    impacted = topology.impacted_endpoints(base, fault)
    assert set(impacted) == {"checkout", "catalog"}


def test_a_short_host_stall_does_not_count_as_impact():
    """A p95 spike with an unchanged median does not count as impact."""
    # p95 spikes from one stalled request, but the median is unchanged
    base = {"home": stats(p50=40, p95=90)}
    fault = {"home": stats(p50=45, p95=1600)}
    assert topology.impacted_endpoints(base, fault) == {}


def test_blast_radius_flags_unexpected_spread():
    """Impact on endpoints that do not depend on the target is flagged as unexpected spread."""
    edges = [{"from": "frontend", "to": "checkout"}, {"from": "checkout", "to": "payment"}]
    base = {k: stats() for k in catalog.ENDPOINTS}
    fault = dict(base, checkout=stats(success=40), home=stats(success=40))
    blast = topology.blast_radius("payment", edges, base, fault, {})
    assert blast["expected_endpoints"] == ["checkout"]
    assert blast["unexpected_endpoints"] == ["home"] and not blast["contained"]
    assert topology.upstream_of("payment", edges) == {"checkout", "frontend"}


# ----------------------------------------------------------------- scoring
def req(endpoint, phase, ok=True, status=200, latency=50.0, timeout=False):
    """One request record for scoring tests."""
    return {"endpoint": endpoint, "phase": phase, "ok": ok, "status": status, "latency_ms": latency,
            "timeout": timeout}


def blast_for(contained=True):
    """A blast-radius result, contained or with unexpected spread."""
    return {"expected_endpoints": ["checkout"], "impacted_endpoints": {"checkout": ["x"]},
            "healthy_endpoints": [e for e in catalog.ENDPOINTS if e != "checkout"],
            "unexpected_endpoints": [] if contained else ["home"]}


def test_perfect_resilience_scores_100():
    """No failures, contained blast and fast recovery score 100."""
    records = [req(e, p) for p in ("baseline", "fault") for e in catalog.ENDPOINTS]
    result = scoring.score(records, blast_for(), time_to_recover_s=1.0, aborted=False)
    assert result["score"] == 100 and result["grade"] == "A"


def test_timeouts_and_slow_recovery_are_penalised():
    """Timeouts and slow recovery lower the score."""
    records = [req(e, "baseline") for e in catalog.ENDPOINTS]
    records += [req(e, "fault") for e in catalog.ENDPOINTS if e != "checkout"]
    records += [req("checkout", "fault", ok=False, status=None, latency=8000, timeout=True)]
    result = scoring.score(records, blast_for(), time_to_recover_s=52, aborted=False)
    assert result["parts"]["error_handling"] == 0
    assert result["parts"]["recovery"] < 20
    assert result["parts"]["availability"] == 90          # 9 of 10 paths fully available
    assert result["grade"] in ("C", "D")


def test_misleading_4xx_is_not_honest_error_handling():
    """A 4xx for a server fault is not counted as honest error handling."""
    records = [req(e, "baseline") for e in catalog.ENDPOINTS]
    records += [req("checkout", "fault", ok=False, status=422, latency=40)]
    assert scoring.score(records, blast_for(), 2, False)["facts"]["misleading_4xx"] == 1
    assert scoring.score(records, blast_for(), 2, False)["parts"]["error_handling"] == 0


def test_guardrail_abort_caps_containment():
    """An aborted run caps containment at 40."""
    records = [req(e, p) for p in ("baseline", "fault") for e in catalog.ENDPOINTS]
    assert scoring.score(records, blast_for(), 2, aborted=True)["parts"]["containment"] <= 40


# --------------------------------------------------------------- fix guard
def test_fix_that_comments_out_the_fault_is_detected():
    """A fix that comments out the fault code is detected."""
    original = "a()\nif (flags.PaymentUnreachable.Value(ctx)) {\n  bad()\n}\nb()\n"
    patched = "a()\n// if (flags.PaymentUnreachable.Value(ctx)) {\n  bad()\n}\nb()\n"
    assert fixcheck.removes_fault_injection(original, patched)


def test_resilience_fix_that_keeps_the_fault_passes():
    """A fix that keeps the fault code and improves handling passes."""
    original = 'x = client.getNumberValue("paymentFailure", 0)\nthrow new Error("boom")\n'
    patched = 'x = client.getNumberValue("paymentFailure", 0)\nconst e = new Error("retry later"); e.code = 14; throw e\n'
    assert fixcheck.removes_fault_injection(original, patched) == []


@pytest.mark.skipif(not (UPSTREAM / "ad").is_dir(), reason="upstream submodule not checked out")
def test_validate_fix_rejects_removing_the_ad_cpu_fault():
    """Removing the ad service's CPU fault code is rejected."""
    line = next(l for l in (UPSTREAM / "ad/src/main/java/oteldemo/AdService.java").read_text().splitlines()
                if "AD_HIGH_CPU_FEATURE_FLAG" in l and "execute" in l)
    result = fixcheck.validate_fix({"file": "ad/src/main/java/oteldemo/AdService.java", "find": line,
                                    "replace": "      // CPU load removed"})
    assert not result["ok"] and "injected fault" in result["error"]


@pytest.mark.skipif(not (UPSTREAM / "payment").is_dir(), reason="upstream submodule not checked out")
def test_validate_fix_rejects_ambiguous_or_foreign_edits():
    """Ambiguous find text and paths outside the sources are rejected."""
    assert not fixcheck.validate_fix({"file": "payment/charge.js", "find": "const", "replace": "let"})["ok"]
    assert not fixcheck.validate_fix({"file": "../../etc/passwd", "find": "x", "replace": "y"})["ok"]
