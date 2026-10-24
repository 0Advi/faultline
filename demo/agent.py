"""LangGraph agents for AI-driven chaos engineering (free-tier LLMs).

1. Planner   topology -> rank (LLM) -> safety_validator
             Ranks and proposes experiments from the dependency map, the fault
             catalog and past results, each with a reason and a hypothesis.
2. Analyst   planner -> investigator <-> tools -> advisor -> fixer <-> validator -> reviewer
             Investigates one finished run with read-only tools (summary, blast
             radius, traces, metrics, logs, container state, source code), writes
             a ranked findings report, then proposes a code fix that is
             syntax-checked (and retried) before it is offered for auto-fix.
3. Campaign  aggregate -> rank_weaknesses (LLM)
             Ranks weaknesses across several runs by severity, with fixes.

No tool can change the sandbox; faults and fixes are applied only by the
experiment engine and the auto-fix module after explicit operator action.

Provider (from .env): GEMINI_API_KEY (GEMINI_MODEL) or GROQ_API_KEY (GROQ_MODEL).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
from typing import Annotated, Callable, Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from . import evidence, topology
from .fixcheck import syntax_check, validate_fix  # noqa: F401  (re-exported for autofix)
from .catalog import CONTAINER_TARGETS, ENDPOINTS, EXCLUDED_TARGETS, HOT_PATCH, SCENARIOS

UPSTREAM = evidence.UPSTREAM
MAX_TOOL_ROUNDS = 4
TOOL_CHARS = 2400
Emit = Callable[[str, dict], None]


# ------------------------------------------------------------------ provider
def provider_info() -> dict:
    """Report which LLM provider and primary model are configured, from GEMINI_API_KEYS /
    GEMINI_API_KEY / GOOGLE_API_KEY (Gemini) or GROQ_API_KEY (Groq); both are None when no key is
    set.
    """
    if os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return {"provider": "gemini", "model": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")}
    if os.environ.get("GROQ_API_KEY"):
        return {"provider": "groq", "model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")}
    return {"provider": None, "model": None}


class _Timing(BaseCallbackHandler):
    """Reports every model call (duration, tokens) to the dashboard."""

    def __init__(self):
        """Start with no dashboard emitter or agent label; make_llm() sets them before each agent
        runs.
        """
        self.emit, self.agent, self.started = None, None, {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        """Remember when a model call started, keyed by its LangChain run id."""
        self.started[run_id] = time.monotonic()

    def on_llm_end(self, response, *, run_id, **kwargs):
        """Emit an 'llm_call' agent event with the call's duration and input/output token counts."""
        if not self.emit:
            return
        usage = {}
        try:
            usage = response.generations[0][0].message.usage_metadata or {}
        except (AttributeError, IndexError):
            pass
        self.emit("agent", {"agent": self.agent, "kind": "llm_call",
                            "seconds": round(time.monotonic() - self.started.pop(run_id, time.monotonic()), 1),
                            "tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens")})


TIMING = _Timing()


EXHAUSTED: set[str] = set()   # models whose free-tier daily quota is used up (this process)


def _quota_kind(error: Exception) -> str | None:
    """Classify a provider error: 'day' (daily free quota used up), 'minute' (rate limited),
    'unavailable' (503 overload) or None (any other error).
    """
    text = str(error)
    if "RESOURCE_EXHAUSTED" not in text and "429" not in text and "rate_limit" not in text.lower():
        return "unavailable" if "503" in text or "UNAVAILABLE" in text else None
    return "day" if "PerDay" in text or "per day" in text.lower() else "minute"


class ModelPool:
    """Free-tier model pool: the configured model first, then GEMINI_FALLBACK_MODELS.
    A model whose daily quota is exhausted is skipped for the rest of the session;
    per-minute limits and 503 overloads are waited out briefly on the same model.
    Exposes the three calls the agents use: invoke, bind_tools, with_structured_output."""

    def __init__(self, emit: "Emit | None", agent: str):
        """Build the ordered list of (model, key index, key) slots from the configured primary
        model, GEMINI_FALLBACK_MODELS and GEMINI_API_KEYS.
        """
        self.emit, self.agent = emit, agent
        info = provider_info()
        self.provider = info["provider"]
        if not self.provider:
            raise RuntimeError("No LLM configured: put GEMINI_API_KEY or GROQ_API_KEY in .env and restart.")
        extra = os.environ.get("GEMINI_FALLBACK_MODELS", "") if self.provider == "gemini" else ""
        models = list(dict.fromkeys([info["model"]] + [m.strip() for m in extra.split(",") if m.strip()]))
        keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
        keys = keys or [os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""]
        if self.provider != "gemini":
            keys = [""]
        # Each (model, key) pair has its own free daily quota; try them in order.
        self.slots = [(m, i, k) for m in models for i, k in enumerate(keys)]
        self.models = models
        self.current = info["model"]

    def _llm(self, name: str, key: str = ""):
        """Create the LangChain chat model for one model name and API key (Gemini with low thinking,
        or Groq), with timing callbacks and no SDK retries.
        """
        if self.provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI
            return ChatGoogleGenerativeAI(model=name, temperature=0.2, max_retries=0, google_api_key=key,
                                          timeout=120, callbacks=[TIMING],
                                          thinking_level=os.environ.get("GEMINI_THINKING", "low"))
        from langchain_groq import ChatGroq
        return ChatGroq(model=name, temperature=0.1, max_retries=0, timeout=90, callbacks=[TIMING])

    def _note(self, text: str):
        """Send a 'llm_retry' event to the dashboard explaining a key switch, rate limit or
        overload.
        """
        if self.emit:
            self.emit("agent", {"agent": self.agent, "kind": "llm_retry", "text": text})

    def call(self, build, messages):
        """Run one model call through the pool: try each non-exhausted slot in order, skip revoked
        keys, mark keys whose daily quota is used up, and back off on per-minute limits or 503s.
        """
        last = None
        for name, index, key in [s for s in self.slots if f"{s[0]}#{s[1]}" not in EXHAUSTED]:
            for attempt in range(4):
                try:
                    result = build(self._llm(name, key)).invoke(messages)
                    self.current = name
                    return result
                except Exception as error:
                    kind, last = _quota_kind(error), error
                    if "UNAUTHENTICATED" in str(error) or "API_KEY_INVALID" in str(error):
                        EXHAUSTED.add(f"{name}#{index}")
                        self._note(f"key {index + 1} rejected (invalid or revoked), switching key")
                        break
                    if kind == "day":
                        EXHAUSTED.add(f"{name}#{index}")
                        self._note(f"{name} (key {index + 1}): free daily quota used up, switching key")
                        break
                    if kind in ("minute", "unavailable") and attempt < 3:
                        self._note(f"{name}: {'rate limited' if kind == 'minute' else 'overloaded'}, retrying")
                        time.sleep(8 * (attempt + 1))
                        continue
                    if kind == "unavailable":
                        break
                    raise
        raise RuntimeError(f"all configured free-tier models are unavailable: {last}")

    def invoke(self, messages):
        """Plain chat completion through the pool."""
        return self.call(lambda llm: llm, messages)

    def bind_tools(self, tools):
        """Return a handle whose invoke() calls the model with these tools bound, through the pool."""
        return _Bound(self, lambda llm: llm.bind_tools(tools))

    def with_structured_output(self, schema):
        """Return a handle whose invoke() asks the model for output matching this Pydantic schema,
        through the pool.
        """
        return _Bound(self, lambda llm: llm.with_structured_output(schema))


class _Bound:
    """A pool call with a fixed model transformation (tools bound or structured output), so the
    agents can use it like a LangChain runnable.
    """
    def __init__(self, pool: ModelPool, build):
        """Store the pool and the function that turns a raw chat model into the bound variant."""
        self.pool, self.build = pool, build

    def invoke(self, messages):
        """Invoke the bound variant through the pool with the given messages."""
        return self.pool.call(self.build, messages)


def make_llm(emit: "Emit | None" = None, agent: str = "") -> ModelPool:
    """Point the timing callback at this agent's event stream and return a fresh ModelPool."""
    TIMING.emit, TIMING.agent = emit, agent
    return ModelPool(emit, agent)


def _text(message) -> str:
    """Return the plain text of an AI message (handles Gemini's list-of-blocks content), or str() of
    anything else.
    """
    return message.text if isinstance(message, AIMessage) else str(message)


def _cap(value, limit: int = TOOL_CHARS) -> str:
    """Serialise a tool result to compact JSON (if needed) and truncate it to the character limit,
    so tool outputs stay within the model's budget.
    """
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def _structured(llm, schema: type[BaseModel], messages: list) -> dict:
    """Ask the model for output matching a Pydantic schema; if native structured output fails, ask
    for raw JSON and validate it. Returns the validated object as a dict.
    """
    try:
        result = llm.with_structured_output(schema).invoke(messages)
        if isinstance(result, BaseModel):
            return result.model_dump()
        if isinstance(result, dict):
            return schema.model_validate(result).model_dump()
    except Exception:
        pass
    reply = llm.invoke(messages + [HumanMessage(
        "Reply with ONLY a JSON object matching this JSON schema, no prose or code fences:\n"
        + json.dumps(schema.model_json_schema()))])
    match = re.search(r"\{.*\}", _text(reply), re.S)
    if not match:
        raise RuntimeError(f"model returned no {schema.__name__}")
    return schema.model_validate(json.loads(match.group(0))).model_dump()


SYSTEM = """You are a senior Site Reliability Engineer running AI-driven chaos engineering on the
OpenTelemetry Astronomy Shop, a 25-service e-commerce app (Envoy frontend-proxy -> Next.js frontend
-> gRPC/HTTP services: ad (Java), cart (.NET) + valkey, checkout (Go), currency (C++), email (Ruby),
payment (Node.js), product-catalog (Go) + PostgreSQL, quote (PHP), recommendation (Python),
shipping (Rust), image-provider (nginx)). Base every statement on tool output; never invent numbers.
Be concise and specific."""


# ======================================================================
# 1. Planner
# ======================================================================
class ProposedExperiment(BaseModel):
    """One experiment proposed by the AI planner: which catalog scenario, target and intensity to
    run, for how long, how important and risky it is, why, what is expected to happen and what it
    may affect.
    """
    scenario: str = Field(description="Scenario id from the catalog")
    target: str = Field(description="Target service (must be allowed for the scenario)")
    variant: str = Field(description="Intensity, one of the scenario's variants")
    fault_s: int = Field(description="Fault duration in seconds, 15-40")
    priority: int = Field(description="1 (low) to 10 (run first)")
    risk: Literal["low", "medium", "high"]
    reason: str = Field(description="Why this experiment is informative, citing the dependency map")
    hypothesis: str
    expected_blast_radius: list[str] = Field(description="Services/user paths expected to be affected")


class Plan(BaseModel):
    """The AI planner's output: a short strategy summary and experiments ranked best first."""
    summary: str = Field(description="2-3 sentences: overall strategy")
    experiments: list[ProposedExperiment] = Field(description="5 experiments ranked best first")


class PlanState(TypedDict):
    """LangGraph state for the planner graph: topology context, raw plan and the validated
    experiments.
    """
    context: dict
    plan: dict
    validated: list


def plan_experiments(emit: Emit, history: list[dict]) -> dict:
    """Run the planner agent (topology -> rank -> safety_validator) and return ranked, validated
    experiment proposals; also emits progress and the final 'plan' event.
    """
    llm = make_llm(emit, "planner")
    info = provider_info()

    def analyse_topology(state: PlanState):
        """Deterministic step: collect services with their fan-in and user paths, call edges with
        live call counts, the fault catalog and past run scores into the planner's context.
        """
        g = topology.graph()
        crit = topology.criticality(g["edges"])
        services = {s: {"user_paths": len(v["endpoints"]), "callers": v["callers"]}
                    for s, v in crit.items() if s not in EXCLUDED_TARGETS and s != "users"}
        catalog = {sid: {"kind": s["kind"], "mechanism": s.get("net"), "category": s["category"], "title": s["title"],
                         "targets": s.get("targets", [s["target"]]), "variants": s["variants"]}
                   for sid, s in SCENARIOS.items()}
        edges = [f'{e["from"]}->{e["to"]}' + (f' ({e["calls"]} calls/h)' if e.get("calls") else "")
                 for e in g["edges"] if e["from"] != "users"]
        past = [{"scenario": h["scenario"], "target": h["target"], "score": h.get("score"),
                 "status": h.get("status")} for h in history[:12]]
        context = {"services": services, "edges": edges, "catalog": catalog, "past_runs": past,
                   "user_paths": {k: v["services"] for k, v in ENDPOINTS.items()},
                   "excluded_targets": EXCLUDED_TARGETS}
        emit("agent", {"agent": "planner", "kind": "tool_result", "tool": "dependency_map",
                       "text": f"{len(services)} services, {len(edges)} call edges, "
                               f"{len(catalog)} fault scenarios, {len(past)} past runs"})
        return {"context": context}

    def rank(state: PlanState):
        """LLM step: rank the most informative or riskiest experiments from the context, covering at
        least three categories.
        """
        ctx = state["context"]
        prompt = ("Plan the next chaos experiments for this system. Choose the most informative or "
                  "riskiest experiments first, using the dependency map (fan-in, critical user paths like "
                  "checkout) and past results (do not repeat a scenario+target that already ran unless "
                  "a fix was applied). Cover at least 3 different categories (e.g. errors, crash, "
                  "slowdown/network, resource/database, traffic). Use only scenario ids, targets and "
                  "variants from the catalog.\n\n" + json.dumps(ctx, separators=(",", ":")))
        emit("agent", {"agent": "planner", "kind": "thought", "text": "Ranking experiments by blast potential, "
                       "business criticality and coverage..."})
        return {"plan": _structured(llm, Plan, [SystemMessage(SYSTEM), HumanMessage(prompt)])}

    def safety_validator(state: PlanState):
        """Deterministic step: drop unknown scenarios, replace disallowed targets and invalid
        intensities with safe defaults, clamp durations and number the experiments.
        """
        out = []
        for i, exp in enumerate(state["plan"]["experiments"]):
            spec = SCENARIOS.get(exp["scenario"])
            issues = []
            if not spec:
                issues.append("unknown scenario")
            else:
                allowed = spec.get("targets", [spec["target"]])
                if exp["target"] not in allowed:
                    issues.append(f"target {exp['target']} not allowed; using {spec['target']}")
                    exp["target"] = spec["target"]
                if exp["variant"] not in spec["variants"]:
                    issues.append(f"variant {exp['variant']} invalid; using {spec['default']}")
                    exp["variant"] = spec["default"]
                exp["category"] = spec["category"]
                exp["title"] = spec["title"]
            exp["fault_s"] = max(10, min(int(exp.get("fault_s") or 20), 45))
            exp["valid"] = bool(spec)
            exp["issues"] = issues
            exp["rank"] = i + 1
            out.append(exp)
        out = [e for e in out if e["valid"]]
        return {"validated": out}

    graph = StateGraph(PlanState)
    graph.add_node("topology", analyse_topology)
    graph.add_node("rank", rank)
    graph.add_node("safety_validator", safety_validator)
    graph.add_edge(START, "topology")
    graph.add_edge("topology", "rank")
    graph.add_edge("rank", "safety_validator")
    graph.add_edge("safety_validator", END)
    emit("agent", {"agent": "planner", "kind": "start", **info})
    final = graph.compile().invoke({"context": {}, "plan": {}, "validated": []})
    result = {"summary": final["plan"].get("summary"), "experiments": final["validated"], "provider": info["provider"], "model": llm.current}
    emit("plan", result)
    return result


# ======================================================================
# 2. Analyst (+ fixer)
# ======================================================================
class Evidence(BaseModel):
    """One observed fact supporting the report, and the name of the tool whose output shows it."""
    claim: str = Field(description="Specific observed fact with numbers")
    source: str = Field(description="Name of the tool whose output shows it")


class Finding(BaseModel):
    """One weakness the experiment revealed, with its severity and why it matters."""
    severity: Literal["critical", "high", "medium", "low"]
    title: str
    detail: str = Field(description="What weakness the experiment revealed and why it matters")


class Recommendation(BaseModel):
    """One prioritised fix: what to change where, why, and how re-running the experiment proves it
    worked.
    """
    priority: Literal["P0", "P1", "P2"]
    title: str
    component: str = Field(description="Service / file to change")
    problem: str
    change: str = Field(description="Concrete code/config/alert change")
    verification: str = Field(description="How re-running this experiment proves the fix")


class Report(BaseModel):
    """The analyst's final report for one experiment: verdict on the hypothesis, root cause,
    customer impact, blast radius, evidence, ranked weaknesses and recommendations.
    """
    headline: str
    hypothesis_verdict: Literal["confirmed", "partially confirmed", "refuted"]
    verdict_reason: str
    root_cause: str = Field(description="Causal chain service by service")
    customer_impact: str
    blast_radius: str
    evidence: list[Evidence]
    findings: list[Finding] = Field(description="Weaknesses ranked by severity, most severe first")
    recommendations: list[Recommendation] = Field(description="3 to 5 prioritised fixes")
    confidence: Literal["high", "medium", "low"]


class Fix(BaseModel):
    """One minimal code fix as an exact find/replace edit in an application source file, with an
    explanation and the metric a re-run should improve.
    """
    file: str = Field(description="Path relative to src/, e.g. payment/index.js")
    title: str
    explanation: str = Field(description="Plain-language explanation of the change and why it fixes the weakness")
    find: str = Field(description="Exact existing code to replace: contiguous complete lines copied verbatim")
    replace: str = Field(description="Replacement code")
    expected_effect: str = Field(description="Which measured metric of a re-run should improve, and how")


class FileChoice(BaseModel):
    """The source file the fixer should change, chosen from a candidate list, and why."""
    file: str = Field(description="One path from the candidate list")
    reason: str


class AnalystState(TypedDict):
    """LangGraph state for the analyst graph: message history, investigation plan, tool rounds,
    report, fix, chosen file, fix attempts and errors, and the reviewer's verdict.
    """
    messages: Annotated[list, add_messages]
    plan: str
    rounds: int
    report: dict
    fix: dict
    fix_file: str
    fix_attempts: int
    fix_error: str
    review: dict


def make_tools(run: dict) -> list:
    """Build the analyst's read-only LangChain tools, each bound to this run's saved record
    (summary, blast radius, endpoint stats, traces, error spans, logs, container state, source
    code).
    """
    @tool
    def get_experiment_summary() -> str:
        """Scenario, injected fault, hypothesis, status/abort reason, per-phase totals, time to first
        failure, time to recover, and the deterministic resilience score breakdown."""
        return _cap({k: run.get(k) for k in (
            "title", "category", "target", "variant", "injection", "hypothesis", "status", "abort_reason",
            "totals", "time_to_first_failure_s", "time_to_recover_s", "resilience")})

    @tool
    def get_blast_radius() -> str:
        """Expected vs actually impacted user paths, services with errors, and whether the impact was contained."""
        return _cap(run.get("blast_radius"))

    @tool
    def get_endpoint_stats(endpoint: str) -> str:
        """Per-phase success %, p50/p95 latency and error statuses for one user path:
        home, catalog, product, recommendations, ads, image, shipping, cart_add, cart_get, checkout."""
        stats = run.get("endpoint_stats", {})
        return _cap({phase: (stats.get(phase) or {}).get(endpoint) for phase in stats})

    @tool
    def get_failed_trace(index: int = 0) -> str:
        """Jaeger error span chain (service, operation, gRPC code, HTTP status, message) of a failed request."""
        traces = run.get("traces") or []
        if not traces:
            return "No failed request traces were captured (failures may have been timeouts only)."
        return _cap(traces[max(0, min(index, len(traces) - 1))])

    @tool
    def get_error_spans() -> str:
        """Prometheus span-metric ERROR counts by service/span during the run, plus calls/errors per service."""
        return _cap({"error_spans": run.get("span_detail"), "per_service": run.get("span_errors")})

    @tool
    def get_service_logs(service: str) -> str:
        """Distinct warning/error log messages (with counts) from one service during the run."""
        logs = (run.get("logs") or {}).get(service)
        if logs is None:
            logs = evidence.service_logs(service, run["started_at"])
        return _cap(logs or f"No warning/error logs from {service}.")

    @tool
    def get_container_state(service: str) -> str:
        """Container status after the run: running, restarts, OOM kill, health, memory limit."""
        from .faults import container_state
        return _cap(container_state(service))

    @tool
    def get_network_evidence() -> str:
        """Network-layer evidence: tc/iptables rules verified in the fault container's namespace, its
        TCP counters baseline vs fault (retransmissions, SYN retries, timeouts, failed connects, resets,
        RTT), and per-hop network+transport time vs server time from traces."""
        return _cap(run.get("network") or "No network evidence was captured for this run.")

    @tool
    def list_source_files(service: str) -> str:
        """List application source files of a service (paths relative to src/)."""
        try:
            return _cap(evidence.list_source(service))
        except PermissionError as error:
            return str(error)

    @tool
    def read_source(path: str, start_line: int = 1, num_lines: int = 120) -> str:
        """Read lines of an application source file (path relative to src/, e.g. checkout/main.go)."""
        try:
            return _cap(evidence.read_source(path, start_line, num_lines), 6000)
        except PermissionError as error:
            return str(error)

    @tool
    def search_source(service: str, pattern: str) -> str:
        """Regex search in a service's source; returns file:line: text matches."""
        try:
            return _cap(evidence.search_source(service, pattern))
        except PermissionError as error:
            return str(error)

    return [get_experiment_summary, get_blast_radius, get_endpoint_stats, get_failed_trace, get_error_spans,
            get_service_logs, get_container_state, get_network_evidence, list_source_files, read_source, search_source]


def _headline(run: dict) -> str:
    """One-paragraph summary of a run (scenario, injection, hypothesis, status, success per phase,
    recovery time, score) used at the top of every analyst prompt.
    """
    t = run.get("totals", {})
    r = run.get("resilience") or {}
    return (f"Experiment {run['run_id']}: {run['title']} on {run['target']} ({run.get('injection')}). "
            f"Hypothesis: {run['hypothesis']} Status: {run['status']}"
            f"{' - ' + run['abort_reason'] if run.get('abort_reason') else ''}. Request success "
            f"baseline {t.get('baseline', {}).get('success')}% / fault {t.get('fault', {}).get('success')}% / "
            f"recovery {t.get('recovery', {}).get('success')}%. Time to recover: {run.get('time_to_recover_s')} s. "
            f"Resilience score {r.get('score')}/100 ({r.get('grade')}).")


def _transcript(messages: list, limit: int = 14000) -> str:
    """Render the investigator's tool calls, results and notes as text (trimmed to the last
    characters), for the advisor and fixer prompts.
    """
    parts = []
    for m in messages:
        if isinstance(m, AIMessage):
            for c in m.tool_calls or []:
                parts.append(f"CALL {c['name']}({json.dumps(c['args'])})")
            if _text(m):
                parts.append(f"ANALYST: {_text(m)}")
        elif isinstance(m, ToolMessage):
            parts.append(f"RESULT {m.name}: {m.content}")
    return "\n".join(parts)[-limit:]


SERVICE_NAMES = sorted({p.name for p in UPSTREAM.iterdir() if p.is_dir()}, key=len, reverse=True)


def _candidate_files(run: dict, report: dict) -> list[str]:
    """Source files of the target and of services the report blames (hot-patchable ones first)."""
    text = json.dumps({k: report.get(k) for k in ("root_cause", "findings", "recommendations")}).lower()
    services = [run["target"]] + [s for s in SERVICE_NAMES if s in text and s != run["target"]]
    files: list[str] = []
    for svc in services:
        try:
            files += [f for f in evidence.list_source(svc) if "test" not in f.lower()]
        except PermissionError:
            continue
    hot = [f for f in HOT_PATCH if f in files]
    return list(dict.fromkeys(hot + files))[:40]


def analyse(run: dict, emit: Emit) -> dict:
    """Run the analyst agent on a finished run (planner -> investigator <-> tools -> advisor ->
    choose_file -> fixer <-> validator -> reviewer), streaming each step to the dashboard;
    returns the report, fix and review.
    """
    llm = make_llm(emit, "analyst")
    info = provider_info()
    tools = make_tools(run)
    investigator_llm = llm.bind_tools(tools)

    def planner(state: AnalystState):
        """LLM step: write a short investigation plan for this run and seed the investigator's
        conversation with it.
        """
        reply = llm.invoke([SystemMessage(SYSTEM), HumanMessage(
            _headline(run) + "\nWrite a 3-4 step investigation plan (one line each) naming the evidence "
            "you will check. Tools: " + ", ".join(t.name for t in tools))])
        plan = _text(reply)
        return {"plan": plan, "rounds": 0, "fix_attempts": 0, "messages": [HumanMessage(
            _headline(run) + "\n\nYour plan:\n" + plan + "\n\nInvestigate now. Call several tools per turn. "
            "Find the causal chain: the user-facing symptom, the trace/logs of the failure, and the source "
            "code responsible for the behaviour (error handling, timeouts, retries, fallbacks). When you have "
            "enough evidence, reply with a concise summary of findings and no tool calls.")]}

    def investigator(state: AnalystState):
        """LLM step: call evidence tools (several per turn) or, when done, summarise the findings."""
        reply = investigator_llm.invoke([SystemMessage(SYSTEM)] + state["messages"])
        return {"messages": [reply], "rounds": state["rounds"] + 1}

    def route(state: AnalystState):
        """Continue to the tools while the investigator requests tool calls and the round budget
        allows; otherwise go to the advisor.
        """
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) and state["rounds"] < MAX_TOOL_ROUNDS else "advisor"

    def advisor(state: AnalystState):
        """LLM step: turn the investigation transcript into the structured Report (verdict, root
        cause, ranked weaknesses, recommendations).
        """
        prompt = (f"{_headline(run)}\n\nInvestigation transcript (tool results are ground truth):\n"
                  f"{_transcript(state['messages'])}\n\nWrite the final report. Rank findings (weaknesses) by "
                  "severity. Recommendations must fix what the evidence shows (error classification, "
                  "timeouts/fail-fast, retries with idempotency, fallbacks/graceful degradation, circuit "
                  "breakers, resource limits, alerting) and say how re-running this experiment verifies "
                  "them. Evidence sources must be names of tools that were called.")
        return {"report": _structured(llm, Report, [SystemMessage(SYSTEM), HumanMessage(prompt)])}

    def choose_file(state: AnalystState):
        """LLM step: pick the one source file the fix should change, from the target's and blamed
        services' files (hot-patchable first).
        """
        candidates = _candidate_files(run, state["report"])
        prompt = (f"Root cause: {state['report']['root_cause']}\nTop recommendation: "
                  + json.dumps(state["report"]["recommendations"][:1]) +
                  "\n\nWhich ONE source file should be changed to fix the most important weakness? It must "
                  "be in the service responsible for the weakness. Files marked [live] can be hot-patched and "
                  "re-tested immediately; prefer one only if it truly addresses the weakness.\n"
                  + "\n".join(f"- {f}{' [live]' if f in HOT_PATCH else ''}" for f in candidates))
        choice = _structured(llm, FileChoice, [SystemMessage(SYSTEM), HumanMessage(prompt)])
        chosen = choice["file"].strip().removeprefix("src/")
        if chosen not in candidates:
            chosen = candidates[0]
        emit("agent", {"agent": "analyst", "kind": "thought", "node": "choose_file",
                       "text": f"Fix target: {chosen} - {choice['reason']}"})
        return {"fix_file": chosen}

    def fixer(state: AnalystState):
        """LLM step: write a minimal find/replace fix in the chosen file from its full source; on a
        retry, include why the previous fix was rejected.
        """
        rel = state["fix_file"]
        lines = (UPSTREAM / rel).read_text().splitlines()
        source = "\n".join(lines[:900])
        report = state["report"]
        retry = (f"\n\nYour previous fix was rejected: {state['fix_error']}\nFix it." if state.get("fix_error") else "")
        prompt = (f"{_headline(run)}\n\nRoot cause: {report['root_cause']}\nRecommendations: "
                  + json.dumps(report["recommendations"][:3]) +
                  f"\n\nWrite ONE minimal, safe code fix in {rel} for the most important weakness, as an exact "
                  "find/replace edit. The fault-injection code (feature-flag checks that simulate the failure) "
                  "is part of the experiment: never delete, comment out or bypass it. Make the code more "
                  "resilient instead (timeouts, fail-fast, retries with idempotency, fallbacks, circuit "
                  "breakers, correct error mapping). Keep behaviour for healthy requests identical. `find` must be complete, "
                  "contiguous lines copied verbatim from the file below (no line numbers), unique in the file. "
                  f"Set file to {rel}.\n\n=== {rel} ===\n{source}{retry}")
        fix = _structured(llm, Fix, [SystemMessage(SYSTEM), HumanMessage(prompt)])
        fix["file"] = rel
        return {"fix": fix, "fix_attempts": state["fix_attempts"] + 1}

    def validator(state: AnalystState):
        """Deterministic step: check the fix with fixcheck.validate_fix and record the error (if
        any) for a retry.
        """
        result = validate_fix(state["fix"])
        fix = {**state["fix"], "validation": result}
        if result["ok"]:
            fix["file"] = result["file"]
        return {"fix": fix, "fix_error": "" if result["ok"] else result["error"]}

    def fix_route(state: AnalystState):
        """Send a rejected fix back to the fixer (up to three attempts); otherwise go to the
        reviewer.
        """
        return "fixer" if state["fix_error"] and state["fix_attempts"] < 3 else "reviewer"

    def reviewer(state: AnalystState):
        """Deterministic grounding review: count evidence items that cite tools actually called,
        flag percentages that match no measured value, and record whether the fix is valid.
        """
        called = {c["name"] for m in state["messages"] if isinstance(m, AIMessage) for c in (m.tool_calls or [])}
        items = state["report"].get("evidence", [])
        measured = set()
        for t in (run.get("totals") or {}).values():
            if t.get("success") is not None:
                measured |= {round(t["success"]), round(100 - t["success"])}
        for phase in (run.get("endpoint_stats") or {}).values():
            for s in phase.values():
                if s.get("success") is not None:
                    measured |= {round(s["success"]), round(100 - s["success"])}
        variant = str(run.get("variant", ""))
        if variant.endswith("%"):
            measured.add(round(float(variant[:-1])))
        quoted = {round(float(x)) for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", json.dumps(state["report"]))}
        return {"review": {"tools_called": sorted(called), "evidence_total": len(items),
                           "evidence_grounded": sum(1 for e in items if e.get("source") in called),
                           "ungrounded": [e["claim"] for e in items if e.get("source") not in called],
                           "percentages_unmatched": sorted(quoted - measured - {100, 0}),
                           "fix_valid": bool(state["fix"].get("validation", {}).get("ok")),
                           "fix_attempts": state["fix_attempts"]}}

    graph = StateGraph(AnalystState)
    for name, fn in [("planner", planner), ("investigator", investigator), ("tools", ToolNode(tools)),
                     ("advisor", advisor), ("choose_file", choose_file), ("fixer", fixer), ("validator", validator), ("reviewer", reviewer)]:
        graph.add_node(name, fn)
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "investigator")
    graph.add_conditional_edges("investigator", route, {"tools": "tools", "advisor": "advisor"})
    graph.add_edge("tools", "investigator")
    graph.add_edge("advisor", "choose_file")
    graph.add_edge("choose_file", "fixer")
    graph.add_edge("fixer", "validator")
    graph.add_conditional_edges("validator", fix_route, {"fixer": "fixer", "reviewer": "reviewer"})
    graph.add_edge("reviewer", END)

    emit("agent", {"agent": "analyst", "kind": "start", **info})
    final: dict = {}
    for update in graph.compile().stream({"messages": [], "rounds": 0}, stream_mode="updates",
                                         config={"recursion_limit": 40}):
        for node, delta in update.items():
            if node == "planner":
                emit("agent", {"agent": "analyst", "kind": "plan", "node": node, "text": delta["plan"]})
            elif node == "investigator":
                msg = delta["messages"][-1]
                for c in msg.tool_calls or []:
                    emit("agent", {"agent": "analyst", "kind": "tool_call", "node": node, "tool": c["name"],
                                   "args": c["args"]})
                if _text(msg):
                    emit("agent", {"agent": "analyst", "kind": "thought", "node": node, "text": _text(msg)})
            elif node == "tools":
                for m in delta["messages"]:
                    emit("agent", {"agent": "analyst", "kind": "tool_result", "node": node, "tool": m.name,
                                   "text": str(m.content)[:700]})
            elif node == "advisor":
                final["report"] = delta["report"]
                emit("agent", {"agent": "analyst", "kind": "report_ready", "node": node,
                               "text": delta["report"].get("headline", "")})
            elif node == "fixer":
                emit("agent", {"agent": "analyst", "kind": "fix", "node": node,
                               "text": f"{delta['fix'].get('title')} ({delta['fix'].get('file')})"})
            elif node == "validator":
                final["fix"] = delta["fix"]
                v = delta["fix"]["validation"]
                emit("agent", {"agent": "analyst", "kind": "validation", "node": node, "ok": v["ok"],
                               "text": v.get("syntax", {}).get("output") if v["ok"] else v["error"]})
            elif node == "reviewer":
                final["review"] = delta["review"]
    result = {**final, "provider": info["provider"], "model": llm.current, "run_id": run["run_id"]}
    emit("analysis", result)
    return result


# ======================================================================
# 3. Campaign report
# ======================================================================
class Weakness(BaseModel):
    """One weakness across the campaign, ranked by severity, with the services and runs that show
    it, its impact and the recommended fix.
    """
    rank: int
    severity: Literal["critical", "high", "medium", "low"]
    title: str
    services: list[str]
    evidence_runs: list[str] = Field(description="run ids that show it")
    impact: str
    fix: str


class CampaignReport(BaseModel):
    """The campaign findings report: executive summary, why the system got its resilience score,
    ranked weaknesses and follow-up experiments.
    """
    executive_summary: str
    score_explanation: str = Field(description="Why the system got its resilience score")
    weaknesses: list[Weakness] = Field(description="Ranked most severe first")
    next_experiments: list[str] = Field(description="2-3 follow-up experiments")


def campaign_report(runs: list[dict], analyses: dict, emit: Emit) -> dict:
    """Run the campaign agent over several runs and their analyses (aggregate -> rank_weaknesses);
    returns the overall score (mean of run scores) and the ranked report.
    """
    llm = make_llm(emit, "campaign")
    info = provider_info()
    scores = [r["resilience"]["score"] for r in runs if r.get("resilience")]
    overall = round(sum(scores) / len(scores)) if scores else None

    class CState(TypedDict):
        """LangGraph state for the campaign graph: per-run digest and the final report."""
        digest: list
        report: dict

    def aggregate(state: CState):
        """Deterministic step: summarise each run (experiment, status, score, recovery, blast
        radius, root cause, findings, fix title).
        """
        digest = []
        for r in runs:
            a = analyses.get(r["run_id"]) or {}
            rep = a.get("report") or {}
            digest.append({"run_id": r["run_id"], "experiment": f"{r['title']} on {r['target']} ({r['variant']})",
                           "status": r["status"], "abort_reason": r.get("abort_reason"),
                           "score": r.get("resilience"), "time_to_recover_s": r.get("time_to_recover_s"),
                           "blast_radius": {k: (r.get("blast_radius") or {}).get(k) for k in
                                            ("impacted_endpoints", "unexpected_endpoints", "contained")},
                           "root_cause": rep.get("root_cause"), "findings": rep.get("findings"),
                           "fix": (a.get("fix") or {}).get("title")})
        return {"digest": digest}

    def rank_weaknesses(state: CState):
        """LLM step: merge and rank weaknesses across all experiments into the CampaignReport."""
        prompt = (f"Overall resilience score (mean of experiments): {overall}/100.\nExperiments:\n"
                  + json.dumps(state["digest"], default=str) +
                  "\n\nWrite the campaign findings report: rank the system's weaknesses by severity across "
                  "all experiments (merge duplicates), each with impact and recommended fix.")
        return {"report": _structured(llm, CampaignReport, [SystemMessage(SYSTEM), HumanMessage(prompt)])}

    graph = StateGraph(CState)
    graph.add_node("aggregate", aggregate)
    graph.add_node("rank_weaknesses", rank_weaknesses)
    graph.add_edge(START, "aggregate")
    graph.add_edge("aggregate", "rank_weaknesses")
    graph.add_edge("rank_weaknesses", END)
    emit("agent", {"agent": "campaign", "kind": "start", **info})
    final = graph.compile().invoke({"digest": [], "report": {}})
    result = {"overall_score": overall, "report": final["report"], "runs": [r["run_id"] for r in runs], "provider": info["provider"], "model": llm.current}
    emit("campaign_report", result)
    return result
