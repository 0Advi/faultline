"""P0-04: typed capabilities and bounded agent tools."""
import json

import pytest

from controller.agent.tools import MAX_CALLS_PER_SESSION, ToolRegistry
from controller.catalog import Catalog, is_live, load_policy
from controller.executor import adapters, runner
from controller.schemas import Execution, Family, Plan


@pytest.fixture
def tools():
    """Fixture: a default (read-only) tool registry."""
    return ToolRegistry()


def propose(tools, cap, **params):
    """Propose a plan for a capability with parameters."""
    return tools.dispatch("propose_plan", {"capability_id": cap, "parameters": params,
                                           "duration_seconds": 15, "expected_impact": "checkout fails"})


def test_catalog_covers_the_three_requested_families():
    """Enabled capabilities cover payment unavailable, currency/amount mismatch and duplicate
    payment.
    """
    families = {c.family for c in Catalog().by_id.values() if c.execution != Execution.DISABLED}
    assert families == {Family.CURRENCY_AMOUNT_MISMATCH, Family.DUPLICATE_PAYMENT, Family.PAYMENT_UNAVAILABLE}


def test_policy_keeps_everything_offline_in_p0():
    """The committed execution policy keeps live execution off."""
    policy = load_policy()
    assert policy["execution_enabled"] is False
    assert not any(is_live(policy, cid) for cid in Catalog().by_id)


@pytest.mark.parametrize("cap, params", [
    ("payment-unreachable", {}),
    ("payment-handler-failure", {"failure_rate": "100%"}),
    ("checkout-currency-mismatch", {"currency_code": "XXX", "attempts": 2}),
    ("payment-amount-mismatch", {"mode": "negative_amount"}),
    ("checkout-duplicate-submit", {"concurrency": 3}),
    ("payment-charge-replay", {"repeat": 2}),
])
def test_supported_plans_validate_and_dry_run(tools, cap, params):
    """Supported capability/parameter combinations validate and dry-run."""
    out = propose(tools, cap, **params)
    assert out["ok"], out
    plan = Plan.model_validate(out["result"])
    dry = runner.execute(plan, live=False)
    assert dry["mode"] == "dry_run" and dry["actions"]


@pytest.mark.parametrize("cap, params, code", [
    ("kill-database", {}, "UNSUPPORTED_CAPABILITY"),
    ("payment-process-outage", {}, "CAPABILITY_DISABLED"),
    ("payment-delay-or-response-loss", {}, "CAPABILITY_DISABLED"),
    ("payment-handler-failure", {"failure_rate": "250%"}, "PARAMETER_OUT_OF_BOUNDS"),
    ("checkout-duplicate-submit", {"concurrency": 500}, "PARAMETER_OUT_OF_BOUNDS"),
    ("payment-unreachable", {"flag": "cartFailure"}, "PARAMETER_OUT_OF_BOUNDS"),
    ("payment-amount-mismatch", {"mode": "; rm -rf /"}, "PARAMETER_OUT_OF_BOUNDS"),
])
def test_unsupported_requests_get_structured_explanations(tools, cap, params, code):
    """Unsupported requests get a structured error code and explanation."""
    out = propose(tools, cap, **params)
    assert out == {**out, "ok": False, "error": code}
    assert out["message"]


def test_unknown_capability_suggests_alternatives(tools):
    """A misspelled capability suggests the closest names."""
    out = propose(tools, "payment-unreachble")
    assert "payment-unreachable" in out["closest"]


@pytest.mark.parametrize("args", [
    {"capability_id": "payment-unreachable", "expected_impact": "x", "command": "docker stop payment"},
    {"capability_id": "payment-unreachable", "expected_impact": "x", "target": "frontend"},
    {"capability_id": "payment-unreachable", "expected_impact": "x", "duration_seconds": 3600},
    {"capability_id": "payment-unreachable", "expected_impact": "x", "parameters": {"p": ["a"]}},
])
def test_model_cannot_smuggle_commands_targets_or_limits(tools, args):
    """Extra fields like commands, targets or limits in tool arguments are rejected."""
    out = tools.dispatch("propose_plan", args)
    assert out["ok"] is False and out["error"] == "INVALID_ARGUMENTS"


def test_adapter_config_hidden_from_model(tools):
    """Adapter configuration is never shown to the model."""
    out = tools.dispatch("describe_capability", {"capability_id": "payment-amount-mismatch"})
    assert out["ok"] and "adapter_config" not in out["result"]


def test_default_registry_cannot_execute_or_run_commands(tools):
    """The default registry cannot run plans live or execute commands."""
    plan = propose(tools, "payment-unreachable")["result"]
    assert tools.dispatch("run_plan", {"plan": plan, "live": True})["error"] == "MUTATION_NOT_PERMITTED"
    assert tools.dispatch("abort_run", {"run_id": "run-1"})["error"] == "MUTATION_NOT_PERMITTED"
    assert tools.dispatch("shell", {"cmd": "ls"})["error"] == "UNKNOWN_TOOL"


def test_budget_and_size_limits(tools):
    """Oversized arguments and too many calls per session are refused."""
    assert tools.dispatch("list_capabilities", "x" * 5000)["error"] == "ARGUMENTS_TOO_LARGE"
    for _ in range(MAX_CALLS_PER_SESSION):
        tools.dispatch("list_capabilities", {})
    assert tools.dispatch("list_capabilities", {})["error"] == "TOOL_BUDGET_EXCEEDED"


def test_tool_definitions_are_json_schema(tools):
    """Every tool definition is valid JSON with an object input schema."""
    for d in tools.definitions():
        json.dumps(d)
        assert d["input_schema"]["type"] == "object"


def test_live_run_blocked_without_gates(tools):
    """A live run is refused while gates are not satisfied."""
    plan = Plan.model_validate(propose(tools, "payment-unreachable")["result"])
    with pytest.raises(runner.Unsupported) as refusal:
        runner.execute(plan, live=True)
    assert refusal.value.code == "LIVE_EXECUTION_BLOCKED"


def test_grpc_argv_is_fixed_except_validated_payload():
    """The grpcurl command is fixed; only the validated payload varies."""
    cap = Catalog().get("payment-amount-mismatch")
    argv = adapters.grpc_argv(cap, {"mode": "unknown_currency"}, "faultline-sandbox", "run-1", 10)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--privileged" not in argv and "/var/run/docker.sock" not in " ".join(argv)
    assert json.loads(argv[argv.index("-d") + 1])["amount"]["currencyCode"] == "XXX"


def test_shop_refuses_non_loopback_url():
    """The shop client refuses non-loopback URLs."""
    with pytest.raises(PermissionError):
        adapters.Shop("http://payments.example.com", 5)


def test_configuration_alone_cannot_enable_live_execution(tools, monkeypatch, tmp_path):
    """Flipping execution_enabled/approved is not enough: baseline, payment policy and
    sandbox scope gates still block, before any run record or mutation exists."""
    import shutil
    from controller.executor.flag_store import FLAG_FILE
    from tests.fake_env import FakeEnvironment

    plan = Plan.model_validate(propose(tools, "payment-unreachable")["result"])
    monkeypatch.setattr(runner, "load_policy", lambda: {
        **load_policy(), "execution_enabled": True, "live_capabilities": [plan.capability_id]})
    monkeypatch.setattr(runner.sandbox, "load_spec", lambda: {**tools.spec, "approved": True})
    monkeypatch.setattr(runner.payment_policy, "POLICY_PATH", tmp_path / "missing.yaml")

    def forbidden(*args, **kwargs):
        """Fail the test if anything tries to create a run or mutate the app."""
        pytest.fail("live refusal must happen before a run record or application mutation")

    monkeypatch.setattr(runner, "RunStore", forbidden)
    monkeypatch.setattr(runner, "Runner", forbidden)
    shutil.copy(FLAG_FILE, tmp_path / "demo.flagd.json")
    with pytest.raises(runner.Unsupported) as refusal:
        runner.execute(plan, live=True, env=FakeEnvironment(tmp_path / "demo.flagd.json"))
    blockers = refusal.value.details["blockers"]
    assert refusal.value.code == "LIVE_EXECUTION_BLOCKED"
    assert any("baseline_ref" in b for b in blockers)
    assert any("payment_policy.yaml" in b for b in blockers)
