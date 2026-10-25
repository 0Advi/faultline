"""Markdown findings report for a campaign or a single run."""
from __future__ import annotations

import json
from pathlib import Path

from .catalog import ENDPOINTS

RUNS_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "demo-runs"


def _load(name: str):
    """Load a JSON file from artifacts/demo-runs, or None if it does not exist."""
    path = RUNS_DIR / name
    return json.loads(path.read_text()) if path.exists() else None


def _run_section(run: dict, analysis: dict | None, comparison: dict | None) -> list[str]:
    """Markdown section for one run: scenario, score, success per phase, recovery, blast radius, AI
    verdict, weaknesses, recommendations, fix diff and auto-fix comparison.
    """
    r = run.get("resilience") or {}
    t = run.get("totals") or {}
    blast = run.get("blast_radius") or {}
    out = [f"## {run['title']} on `{run['target']}` ({run['variant']}) - {run['run_id']}", "",
           f"- **Category:** {run.get('category')}  |  **Injection:** {run.get('injection')}  |  **Status:** {run['status']}"
           + (f" ({run['abort_reason']})" if run.get("abort_reason") else ""),
           f"- **Hypothesis:** {run['hypothesis']}",
           f"- **Resilience score:** {r.get('score')}/100 (grade {r.get('grade')}) - "
           + ", ".join(f"{k} {v}" for k, v in (r.get("parts") or {}).items()),
           f"- **Request success:** baseline {t.get('baseline', {}).get('success')}% / fault "
           f"{t.get('fault', {}).get('success')}% / recovery {t.get('recovery', {}).get('success')}%",
           f"- **Time to first failure:** {run.get('time_to_first_failure_s')} s  |  **Time to recover:** "
           f"{run.get('time_to_recover_s')} s",
           "- **Blast radius:** impacted paths: " + (", ".join(
               f"{ENDPOINTS[k]['label']} ({'; '.join(v)})" for k, v in (blast.get("impacted_endpoints") or {}).items())
               or "none") + f"; unexpected: {', '.join(blast.get('unexpected_endpoints') or []) or 'none'}", ""]
    net = run.get("network") or {}
    if net.get("fault"):
        b, f = net.get("baseline") or {}, net["fault"]
        rules = (net.get("rules") or {}).get("qdisc", []) + (net.get("rules") or {}).get("iptables", [])
        out[-1:-1] = [f"- **Network ({net['service']}):** retransmitted {b.get('retransmit_pct')}% -> "
                      f"{f.get('retransmit_pct')}% of segments; failed connects {f.get('connect_failures')}; "
                      f"resets sent {f.get('resets_sent')}; median RTT {f.get('rtt_p50_ms')} ms"
                      + (f"; rules verified: `{'; '.join(rules)}`" if rules else "")]
        out[-1:-1] = [f"- **Hop {h['edge']}:** network {h['baseline_network_ms']} -> {h['fault_network_ms']} ms, "
                      f"server {h['baseline_server_ms']} -> {h['fault_server_ms']} ms" for h in net.get("hops", [])[:3]]
    rep = (analysis or {}).get("report")
    if rep:
        out += [f"**AI verdict:** hypothesis {rep['hypothesis_verdict']} - {rep['verdict_reason']}", "",
                f"**Root cause:** {rep['root_cause']}", "", f"**Customer impact:** {rep['customer_impact']}", "",
                "| Severity | Weakness | Detail |", "|---|---|---|"]
        out += [f"| {f['severity']} | {f['title']} | {f['detail']} |" for f in rep.get("findings", [])]
        out += ["", "| Priority | Recommendation | Component | Change | Verify by |", "|---|---|---|---|---|"]
        out += [f"| {x['priority']} | {x['title']} | `{x['component']}` | {x['change']} | {x['verification']} |"
                for x in rep.get("recommendations", [])]
        fix = (analysis or {}).get("fix")
        if fix and (fix.get("validation") or {}).get("ok"):
            out += ["", f"**Proposed fix - {fix['title']}** (`{fix['file']}`, "
                        f"{fix['validation'].get('deploy')}): {fix['explanation']}", "", "```diff",
                    fix["validation"]["diff"].rstrip(), "```"]
    if comparison:
        b, a = comparison["before"], comparison["after"]
        out += ["", f"**Fix verified by re-run** ({comparison['after_run']}): score {b['score']} -> {a['score']} "
                    f"({comparison['score_delta']:+d}); fault success {b['fault_success']}% -> {a['fault_success']}%; "
                    f"misleading 4xx {b['misleading_4xx']} -> {a['misleading_4xx']}; timeouts {b['timeouts']} -> "
                    f"{a['timeouts']}."]
    return out + [""]


def markdown(campaign_id: str | None, run_id: str | None) -> tuple[str, str]:
    """Build the downloadable findings report for a campaign (summary, ranked weaknesses, every run)
    or a single run; returns (text, filename).
    """
    lines = ["# Chaos engineering findings report", "",
             "System under test: OpenTelemetry Astronomy Shop 3.1.0 (25 services, Docker Compose sandbox).", ""]
    if campaign_id:
        doc = _load(f"{campaign_id}.json") or {}
        rep = doc.get("report") or {}
        lines += [f"Campaign `{campaign_id}` - {len(doc.get('runs', []))} experiments - "
                  f"**overall resilience score {rep.get('overall_score')}/100**", ""]
        cr = rep.get("report")
        if cr:
            lines += ["## Executive summary", "", cr["executive_summary"], "", cr["score_explanation"], "",
                      "## Ranked weaknesses", "", "| # | Severity | Weakness | Services | Impact | Fix |",
                      "|---|---|---|---|---|---|"]
            lines += [f"| {w['rank']} | {w['severity']} | {w['title']} | {', '.join(w['services'])} | "
                      f"{w['impact']} | {w['fix']} |" for w in cr["weaknesses"]]
            lines += ["", "**Next experiments:** " + "; ".join(cr.get("next_experiments", [])), ""]
        run_ids = doc.get("runs", [])
        name = f"{campaign_id}-findings.md"
    else:
        run_ids, name = [run_id], f"{run_id}-findings.md"
    lines += ["# Experiments", ""]
    for rid in run_ids:
        run = _load(f"{rid}.json")
        if run:
            lines += _run_section(run, _load(f"{rid}.analysis.json"), _load(f"{rid}.comparison.json"))
    return "\n".join(lines), name
