#!/usr/bin/env python3
"""Cut scripts/record_showcase.py footage into a <= 3-minute video that follows the demo brief:
sandbox -> failure injected + application behaviour -> observation/metrics -> AI analysis -> safety.

Usage: python scripts/edit_showcase.py [--out artifacts/showcase-video] [--max 178]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edit_demo import FFMPEG, render, short  # noqa: E402

RUNS = Path("artifacts/demo-runs")


def ev_first(ev, kind, n=None):
    """The first logged event of a kind (optionally for scenario n)."""
    return next((e for e in ev if e["kind"] == kind and (n is None or e.get("n") == n)), None)


def wrap(text: str, width: int = 92) -> str:
    """Burned-in captions: at most two lines that fit the 1920 px frame."""
    import textwrap
    lines = []
    for part in text.split("\n"):
        lines += textwrap.wrap(part, width) or [""]
    return "\n".join(lines[:3])


def build(out: Path) -> list[dict]:
    """Plan the showcase segments from the event log: intro, then per scenario selection, fault,
    shop impact, recovery and analysis.
    """
    doc = json.loads((out / "events.json").read_text())
    ev, starts = doc["events"], doc["starts"]
    offset = starts["shop"] - starts["dashboard"]          # shop video starts this much later
    dash, shop = str(out / "dashboard.webm"), str(out / "shop.webm")
    to_dash = lambda e: e["t"] if e["video"] == "dashboard" else e["t"] + offset
    segs = []

    def full(name, start, dur, title, caption, target=None):
        """Add a full-screen dashboard segment, sped up to fit the target length."""
        segs.append({"name": name, "kind": "full", "src": dash, "start": start, "dur": dur,
                     "speed": max(1.0, dur / target) if target else 1.0, "title": title, "caption": caption})

    def side(name, start, end, target, title, caption, timer=None):
        """Add a side-by-side dashboard and shop segment, optionally with an on-screen timer."""
        dur = max(1.0, end - start)
        seg = {"name": name, "kind": "side", "src": dash, "src2": shop, "start": start,
               "start2": max(0.0, start - offset), "dur": dur, "speed": max(1.0, dur / target),
               "title": title, "caption": caption}
        if timer:
            seg["timer"] = timer
        segs.append(seg)

    intro = ev_first(ev, "intro")
    full("intro", intro["t"], 7, "AI Chaos Lab: running in our own sandbox",
         "25-service OpenTelemetry Astronomy Shop on Docker, localhost only; all health checks green")
    selects = [e for e in ev if e["kind"] == "select"]
    for sel in selects:
        n = sel["n"]
        done = ev_first(ev, "done", n) or {}
        rec = json.loads((RUNS / f"{done['run']}.json").read_text()) if done.get("run") else {}
        ana_path = RUNS / f"{done.get('run')}.analysis.json"
        ana = json.loads(ana_path.read_text()) if ana_path.exists() else {}
        title = f"{n}/{len(selects)}  {rec.get('title', sel['sid'])} on {sel['target']}"
        full(f"s{n}a", sel["t"] + 0.5, 4.5, title,
             "Dashboard tells the presenter where the fault will show: " + short(sel.get("expect"), 75))
        run, live = ev_first(ev, "run", n), ev_first(ev, "fault_live", n)
        if not (run and live):
            continue
        side(f"s{n}b", run["t"] - 1.5, live["t"], 6, title,
             "Before: the shop is healthy. Health gate passes, the fault is journaled, then injected")
        stop = ev_first(ev, "end_fault", n) or ev_first(ev, "auto_stopped", n)
        fault_end = min(stop["t"] if stop else live["t"] + 22, live["t"] + 24)
        click = ev_first(ev, "place_order_click", n)
        side(f"s{n}c", live["t"], fault_end, 18, title,
             f"FAULT LIVE ({short(rec.get('injection'), 55)}). Customer sees: {short(sel.get("expect"), 150)}",
             timer="Order pending" if click and click.get("label") == "during fault" else None)
        recovered = ev_first(ev, "recovered", n)
        if stop and recovered:
            if stop["kind"] == "auto_stopped":
                reason = (rec.get("abort_reason") or "").removeprefix("guardrail: ")
                cap = f"Safety guardrail auto-stopped the fault: {short(reason, 85)}"
            else:
                cap = (f"'End fault now': fault restored and verified; shop healthy again in "
                       f"{rec.get('time_to_recover_s')} s")
            # Keep the last ~12 s before recovery at near real speed so the result is readable.
            end = to_dash(recovered) + 3
            side(f"s{n}d", max(stop["t"], end - 13), end, 9, title, cap)
        res, blast = ev_first(ev, "results", n), ev_first(ev, "blast", n)
        r = rec.get("resilience") or {}
        b = rec.get("blast_radius") or {}
        if res and blast:
            full(f"s{n}e", res["t"] + 0.5, 3.5, title,
                 f"Observed live: path availability {r.get('parts', {}).get('availability')}%, time to recover "
                 f"{rec.get('time_to_recover_s')} s, resilience score {r.get('score')}/100")
            full(f"s{n}f", blast["t"] + 1, 4, title,
                 f"Blast radius from traces + metrics: {', '.join(b.get('impacted_endpoints', {})) or 'none'} "
                 f"{'(contained)' if b.get('contained') else '(spread)'}")
        an, an_end = ev_first(ev, "analysis", n), ev_first(ev, "analysis_end", n)
        if an and an_end:
            rep = ana.get("report") or {}
            weak = (rep.get("findings") or [{}])[0]
            recs = sorted(rep.get("recommendations") or [], key=lambda x: x.get("priority", "P9"))
            full(f"s{n}g", an["t"], an_end["t"] - an["t"] + 1, title,
                 f"AI weakness [{weak.get('severity', '-')}]: {short(weak.get('title'), 70)}\n"
                 f"Recommended fix: {short(recs[0]['title'] if recs else '-', 80)}", target=9)
    safety = ev_first(ev, "safety")
    if safety:
        full("safety", safety["t"], 9, "Safety controls",
             "Health gate · guardrails auto-stop on blast radius, error rate, latency · 60 s cap\n"
             "Every fault journaled and auto-restored (verified) · kill switch · read-only AI tools")
    for seg in segs:
        seg["caption"] = wrap(seg["caption"])
    return segs


def main():
    """Command line: plan, render and concatenate the showcase video."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="artifacts/showcase-video")
    parser.add_argument("--max", type=float, default=178)
    args = parser.parse_args()
    out = Path(args.out)
    segs = build(out)
    total = sum(s["dur"] / s["speed"] for s in segs)
    if total > args.max:
        k = total / args.max
        for s in segs:
            s["speed"] *= k
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        parts = []
        for s in segs:
            path = tmp / f"{s['name']}.mp4"
            render(tmp, s, path)
            parts.append(path)
            print(f"{s['name']:7} {s['kind']:4} {s['dur']:5.1f}s @x{s['speed']:.2f} -> {s['dur'] / s['speed']:4.1f}s", flush=True)
        (tmp / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
                        str(tmp / "list.txt"), "-c", "copy", str(out / "showcase.mp4")], check=True)
    (out / "edit.json").write_text(json.dumps(segs, indent=1))
    print(f"showcase.mp4: {sum(s['dur'] / s['speed'] for s in segs):.0f} s", flush=True)


if __name__ == "__main__":
    main()
