"""Replaceable model provider adapter (P4-01).

The rest of the controller talks to `ModelProvider` only. Credentials and the
model ID come from the environment (never tracked files):

  FAULTLINE_AGENT_MODEL      default claude-opus-5-5
  FAULTLINE_AGENT_EFFORT     default high (diagnosis is intelligence-sensitive)
  FAULTLINE_AGENT_TIMEOUT_S  default 120
  ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login` profile (SDK-resolved)

Every failure becomes a ProviderError with a stable `kind`; nothing here ever
turns a failed call into an empty "successful" answer. The runner and watchdog
never import this module, so model failure cannot disable them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import time
from typing import Callable, Optional, Protocol

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ProviderError(RuntimeError):
    """kind: credentials | auth | permission | not_found | rate_limit | overloaded | timeout |
    connection | bad_request | server | refusal | truncated | invalid_output | budget"""

    def __init__(self, kind: str, message: str, retryable: bool = False, **details):
        """Create a provider error with a stable kind, a message, whether it is retryable and extra
        details.
        """
        super().__init__(f"{kind}: {message}")
        self.kind, self.message, self.retryable, self.details = kind, message, retryable, details

    def as_dict(self) -> dict:
        """The error as a JSON-safe dict for results and API responses."""
        return {"kind": self.kind, "message": self.message[:500], "retryable": self.retryable, **self.details}


@dataclass(frozen=True)
class ModelRequest:
    """A provider-neutral model request: system prompt, messages, token limit, tools, structured-
    output schema, effort and caching.
    """
    system: str
    messages: list
    max_tokens: int = 16000
    tools: Optional[list] = None
    output_schema: Optional[dict] = None     # JSON schema for structured output
    effort: Optional[str] = None
    cache: bool = True                       # auto-cache the stable prefix (system + context)


@dataclass
class ModelResponse:
    """A provider-neutral model response: text, tool calls, stop reason, raw content, requested and
    served model, usage, request id and latency.
    """
    text: str
    tool_calls: list[dict]                   # [{"id", "name", "input"}]
    stop_reason: str
    content: list                            # raw assistant content, echoed back in tool loops
    model_requested: str
    model_served: str
    usage: dict = field(default_factory=dict)
    request_id: Optional[str] = None
    latency_s: float = 0.0
    fallback_used: bool = False

    def json(self) -> dict:
        """Parse the response text as a JSON object, raising ProviderError('invalid_output')
        otherwise.
        """
        try:
            value = json.loads(self.text)
        except ValueError as error:
            raise ProviderError("invalid_output", f"structured output is not JSON: {error}")
        if not isinstance(value, dict):
            raise ProviderError("invalid_output", "structured output is not a JSON object")
        return value


class ModelProvider(Protocol):
    """Interface every model provider implements: a name, a model id and complete()."""
    name: str
    model: str

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Send one request to the model and return its response."""
        ...


# --------------------------------------------------------------- Anthropic
class AnthropicProvider:
    """Claude via the official SDK, with server-side refusal fallback opted in."""

    name = "anthropic"

    def __init__(self, model: Optional[str] = None, effort: Optional[str] = None,
                 timeout_s: Optional[float] = None, max_retries: int = 2, client=None):
        """Configure model, effort, timeout and retries from arguments or FAULTLINE_AGENT_* environment
        variables.
        """
        self.model = model or os.environ.get("FAULTLINE_AGENT_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.environ.get("FAULTLINE_AGENT_EFFORT", "high")
        self.timeout_s = timeout_s or float(os.environ.get("FAULTLINE_AGENT_TIMEOUT_S", "120"))
        self._client = client
        self._max_retries = max_retries

    @property
    def client(self):
        """Create the Anthropic SDK client on first use, turning a missing SDK or credentials into
        ProviderError.
        """
        if self._client is None:
            try:
                import anthropic
            except ImportError as error:
                raise ProviderError("credentials", "the anthropic SDK is not installed "
                                    "(pip install -r requirements-dev.txt)") from error
            try:
                self._client = anthropic.Anthropic(timeout=self.timeout_s, max_retries=self._max_retries)
            except anthropic.AnthropicError as error:
                raise ProviderError("credentials", f"no usable Anthropic credentials: {error}") from error
        return self._client

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Call Claude with the request (structured output, tools, prompt caching, server-side
        fallback) and map refusals, truncation and the reply into a ModelResponse.
        """
        params = {"model": self.model, "max_tokens": request.max_tokens, "system": request.system,
                  "messages": request.messages, "betas": [FALLBACK_BETA], "fallbacks": "default"}
        output_config = {"effort": request.effort or self.effort}
        if request.output_schema:
            output_config["format"] = {"type": "json_schema", "schema": request.output_schema}
        params["output_config"] = output_config
        if request.tools:
            params["tools"] = request.tools
        if request.cache:
            params["cache_control"] = {"type": "ephemeral"}
        started = time.monotonic()
        response = self._call(params)
        latency = round(time.monotonic() - started, 3)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderError("refusal", getattr(details, "explanation", None) or "the model declined",
                                category=getattr(details, "category", None), model_served=response.model)
        if response.stop_reason == "max_tokens":
            raise ProviderError("truncated", "response hit max_tokens before completing")
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [{"id": b.id, "name": b.name, "input": b.input} for b in response.content if b.type == "tool_use"]
        usage = response.usage
        return ModelResponse(
            text=text, tool_calls=calls, stop_reason=response.stop_reason,
            content=list(response.content),  # SDK blocks, echoed back unchanged in tool loops
            model_requested=self.model, model_served=response.model,
            usage={"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                   "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
                   "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0},
            request_id=getattr(response, "_request_id", None), latency_s=latency,
            fallback_used=any(getattr(b, "type", None) == "fallback" for b in response.content))

    def _call(self, params: dict):
        """Make the SDK call and translate every SDK exception into a ProviderError with a stable
        kind.
        """
        import anthropic
        try:
            return self.client.beta.messages.create(**params)
        except anthropic.CredentialsError as error:
            raise ProviderError("credentials", f"no usable Anthropic credentials: {error}") from error
        except anthropic.AuthenticationError as error:
            raise ProviderError("auth", error.message) from error
        except anthropic.PermissionDeniedError as error:
            raise ProviderError("permission", error.message) from error
        except anthropic.NotFoundError as error:
            raise ProviderError("not_found", f"model {self.model}: {error.message}") from error
        except anthropic.RateLimitError as error:
            raise ProviderError("rate_limit", error.message, retryable=True,
                                retry_after=error.response.headers.get("retry-after")) from error
        except anthropic.BadRequestError as error:
            raise ProviderError("bad_request", error.message) from error
        except anthropic.APIStatusError as error:
            kind = "overloaded" if error.status_code == 529 else "server" if error.status_code >= 500 else "bad_request"
            raise ProviderError(kind, error.message, retryable=error.status_code >= 500,
                                status=error.status_code) from error
        except anthropic.APITimeoutError as error:
            raise ProviderError("timeout", f"no response within {self.timeout_s}s", retryable=True) from error
        except anthropic.APIConnectionError as error:
            raise ProviderError("connection", str(error), retryable=True) from error


# ------------------------------------------------------------------- fakes
class FakeProvider:
    """Deterministic provider for tests: replies come from a script or a function."""

    name = "fake"

    def __init__(self, replies: Optional[list] = None,
                 responder: Optional[Callable[[ModelRequest], object]] = None, model: str = "fake-model"):
        """Script replies (or a responder function) for tests; every request is recorded."""
        self.replies, self.responder, self.model = list(replies or []), responder, model
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        """Return the next scripted reply (text, JSON, tool calls, response or exception) as a
        ModelResponse.
        """
        self.requests.append(request)
        reply = self.responder(request) if self.responder else (self.replies.pop(0) if self.replies else None)
        if reply is None:
            raise ProviderError("server", "fake provider has no scripted reply")
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, ModelResponse):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply.get("json")) if "json" in reply else reply.get("text", "")
        calls = [] if isinstance(reply, str) else reply.get("tool_calls", [])
        content = ([{"type": "text", "text": text}] if text else []) + \
            [{"type": "tool_use", **c} for c in calls]
        return ModelResponse(text=text, tool_calls=calls, stop_reason="tool_use" if calls else "end_turn",
                             content=content, model_requested=self.model, model_served=self.model,
                             usage={"input_tokens": 100, "output_tokens": 50}, latency_s=0.0)


def default_provider() -> ModelProvider:
    """The provider used when none is specified: Claude via the Anthropic SDK."""
    return AnthropicProvider()
