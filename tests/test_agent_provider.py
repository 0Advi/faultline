"""P4-01: provider adapter request shape, failure mapping and isolation (no network)."""
import ast
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from controller.agent.providers import (AnthropicProvider, FakeProvider, ModelRequest, ProviderError,
                                        FALLBACK_BETA)

ROOT = Path(__file__).resolve().parents[1]


def message(text='{"a": 1}', stop="end_turn", model="claude-opus-5-5", blocks=None):
    """A fake Anthropic SDK message with text or blocks, stop reason, model and usage."""
    return SimpleNamespace(
        content=blocks or [SimpleNamespace(type="text", text=text)], stop_reason=stop, model=model,
        stop_details=None, _request_id="req_123",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=1000,
                              cache_creation_input_tokens=0))


class FakeClient:
    """Fake Anthropic client that records request parameters and returns a fixed result or raises."""
    def __init__(self, result):
        """Remember the result and expose beta.messages.create."""
        self.calls, self.result = [], result
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **params):
        """Record the call and return (or raise) the configured result."""
        self.calls.append(params)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def provider(result, **kw):
    """An AnthropicProvider wired to a fake client."""
    client = FakeClient(result)
    return AnthropicProvider(client=client, **kw), client


def test_request_shape_pins_model_effort_schema_fallback_and_cache(monkeypatch):
    """Requests pin the model, effort, output schema, fallback models and prompt caching."""
    monkeypatch.delenv("FAULTLINE_AGENT_MODEL", raising=False)
    p, client = provider(message())
    response = p.complete(ModelRequest(system="sys", messages=[{"role": "user", "content": "x"}],
                                       output_schema={"type": "object"}))
    params = client.calls[0]
    assert params["model"] == "claude-opus-5-5"
    assert params["betas"] == [FALLBACK_BETA] and params["fallbacks"] == "default"
    assert params["output_config"] == {"effort": "high", "format": {"type": "json_schema",
                                                                   "schema": {"type": "object"}}}
    assert params["cache_control"] == {"type": "ephemeral"} and "thinking" not in params
    assert "tool_choice" not in params                                   # forced tool use is a 400 here
    assert response.json() == {"a": 1} and response.request_id == "req_123"
    assert response.usage["cache_read_input_tokens"] == 1000 and response.model_served == "claude-opus-5-5"


def test_model_id_comes_from_environment_not_tracked_files(monkeypatch):
    """Model id and effort come from environment variables."""
    monkeypatch.setenv("FAULTLINE_AGENT_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("FAULTLINE_AGENT_EFFORT", "medium")
    p, client = provider(message())
    p.complete(ModelRequest(system="s", messages=[]))
    assert client.calls[0]["model"] == "claude-sonnet-5-5"
    assert client.calls[0]["output_config"]["effort"] == "medium"


def test_served_model_is_recorded_when_a_fallback_answered():
    """The model that actually answered is recorded when a fallback served the request."""
    p, _ = provider(message(model="claude-opus-4-8"))
    response = p.complete(ModelRequest(system="s", messages=[]))
    assert (response.model_requested, response.model_served) == ("claude-opus-5-5", "claude-opus-4-8")


def test_tool_calls_are_extracted_and_raw_blocks_kept():
    """Tool calls are extracted while the raw content blocks are kept for the next turn."""
    blocks = [SimpleNamespace(type="text", text="checking"),
              SimpleNamespace(type="tool_use", id="toolu_1", name="get_run", input={"run_id": "run-1"})]
    p, _ = provider(message(stop="tool_use", blocks=blocks))
    response = p.complete(ModelRequest(system="s", messages=[]))
    assert response.tool_calls == [{"id": "toolu_1", "name": "get_run", "input": {"run_id": "run-1"}}]
    assert response.content == blocks                                    # echoed back unchanged


@pytest.mark.parametrize("stop, kind", [("refusal", "refusal"), ("max_tokens", "truncated")])
def test_refusal_and_truncation_are_errors_not_answers(stop, kind):
    """A refusal or a truncated reply raises an error instead of returning an answer."""
    p, _ = provider(message(stop=stop))
    with pytest.raises(ProviderError) as error:
        p.complete(ModelRequest(system="s", messages=[]))
    assert error.value.kind == kind


def _status_error(cls, status, headers=None):
    """Build an SDK HTTP status error with a given status and headers."""
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request, headers=headers or {}), body=None)


@pytest.mark.parametrize("error, kind, retryable", [
    (lambda: _status_error(anthropic.AuthenticationError, 401), "auth", False),
    (lambda: _status_error(anthropic.PermissionDeniedError, 403), "permission", False),
    (lambda: _status_error(anthropic.NotFoundError, 404), "not_found", False),
    (lambda: _status_error(anthropic.RateLimitError, 429, {"retry-after": "7"}), "rate_limit", True),
    (lambda: _status_error(anthropic.BadRequestError, 400), "bad_request", False),
    (lambda: _status_error(anthropic.InternalServerError, 500), "server", True),
    (lambda: anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x")), "timeout", True),
    (lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), "connection", True),
])
def test_sdk_errors_map_to_stable_kinds(error, kind, retryable):
    """SDK errors map to stable error kinds and retryable flags."""
    p, _ = provider(error())
    with pytest.raises(ProviderError) as raised:
        p.complete(ModelRequest(system="s", messages=[]))
    assert (raised.value.kind, raised.value.retryable) == (kind, retryable)
    if kind == "rate_limit":
        assert raised.value.details["retry_after"] == "7"


def test_invalid_structured_output_is_an_error():
    """Non-JSON structured output raises invalid_output."""
    p, _ = provider(message(text="not json"))
    with pytest.raises(ProviderError, match="invalid_output"):
        p.complete(ModelRequest(system="s", messages=[])).json()


def test_fake_provider_scripts_replies_and_errors():
    """The fake provider returns scripted replies and errors in order."""
    fake = FakeProvider([{"json": {"x": 1}}, ProviderError("timeout", "slow")])
    assert fake.complete(ModelRequest(system="s", messages=[])).json() == {"x": 1}
    with pytest.raises(ProviderError):
        fake.complete(ModelRequest(system="s", messages=[]))
    with pytest.raises(ProviderError):                                   # script exhausted
        fake.complete(ModelRequest(system="s", messages=[]))


def test_runner_and_watchdog_never_import_the_agent():
    """Model failure cannot disable execution safety: no import path exists."""
    for folder in ("executor", "watchdog", "store", "safety", "report"):
        for path in (ROOT / "controller" / folder).rglob("*.py"):
            tree = ast.parse(path.read_text())
            modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            assert not any("agent" in m or m == "anthropic" for m in modules), path
