"""Regression coverage for host-specific readiness and delayed trace assembly."""
from pathlib import Path
from controller import catalog
from controller.safety import sandbox
from controller.observer.observer import wait_for_trace


def test_operator_overrides_do_not_replace_explicit_paths(tmp_path, monkeypatch):
    """Local operator override files never replace paths passed explicitly."""
    local = tmp_path / "local.yaml"
    local.write_text("approved: true\nexecution_enabled: true\n")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("approved: false\nexecution_enabled: false\n")
    monkeypatch.setenv("FAULTLINE_SANDBOX_SPEC", str(local))
    monkeypatch.setenv("FAULTLINE_EXECUTION_POLICY", str(local))
    assert sandbox.load_spec()["approved"]
    assert catalog.load_policy()["execution_enabled"]
    assert not sandbox.load_spec(explicit)["approved"]
    assert not catalog.load_policy(explicit)["execution_enabled"]


def test_desktop_alias_preserves_root_and_symlink_checks(tmp_path, monkeypatch):
    """The macOS /host_mnt path alias still enforces allowed roots and symlink checks."""
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "probe"
    inside.write_text("ok")
    outside = tmp_path / "outside"
    outside.write_text("no")
    assert sandbox._under("/host_mnt" + str(inside), [allowed])
    assert not sandbox._under("/host_mnt" + str(outside), [allowed])
    escape = allowed / "escape"
    escape.symlink_to(outside)
    assert not sandbox._under("/host_mnt" + str(escape), [allowed])
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert not sandbox._under("/host_mnt" + str(inside), [allowed])


def test_waits_for_trace_assembly_and_keeps_partial_at_deadline():
    """The observer waits for the full trace and keeps the partial one if the deadline passes."""
    partial = {"services": ["checkout"], "charge_spans": []}
    full = {"services": ["frontend", "checkout", "payment"], "charge_spans": [{}]}
    class DelayedTrace:
        """Fake telemetry whose trace is partial on the first call and complete afterwards."""
        calls = 0
        def trace(self, _):
            """Return the partial trace first, then the full one."""
            self.calls += 1
            return partial if self.calls == 1 else full
    client = DelayedTrace()
    assert wait_for_trace(client, "trace", 1, poll_seconds=0)[0] == full
    assert client.calls == 2
    assert wait_for_trace(DelayedTrace(), "trace", 0)[0] == partial
