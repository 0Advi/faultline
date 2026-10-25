"""Dedicated-sandbox integration checks (P3-04). Never run in hosted CI.

make test-sandbox                         read-only checks against the running sandbox
FAULTLINE_SANDBOX_LIVE=1 FAULTLINE_BASELINE_REF=artifacts/baselines/<id>.json make test-sandbox
                                          also runs real injection + stop paths

The live tests use the real gates: they only proceed when the sandbox is
approved, the capability is enabled, and the baseline/payment policy exist.
Keep the printed evidence directories as the recorded real-sandbox result.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.sandbox,
              pytest.mark.skipif(os.environ.get("FAULTLINE_SANDBOX_TESTS") != "1",
                                 reason="set FAULTLINE_SANDBOX_TESTS=1 to run against the dedicated sandbox")]
ROOT = Path(__file__).resolve().parents[2]
LIVE = os.environ.get("FAULTLINE_SANDBOX_LIVE") == "1"


def cli(*args) -> dict:
    """Run a controller CLI command and return its JSON output."""
    out = subprocess.run([sys.executable, "-m", "controller.cli", *args], cwd=ROOT,
                         capture_output=True, text=True, timeout=600)
    return json.loads(out.stdout)


def test_sandbox_identity_is_approved():
    """The running Docker sandbox passes the isolation preflight."""
    report = cli("preflight")
    assert report["isolation_approved"], report["errors"]


def test_flagd_serves_the_sandbox_owned_flags():
    """flagd serves the flags from the sandbox-owned flag file."""
    proc = subprocess.run([sys.executable, "scripts/verify_flags.py", "--runtime"], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_synthetic_checkout_is_traced_end_to_end():
    """A synthetic checkout succeeds and its trace is found end to end."""
    result = cli("probe")
    assert result["attempt"]["matched"], result["attempt"]
    assert result["observation"]["complete"], result["observation"]["missing"]


def _plan(capability: str, duration: int, **params):
    """A validated plan for a live test run."""
    from controller.agent.tools import ToolRegistry
    from controller.schemas import Plan
    out = ToolRegistry().dispatch("propose_plan", {"capability_id": capability, "parameters": params,
                                                   "duration_seconds": duration, "expected_impact": "sandbox check"})
    assert out["ok"], out
    plan = out["result"] | {"requested_by": "user", "baseline_ref": os.environ["FAULTLINE_BASELINE_REF"]}
    return Plan.model_validate(plan)


@pytest.mark.skipif(not LIVE, reason="set FAULTLINE_SANDBOX_LIVE=1 to inject real faults")
@pytest.mark.parametrize("capability, params", [("payment-unreachable", {}),
                                                ("payment-handler-failure", {"failure_rate": "100%"})])
def test_live_injection_restores_and_recovers(capability, params):
    """A live injection of each capability ends RECOVERED with the flag file restored byte for byte."""
    from controller.executor import runner
    from controller.executor.flag_store import FLAG_FILE
    before = FLAG_FILE.read_bytes()
    summary = runner.execute(_plan(capability, 15, **params), live=True)
    print("evidence:", summary["evidence"])
    assert summary["state"] == "RECOVERED", summary
    assert summary["result"]["impact_observed"] and FLAG_FILE.read_bytes() == before


@pytest.mark.skipif(not LIVE, reason="set FAULTLINE_SANDBOX_LIVE=1 to inject real faults")
def test_live_manual_abort_restores():
    """Aborting a live run while it is running still restores the flag and recovers."""
    from controller.executor import runner
    from controller.executor.flag_store import FLAG_FILE
    from controller.store import RunStore
    before = FLAG_FILE.read_bytes()

    def abort_when_running():
        """Wait until the run is RUNNING, then request an abort."""
        store = RunStore(runner.DB_PATH)
        for _ in range(600):
            active = [r for r in store.active_runs() if r["state"] == "RUNNING"]
            if active:
                time.sleep(3)
                store.request_abort(active[0]["run_id"])
                return
            time.sleep(0.2)
    threading.Thread(target=abort_when_running, daemon=True).start()
    summary = runner.execute(_plan("payment-unreachable", 30), live=True)
    print("evidence:", summary["evidence"])
    assert (summary["state"], summary["stop_reason"]) == ("ABORTED", "manual_abort")
    assert FLAG_FILE.read_bytes() == before
