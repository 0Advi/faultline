"use strict";
const $ = (s) => document.querySelector(s);
/** Escape a value for safe insertion into HTML. */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
/** Format a value with a unit, or a dash when missing. */
const fmt = (v, unit = "") => (v === null || v === undefined ? "–" : `${v}${unit}`);

let status = null, plan = null, graph = null, source = null, evTab = "trace";
let live = freshLive();          // the experiment currently shown in panels 3-6
let findings = null;             // {run, analysis, comparison} shown in panel 8
let campaign = null;             // {id, experiments, index, report}
let feed = [];
let busy = false;

/** Empty state for the experiment shown in the live panels. */
function freshLive() { return { run: null, records: [], timeline: {}, record: null, guard: null }; }

// =================================================================== status
/** Fetch /api/status and render the health, LLM and safety chips, scenario designer and run history. */
async function loadStatus() {
  status = await (await fetch("/api/status")).json();
  busy = status.busy;
  const h = status.health, llm = status.llm;
  const chip = (ok, text) => `<span class="chip ${ok ? "ok" : "bad"}"><i></i>${esc(text)}</span>`;
  const faults = status.active_faults.length;
  const fixes = Object.keys(status.active_fixes || {}).length;
  $("#chips").innerHTML = [chip(h.shop, "Shop"), chip(h.prometheus, "Prometheus"), chip(h.jaeger, "Jaeger"),
    chip(h.flags_baseline && !faults, faults ? `${faults} fault active` : "No active faults"),
    fixes ? chip(true, `${fixes} fix deployed`) : "",
    chip(!!llm.provider, llm.provider ? `${llm.provider} · ${llm.model}` : "LLM not configured")].join("");
  if (!$("#scenario").options.length) buildDesigner();
  $("#st-scenarios").textContent = Object.keys(status.scenarios).length;
  $("#st-runs").textContent = status.runs.length;
  $("#st-faults").textContent = faults;
  $("#st-fault-cell").className = `stat ${faults ? "red" : "green"}`;
  renderHistory();
  setBusy(busy);
}

/** Fill the scenario picker (grouped by category) from the status catalog. */
function buildDesigner() {
  const groups = {};
  for (const [id, s] of Object.entries(status.scenarios)) (groups[s.category] ||= []).push([id, s]);
  $("#scenario").innerHTML = Object.entries(groups).map(([cat, items]) =>
    `<optgroup label="${esc(cat)}">${items.map(([id, s]) => `<option value="${id}">${esc(s.title)}</option>`).join("")}</optgroup>`).join("");
  pickScenario("payment-failure");
}

let shopView = null, shopWin = null;
/** The shop's base URL. */
const shopUrl = () => (status && status.shop_url) || "http://localhost:8081";

/** Fetch where the selected fault shows up in the shop and render that hint. */
async function loadShopView() {
  const sid = $("#scenario").value, target = $("#target").value;
  shopView = await (await fetch(`/api/shop-view?scenario=${encodeURIComponent(sid)}&target=${encodeURIComponent(target)}`)).json();
  if ($("#presenter").checked) applyPresenterDefaults();
  renderShopView();
}

/**
 * Render the 'See it in the shop' panel: which page to open, what to look for and how visible it
 * is.
 */
function renderShopView() {
  const v = shopView;
  if (!v || !v.path) { $("#shopview").innerHTML = ""; return; }
  const label = { high: "clearly visible", medium: "visible if you look", low: "subtle", none: "not visible to customers" }[v.visibility];
  $("#shopview").innerHTML = `<div class="sv-head"><span class="sv-title">See it in the shop</span>
      <span class="vis ${esc(v.visibility)}">${esc(label)}</span>
      <button id="open-shop" title="Opens (or reuses) a shop tab">Open shop at ${esc(v.path)} ↗</button></div>
    <ol>${v.steps.map((x) => `<li>${esc(x)}</li>`).join("")}<li>${$("#presenter").checked
      ? "Presenter mode reloads the shop tab you opened with the button above when FAULT LIVE starts"
      : "Reload the shop tab once FAULT LIVE shows (pages already loaded keep their old data)"}</li></ol>
    <div class="sv-expect"><b>You should see:</b> ${esc(v.expect)}</div>`;
  $("#open-shop").onclick = () => openShop();
}

/** Open the shop page affected by the selected scenario in a named tab. */
function openShop() {
  if (!shopView) return;
  shopWin = window.open(shopUrl() + shopView.path, "chaos-shop");
}

/** In presenter mode, use the scenario's most visible variant and longer fault window. */
function applyPresenterDefaults() {
  const p = (shopView && shopView.presenter) || {};
  if (p.variant && [...$("#variant").options].some((o) => o.value === p.variant)) $("#variant").value = p.variant;
  $("#fault_s").value = Math.min(60, p.fault_s || 60);
  $("#baseline_s").value = 6;
}

/**
 * Select a scenario (and optionally target and variant) in the designer and update its options and
 * hypothesis.
 */
function pickScenario(id, target, variant) {
  const s = status.scenarios[id];
  $("#scenario").value = id;
  const targets = s.targets || [s.target];
  $("#target").innerHTML = targets.map((t) => `<option>${esc(t)}</option>`).join("");
  $("#target").value = target && targets.includes(target) ? target : s.target;
  $("#target").disabled = targets.length === 1;
  $("#variant").innerHTML = s.variants.map((v) => `<option>${esc(v)}</option>`).join("");
  $("#variant").value = variant && s.variants.includes(variant) ? variant : s.default;
  if (!live.run) $("#hypothesis").textContent = s.hypothesis.replaceAll("{target}", $("#target").value);
  loadShopView();
}

/** Enable or disable the action buttons while a job runs. */
function setBusy(b) {
  busy = b;
  for (const id of ["#run", "#plan-btn", "#campaign-btn", "#hero-plan", "#hero-run"]) $(id).disabled = b || (id === "#campaign-btn" && !plan?.experiments?.length);
  document.querySelectorAll("[data-needs-idle]").forEach((el) => (el.disabled = b));
  $("#run").textContent = b && status?.job === "experiment" ? "Running…" : "Run experiment";
}

// =================================================================== events
let lastSeq = 0, phaseClock = null, lostSince = null;
/**
 * Open the server-sent event stream and route each event to handle(); reconnects resume after the
 * last sequence.
 */
function connect() {
  if (source) source.close();
  lastSeq = 0;
  source = new EventSource("/api/events?after=0");
  source.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.seq <= lastSeq) return;          // replayed after a reconnect
    lastSeq = ev.seq; lostSince = null;
    handle(ev);
  };
  source.onerror = () => { if (busy && !lostSince) { lostSince = Date.now(); line("Lost connection to the lab server, reconnecting...", "warn"); } };
}

// Phase countdown + detection of runs that died with the server (never look "stuck").
const PHASE_LABEL = { baseline: "Baseline (steady state)", inject: "Injecting fault", fault: "Fault active, guardrails armed",
  restore: "Restoring", recovered: "Recovery check", evidence: "Collecting evidence" };
setInterval(async () => {
  if (!busy) return;
  if (lostSince && Date.now() - lostSince > 6000) {
    try {
      const st = await (await fetch("/api/status")).json();
      if (!st.busy) {
        lostSince = null; setBusy(false); source?.close();
        line(`Run interrupted: the lab server restarted or stopped. Active faults: ${st.active_faults.length} (every fault is journaled and restored on restart).`, "error");
        loadStatus();
      } else { connect(); }
    } catch { line("Lab server is not reachable. Start it with: python -m demo.server", "error"); }
    return;
  }
  if (!phaseClock || !live.run) { document.title = "AI Chaos Lab"; return; }
  const secs = Math.round((Date.now() - phaseClock.at) / 1000);
  const left = Math.max(0, (live.run.durations?.fault || 0) - secs);
  document.title = phaseClock.name === "fault" ? `● FAULT LIVE ${left}s · AI Chaos Lab`
    : phaseClock.name === "baseline" ? "Baseline… · AI Chaos Lab"
    : ["restore", "recovered"].includes(phaseClock.name) ? "Restored · AI Chaos Lab" : "AI Chaos Lab";
  const planned = { baseline: live.run.durations?.baseline, fault: live.run.durations?.fault, recovered: live.run.durations?.recovery }[phaseClock.name];
  const n = live.records.length;
  const extra = phaseClock.name === "evidence" ? " (waiting for traces and metrics to flush, up to ~40 s)" : "";
  $("#runline").innerHTML = `${phaseClock.name === "fault" ? `<span class="live-banner">FAULT LIVE · ${left}s left</span>` : ""}<b>${esc(PHASE_LABEL[phaseClock.name] || phaseClock.name)}</b> · ${secs}s${planned ? ` of ${planned}s` : ""} · ${n} requests observed${extra}${phaseClock.note ? ` · ${esc(phaseClock.note)}` : ""}`;
}, 1000);

// Live events arrive ~15/s; redraw the heat-map at most twice a second to keep the browser light.
let renderPending = false;
/** Throttle live-panel redraws to at most one every 500 ms. */
function schedule() { if (!renderPending) { renderPending = true; setTimeout(() => { renderPending = false; renderLive(); }, 500); } }

/**
 * Apply one streamed event (run, request, phase, guardrail, record, agent step, plan, campaign,
 * analysis...) to the page state.
 */
function handle(ev) {
  const d = ev.data;
  switch (ev.kind) {
    case "run":
      live = freshLive(); live.run = d;
      $("#hypothesis").textContent = d.hypothesis;
      renderMeta(); renderLive(); renderTopology();
      break;
    case "phase":
      live.timeline[d.name] = d.t;
      phaseClock = { name: d.name, at: Date.now(), note: d.description || (d.results || []).join("; ") || (d.ttr != null ? `recovered in ${d.ttr}s` : "") };
      $("#end-fault").disabled = !["inject", "fault"].includes(d.name);
      if (["fault", "restore"].includes(d.name)) renderNetwork();   // active rules appear / disappear
      if (d.name === "fault" && $("#presenter").checked && shopWin && !shopWin.closed && shopView) {
        shopWin.location.href = shopUrl() + shopView.path;   // fresh load: the fault is live now
      }
      if (d.name === "inject") line(`Fault injected: ${d.description}`);
      else if (d.name === "restore") line(`Fault removed: ${(d.results || []).join("; ")}`);
      else if (d.name === "recovered") line(`All healthy paths recovered ${d.ttr}s after restore`);
      else if (d.name === "not_recovered") line("Did not fully recover within 90 s", "error");
      else if (d.name === "baseline") line("Measuring steady state on all customer paths…");
      schedule(); break;
    case "req": live.records.push(d); schedule(); break;
    case "log":
      line(d.message, d.level);
      if ((d.message || "").startsWith("guardrail") || (d.message || "").startsWith("health gate")) live.guard = d.message;
      schedule(); break;
    case "evidence":
      live.record = d; live.records = d.requests || live.records; live.timeline = d.timeline || live.timeline; live.run ||= d;
      line(d.status === "aborted" ? `Stopped safely: ${d.abort_reason}` : "Evidence collected; AI analyst investigating…", d.status === "aborted" ? "warn" : "");
      renderLive(); renderEvidence(); renderTopology(); renderMeta(); loadStatus(); renderNetwork(true);
      break;
    case "agent": feed.push(d); renderFeed(); if (d.kind === "error" && findings) renderFindings(); break;
    case "analysis":
      findings = { run: live.record, analysis: d, comparison: null };
      renderFindings(); renderStepper(); loadStatus(); break;
    case "plan": plan = d; renderPlan(); break;
    case "campaign": campaign = { ...(campaign || {}), ...d }; renderCampaign(); break;
    case "campaign_report": campaign = { ...(campaign || {}), report: d }; renderCampaign(); break;
    case "campaign_done": campaign = { ...(campaign || {}), doc: d, report: d.report }; renderCampaign(); break;
    case "autofix":
      if (findings) { findings.autofix = d; renderFindings(); }
      line(d.stage === "deployed" ? `Fix deployed to ${d.service}: ${d.message}` : `Deploying fix to ${d.file}…`); break;
    case "comparison": if (findings) { findings.comparison = d; renderFindings(); } loadStatus(); break;
    case "done": phaseClock = null; document.title = "AI Chaos Lab"; $("#end-fault").disabled = true; setBusy(false); loadStatus(); renderStepper();
      if (live.record) line(live.record.status === "completed" ? `Finished: ${live.record.title} on ${live.record.target}. Score ${live.record.resilience?.score ?? "–"}/100.` : `Stopped safely: ${live.record.abort_reason}`, live.record.status === "completed" ? "" : "warn");
      break;
  }
}

/** Show a one-line status message (red for warnings and errors). */
function line(text, level) {
  $("#runline").innerHTML = `<span style="color:${level === "warn" || level === "error" ? "var(--bad)" : "inherit"}">${esc(text)}</span>`;
}

// ===================================================================== plan
/** Render the AI planner's ranked experiments with Load and Run buttons. */
function renderPlan() {
  if (!plan || !plan.experiments) return;
  const rows = plan.experiments.map((e, i) => `
    <tr><td>${e.rank}</td>
      <td><b>${esc(e.title)}</b><div class="muted">${esc(e.target)} · ${esc(e.variant)} · ${e.fault_s}s</div></td>
      <td><span class="tag">${esc(e.category)}</span></td>
      <td><span class="risk ${esc(e.risk)}">${esc(e.risk)}</span></td>
      <td><span class="scorebar" style="width:${e.priority * 6}px"></span> ${e.priority}/10</td>
      <td class="why">${esc(e.reason)}<div><i>Hypothesis:</i> ${esc(e.hypothesis)}</div>
        <div>Expected blast radius: ${esc((e.expected_blast_radius || []).join(", "))}</div>
        ${e.issues?.length ? `<div style="color:var(--warn)">Validator: ${esc(e.issues.join("; "))}</div>` : ""}</td>
      <td><button data-load="${i}">Load</button> <button data-run="${i}" data-needs-idle>Run</button></td></tr>`).join("");
  $("#plan-out").innerHTML = `<div class="verdict">${esc(plan.summary)} <span class="muted">— ${esc(plan.provider)} · ${esc(plan.model)}</span></div>
    <table class="plan-table"><tr><th>#</th><th>Proposed injection</th><th>Category</th><th>Risk</th><th>Priority</th><th>Reason (AI)</th><th></th></tr>${rows}</table>`;
  setBusy(busy);
}

/** Load a planned experiment into the designer. */
function loadProposal(e) {
  pickScenario(e.scenario, e.target, e.variant);
  $("#fault_s").value = e.fault_s;
  $("#hypothesis").textContent = e.hypothesis;
  location.hash = "#experiment";
}

/** Read the experiment parameters and guardrails from the designer form. */
function designerParams() {
  return { scenario: $("#scenario").value, target: $("#target").value, variant: $("#variant").value,
    baseline_s: +$("#baseline_s").value, fault_s: +$("#fault_s").value, recovery_s: +$("#recovery_s").value,
    guardrails: { max_unexpected_endpoints: +$("#g_unexpected").value, max_error_rate: +$("#g_error").value,
      max_unrelated_p95_ms: +$("#g_p95").value, max_fault_s: +$("#g_cap").value } };
}

/** POST JSON to the lab server; shows the error and returns null on failure. */
async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const doc = await res.json();
  if (!res.ok) { line(doc.error || res.statusText, "error"); return null; }
  return doc;
}

/**
 * Start a server job (experiment, campaign, analysis, auto-fix): lock the buttons, reset panels and
 * listen for events.
 */
async function startJob(url, body, opts = {}) {
  setBusy(true);                                  // disable at once: no double submits
  if (opts.reset !== false) { live = freshLive(); feed = []; renderFeed(); renderLive(); }
  connect();
  const ok = await post(url, body);
  if (ok) { status.job = ok.job; setBusy(true); } else { setBusy(false); }
}

// ===================================================================== live
/** Render the pills describing the current experiment (category, target, variant, AI-proposed). */
function renderMeta() {
  const r = live.record || live.run;
  if (!r) return;
  $("#exp-meta").innerHTML = [`<span class="pill">${esc(r.category)}</span>`, `<span class="pill">target: ${esc(r.target)}</span>`,
    `<span class="pill">${esc(r.variant)}</span>`, r.proposed_by ? `<span class="pill ai">proposed by AI planner</span>` : "",
    r.label ? `<span class="pill ai">${esc(r.label)}</span>` : "",
    live.record ? `<span class="pill ${live.record.status === "completed" ? "ok" : "bad"}">${esc(live.record.status)}</span>` : ""].join("");
}

/** Redraw the live panels: stepper, KPI tiles and request matrix. */
function renderLive() { renderStepper(); renderKpis(); renderMatrix(); }

/**
 * Highlight the current lifecycle step (baseline, inject, fault, restore, recovered, evidence,
 * agent).
 */
function renderStepper() {
  const tl = live.timeline, order = ["baseline", "inject", "fault", "restore", "recovered", "evidence", "agent"];
  let cur = null;
  if (findings?.run?.run_id && live.record?.run_id === findings.run.run_id && findings.analysis) cur = "done";
  else if (live.record) cur = feed.some((f) => f.agent === "analyst") ? "agent" : (busy ? "agent" : "done");
  else if ("evidence" in tl) cur = "evidence";
  else if ("recovered" in tl || "not_recovered" in tl) cur = "evidence";
  else if ("restore" in tl) cur = "recovered";
  else if ("inject" in tl) cur = "fault";
  else if ("baseline" in tl) cur = "baseline";
  const idx = cur === "done" ? order.length : order.indexOf(cur);
  document.querySelectorAll("#stepper li").forEach((li, i) => {
    li.className = cur === null ? "" : i < idx ? "done" : i === idx ? "active" : "";
    if (li.dataset.step === "recovered" && "not_recovered" in tl) li.className = "fail";
    if (li.dataset.step === "fault" && live.record?.status === "aborted") li.className = "fail";
  });
}

/** Mean per-path success rate and median latency for one phase. */
function pathStats(phase) {
  const rows = live.records.filter((r) => r.phase === phase);
  if (!rows.length) return null;
  const by = {};
  for (const r of rows) (by[r.endpoint] ||= []).push(r.ok);
  const vals = Object.values(by).map((v) => v.filter(Boolean).length / v.length);
  return { n: rows.length, rate: Math.round(1000 * vals.reduce((a, b) => a + b, 0) / vals.length) / 10 };
}

/**
 * Render the KPI tiles: success under fault, latency, impacted paths, recovery time and resilience
 * score.
 */
function renderKpis() {
  const rec = live.record, res = rec?.resilience;
  const tile = (label, value, note, cls = "") => `<div class="kpi ${cls}"><div class="label">${label}</div><div class="value">${value}</div><div class="note">${note}</div></div>`;
  const b = pathStats("baseline"), f = pathStats("fault");
  const impacted = rec ? Object.keys(rec.blast_radius.impacted_endpoints).length : null;
  const guard = rec ? (rec.status === "aborted" ? "Tripped" : "Passed") : (live.timeline.inject !== undefined ? "Armed" : "–");
  $("#kpis").innerHTML = [
    tile("Resilience score", res ? `${res.score}<small style="font-size:14px"> ${res.grade}</small>` : "–", res ? "deterministic, 5 factors" : "after the run", res ? (res.score >= 75 ? "ok" : "bad") : ""),
    tile("Path availability", f ? `${f.rate}%` : b ? `${b.rate}%` : "–", f ? `under fault (baseline ${b?.rate ?? "–"}%)` : "baseline", f ? (f.rate >= 95 ? "ok" : "bad") : ""),
    tile("Impacted paths", impacted === null ? "–" : `${impacted}/10`, rec ? (rec.blast_radius.contained ? "contained to dependents" : "spread beyond dependents") : "blast radius", rec ? (rec.blast_radius.contained ? "ok" : "bad") : ""),
    tile("Time to impact", fmt(rec?.time_to_first_failure_s, "s"), "inject → first failure"),
    tile("Time to recover", fmt(rec?.time_to_recover_s, "s"), "restore → all paths healthy", rec ? (rec.time_to_recover_s !== null && rec.time_to_recover_s <= 10 ? "ok" : "bad") : ""),
    tile("Guardrails", guard, live.guard ? esc(live.guard).slice(0, 60) : "auto-stop thresholds", guard === "Tripped" ? "bad" : guard === "Passed" ? "ok" : ""),
  ].join("");
}

/**
 * Draw the request matrix: one row per user path, one dot per request over time, with phase
 * markers.
 */
function renderMatrix() {
  const eps = Object.keys(status?.endpoints || {});
  const recs = live.records, tl = live.timeline;
  const tMax = Math.max(20, ...recs.map((r) => r.t), ...Object.values(tl).map(Number)) + 1;
  const L = 150, W = 1150, rowH = 22, T = 22, H = T + eps.length * rowH + 22;
  const cw = (W - L) / Math.ceil(tMax), x = (t) => L + t * cw;
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Health of each customer path over time">`;
  if (tl.inject !== undefined) {
    const x1 = x(tl.restore ?? tMax);
    svg += `<rect x="${x(tl.inject)}" y="${T - 4}" width="${Math.max(2, x1 - x(tl.inject))}" height="${eps.length * rowH + 6}" fill="var(--band)" stroke="var(--bad)" stroke-dasharray="4 4" stroke-opacity=".6"/>`;
    svg += `<text x="${x(tl.inject) + 4}" y="12" style="fill:var(--bad)">fault injected</text>`;
  }
  if (tl.restore !== undefined) svg += `<text x="${x(tl.restore) + 4}" y="12">restored</text>`;
  const rec = tl.recovered;
  if (rec !== undefined) svg += `<line x1="${x(rec)}" x2="${x(rec)}" y1="${T - 4}" y2="${T + eps.length * rowH}" stroke="var(--ok)" stroke-width="2"/><text x="${x(rec) + 4}" y="${H - 6}" style="fill:var(--ok)">healthy again</text>`;
  const buckets = {};
  for (const r of recs) {
    const k = `${r.endpoint}|${Math.floor(r.t)}`;
    const b = (buckets[k] ||= { n: 0, bad: 0, slow: 0 });
    b.n++; if (!r.ok) b.bad++; else if ((r.latency_ms || 0) > 1000) b.slow++;
  }
  const expected = new Set(live.record?.blast_radius?.expected_endpoints || []);
  eps.forEach((ep, i) => {
    const y = T + i * rowH;
    svg += `<text x="${L - 8}" y="${y + 15}" text-anchor="end" style="${expected.has(ep) ? "fill:var(--ink);font-weight:600" : ""}">${esc(status.endpoints[ep].label)}</text>`;
    for (let s = 0; s < Math.ceil(tMax); s++) {
      const b = buckets[`${ep}|${s}`];
      if (!b) continue;
      const c = b.bad ? "var(--bad)" : b.slow ? "var(--warn)" : "var(--ok)";
      const op = b.bad ? 0.45 + 0.55 * (b.bad / b.n) : 0.85;
      svg += `<rect x="${x(s) + 0.5}" y="${y + 2}" width="${Math.max(1, cw - 1)}" height="${rowH - 4}" rx="2" fill="${c}" fill-opacity="${op}"><title>${esc(ep)} @${s}s: ${b.n} req, ${b.bad} failed, ${b.slow} slow</title></rect>`;
    }
  });
  const step = tMax > 80 ? 20 : 10;
  for (let t = 0; t <= tMax; t += step) svg += `<text x="${x(t)}" y="${H - 6}" text-anchor="middle">${t}s</text>`;
  if (!recs.length) svg += `<text x="${(L + W) / 2}" y="${H / 2}" text-anchor="middle">Run an experiment to see every customer path live</text>`;
  $("#matrix").innerHTML = svg + "</svg>";
}

// ================================================================ topology
/** Fetch and draw the dependency graph, colouring the target and impacted services of the shown run. */
async function renderTopology() {
  // While a run is live, show only its own blast radius (not the previous run's).
  const runId = live.run ? live.record?.run_id : findings?.run?.run_id;
  graph = await (await fetch(`/api/topology${runId ? `?run=${runId}` : ""}`)).json();
  const blast = graph.blast;
  const state = (id) => {
    if (!blast) return live.run && id === live.run.target ? "injected" : "healthy";
    const s = blast.services[id];
    if (s) return s;
    if (blast.potential_services.includes(id)) return "potential";
    return "healthy";
  };
  const pos = Object.fromEntries(graph.nodes.map((n) => [n.id, n.xy]));
  const W = 1000, H = 580;
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Service dependency graph with blast radius">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--edge)"/></marker>
    <marker id="arrb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--bad)"/></marker></defs>`;
  [["Users", 60], ["Edge", 200], ["Frontend", 350], ["Services", 530], ["Data & downstream", 720], ["Platform", 900]]
    .forEach(([t, x]) => (svg += `<text x="${x}" y="${H - 2}" text-anchor="middle" style="fill:var(--muted);font-size:11px;letter-spacing:.06em">${t.toUpperCase()}</text>`));
  svg += `<rect x="835" y="15" width="130" height="540" rx="10" fill="var(--soft)" stroke="var(--line)"/>`;
  for (const e of graph.edges) {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) continue;
    const hot = blast && ["injected", "errors", "degraded"].includes(state(e.from)) && ["injected", "errors"].includes(state(e.to));
    const w = e.calls ? Math.min(4, 1 + Math.log10(e.calls)) : 1;
    const x1 = a[0] + 55, x2 = b[0] - 55;
    let d;
    if (Math.abs(a[0] - b[0]) < 10) { const dx = 70; d = `M${a[0] + 55},${a[1]} C${a[0] + 55 + dx},${a[1]} ${b[0] + 55 + dx},${b[1]} ${b[0] + 55},${b[1]}`; }
    else d = `M${x1},${a[1]} C${(x1 + x2) / 2},${a[1]} ${(x1 + x2) / 2},${b[1]} ${x2},${b[1]}`;
    svg += `<path d="${d}" fill="none" stroke="${hot ? "var(--bad)" : "var(--edge)"}" stroke-width="${hot ? w + 1 : w}" ${e.observed ? "" : 'stroke-dasharray="4 3"'} marker-end="url(#${hot ? "arrb" : "arr"})"><title>${esc(e.from)} → ${esc(e.to)}${e.calls ? ` · ${e.calls} calls (Jaeger, last hour)` : " · static map"}</title></path>`;
  }
  const fills = { injected: ["var(--inj)", "#fff", "var(--inj)"], errors: ["var(--bad)", "#120806", "var(--bad)"], degraded: ["var(--warn)", "#140b04", "var(--warn)"],
    potential: ["var(--card)", "var(--ink)", "var(--warn)"], healthy: ["var(--card)", "var(--ink)", "var(--ok)"] };
  for (const n of graph.nodes) {
    const [x, y] = n.xy, st = n.platform ? "platform" : state(n.id);
    const [bg, fg, stroke] = n.platform ? ["var(--card)", "var(--muted)", "var(--line)"] : fills[st];
    const w = n.platform ? 112 : 110;
    svg += `<g class="${n.platform ? "plat" : ""}"><rect x="${x - w / 2}" y="${y - 13}" width="${w}" height="26" rx="7" fill="${bg}" stroke="${stroke}" stroke-width="${st === "healthy" || n.platform ? 1.2 : 2}" ${st === "potential" ? 'stroke-dasharray="4 3"' : ""}/>
      <text x="${x}" y="${y + 4}" text-anchor="middle" style="fill:${fg};${st === "injected" ? "font-weight:700" : ""}">${st === "injected" ? "⚡ " : ""}${esc(n.id)}</text><title>${esc(n.id)}: ${st}</title></g>`;
  }
  $("#topology").innerHTML = svg + "</svg>";
  renderBlast(blast);
}

/** Render the blast-radius verdict and the expected, unexpected and healthy paths. */
function renderBlast(blast) {
  if (!blast) { $("#blast").innerHTML = `<p class="muted">Arrow thickness = observed calls (Jaeger, last hour); dashed = static map only. Run an experiment to colour its blast radius.</p>`; return; }
  const label = (ep) => esc(status.endpoints[ep]?.label || ep);
  $("#blast").innerHTML = `
    <div class="verdict" style="color:${blast.contained ? "var(--ok)" : "var(--bad)"}">${blast.contained ? "✓ Contained: only paths that depend on" : "✕ Spread beyond paths that depend on"} <b>${esc(blast.target)}</b></div>
    <h4>Impacted paths (${Object.keys(blast.impacted_endpoints).length})</h4>
    <ul>${Object.entries(blast.impacted_endpoints).map(([k, v]) => `<li><b>${label(k)}</b> — ${esc(v.join("; "))}${blast.unexpected_endpoints.includes(k) ? ' <span style="color:var(--bad)">(unexpected)</span>' : ""}</li>`).join("") || "<li>none</li>"}</ul>
    <h4>Depend on ${esc(blast.target)} (expected)</h4><ul><li>${blast.expected_endpoints.map(label).join(", ") || "none"}</li></ul>
    <h4>Stayed healthy</h4><ul><li>${blast.healthy_endpoints.map(label).join(", ")}</li></ul>
    <h4>Upstream callers (potential radius)</h4><ul><li>${esc(blast.potential_services.join(", ") || "none")}</li></ul>`;
}

// ================================================================ evidence
/** Render the evidence tab: trace, metrics, logs or per-path baseline vs fault stats. */
function renderEvidence() {
  const rec = live.record;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === evTab));
  if (!rec) return;
  let html = "";
  if (evTab === "trace") {
    html = (rec.traces || []).map((tr) => tr.found ? `<div class="muted">${esc(status.endpoints[tr.endpoint]?.label)} failed · trace <code>${esc(tr.trace_id)}</code> · ${tr.span_count} spans · ${esc(tr.services.join(", "))}</div>
      <ul class="chain">${tr.error_chain.map((s) => `<li><span class="svc">${esc(s.service)}</span> <span class="op">${esc(s.operation)} · ${s.duration_ms} ms${s.http_status ? ` · HTTP ${esc(s.http_status)}` : ""}${s.rpc_code != null ? ` · gRPC ${esc(s.rpc_code)}` : ""}</span>${s.message ? `<div class="msg">${esc(s.message)}</div>` : ""}</li>`).join("") || '<li class="muted">No span marked as error: the request timed out without an error status.</li>'}</ul>`
      : `<p class="muted">${esc(tr.endpoint)}: trace ${esc(tr.trace_id)} not found in Jaeger.</p>`).join("") || `<p class="muted">No failed request was traced.</p>`;
  } else if (evTab === "spans") {
    html = `<div class="muted">Prometheus span metrics (OpenTelemetry Collector) during the run</div><table><tr><th>Service</th><th class="num">Calls</th><th class="num">Errors</th></tr>` +
      Object.entries(rec.span_errors || {}).sort((a, b) => b[1].errors - a[1].errors || b[1].calls - a[1].calls).map(([s, v]) => `<tr><td>${esc(s)}</td><td class="num">${v.calls}</td><td class="num">${v.errors ? `<span class="tag err">${v.errors}</span>` : 0}</td></tr>`).join("") + "</table>" +
      ((rec.span_detail || []).length ? `<h4>Error spans</h4><table>${rec.span_detail.map((m) => `<tr><td>${esc(m.service)}</td><td>${esc(m.span)}</td><td class="num"><span class="tag err">${m.errors}</span></td></tr>`).join("")}</table>` : "");
  } else if (evTab === "logs") {
    html = Object.entries(rec.logs || {}).map(([svc, rows]) => `<h4>${esc(svc)}</h4>` + (rows.length
      ? `<table>${rows.map((r) => `<tr><td><span class="tag err">${esc(r.level)}</span></td><td>${esc(r.message)}</td><td class="num">×${r.count}</td></tr>`).join("")}</table>` : `<p class="muted">No warnings or errors.</p>`)).join("");
  } else if (evTab === "containers") {
    html = `<table><tr><th>Service</th><th>Status</th><th>Health</th><th>Restarts</th><th>OOM</th><th>Mem limit</th><th>Started</th></tr>` +
      Object.values(rec.containers || {}).map((c) => `<tr><td>${esc(c.service)}</td><td>${esc(c.status)}</td><td>${esc(c.health ?? "–")}</td><td>${esc(c.restart_count)}</td><td>${c.oom_killed ? '<span class="tag err">yes</span>' : "no"}</td><td>${esc(c.memory_limit_mb ?? "–")} MB</td><td>${esc((c.started_at || "").slice(11, 19))}</td></tr>`).join("") + "</table>" +
      `<p class="muted">Restore actions: ${esc((rec.restore_results || []).join("; "))}. Flags clean: ${rec.flags_clean}. Faults left active: ${rec.active_faults_left}.</p>`;
  } else if (evTab === "network") {
    html = networkEvidenceHtml(rec.network);
  } else {
    const s = rec.endpoint_stats;
    html = `<table><tr><th>Path</th><th>Baseline</th><th>Under fault</th><th>Recovery</th><th>Fault errors</th></tr>` + Object.keys(s.baseline).map((ep) => {
      const c = (p) => s[p][ep]?.n ? `${s[p][ep].success}% · p95 ${s[p][ep].p95} ms` : "–";
      return `<tr><td>${esc(status.endpoints[ep].label)}</td><td>${c("baseline")}</td><td>${c("fault")}</td><td>${c("recovery")}</td><td>${esc(Object.entries(s.fault[ep]?.errors || {}).map(([k, v]) => `${k} ×${v}`).join(", "))}</td></tr>`;
    }).join("") + "</table>";
  }
  $("#evidence").innerHTML = html;
}

// ==================================================================== feed
/** Bytes as a short human string (kB / MB / GB). */
const bytes = (n) => n == null ? "–" : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`;

/** Fetch the sandbox network (subnet, container IPs, ports, traffic, active fault rules) and render it. */
async function renderNetwork(fresh = false) {
  let net;
  try {
    net = await (await fetch(`/api/network${fresh ? "?fresh=1" : ""}`)).json();
  } catch { net = { error: "network view unavailable" }; }
  if (!net || net.error) { $("#net-summary").innerHTML = `<p class="muted">${esc(net?.error || "Network view unavailable (is Docker running?)")}</p>`; $("#net-table").innerHTML = ""; return; }
  const rules = net.active_rules || [];
  $("#net-summary").innerHTML = `<dl><dt>Network</dt><dd>${esc(net.network)} (${esc(net.driver)})</dd>
    <dt>Subnet</dt><dd>${esc(net.subnet)}</dd><dt>Gateway</dt><dd>${esc(net.gateway)}</dd>
    <dt>Containers</dt><dd>${net.containers.length}</dd><dt>Read at</dt><dd>${esc(net.read_at || "")}</dd></dl>
    <h4 class="sub-h">Network faults active now</h4>${rules.length ? rules.map((r) => `<span class="rule">${esc(r.service)}: ${esc(r.mechanism)}${r.peer ? ` → ${esc(r.peer)}` : ""}${r.dev ? ` on ${esc(r.dev)}` : ""}${r.iptables.length ? `<br>${r.iptables.map(esc).join("<br>")}` : ""}</span>`).join("") : '<p class="muted">None: every namespace is clean.</p>'}`;
  const faulted = new Set(rules.flatMap((r) => [r.service, r.peer]));
  $("#net-table").innerHTML = `<table><tr><th>Service</th><th>IP</th><th>MAC</th><th>Listening TCP ports</th><th class="num">Received</th><th class="num">Sent</th></tr>${net.containers.map((c) => `<tr><td>${faulted.has(c.service) ? '<span class="tag err">fault</span> ' : ""}${esc(c.service)}</td><td><code>${esc(c.ip)}</code></td><td><code>${esc(c.mac || "–")}</code></td><td><code>${esc((c.ports || []).join(", ") || "–")}</code></td><td class="num">${bytes(c.rx_bytes)}</td><td class="num">${bytes(c.tx_bytes)}</td></tr>`).join("")}</table>`;
}

/** A metric cell with the change from baseline (red when it got worse). */
function deltaCell(b, f, unit = "", higherIsWorse = true) {
  if (f == null) return "–";
  const worse = b != null && (higherIsWorse ? f > b * 1.2 + 0.01 : f < b * 0.8);
  return `<span class="${worse ? "delta-up" : ""}">${f}${unit}</span>`;
}

/** Network evidence of a run: verified rules, TCP behaviour baseline vs fault, per-hop network time. */
function networkEvidenceHtml(n) {
  if (!n) return `<p class="muted">No network evidence for this run (the fault's container could not be observed).</p>`;
  const b = n.baseline || {}, f = n.fault || {};
  const rows = [["Segments sent / s", "segments_out_per_s", ""], ["Retransmissions / s", "retransmits_per_s", ""],
    ["Retransmitted share", "retransmit_pct", " %"], ["SYN retransmissions", "syn_retransmits", ""],
    ["Retransmission timeouts", "timeouts", ""], ["Failed connection attempts", "connect_failures", ""],
    ["Resets sent", "resets_sent", ""], ["New connections", "new_connections", ""],
    ["Median RTT of open connections", "rtt_p50_ms", " ms"], ["Max RTT", "rtt_max_ms", " ms"]];
  const rules = n.rules ? [...(n.rules.qdisc || []), ...(n.rules.iptables || [])] : [];
  return `<div class="muted">TCP counters (<code>nstat</code>) and sockets (<code>ss</code>) read inside <b>${esc(n.service)}</b>'s network namespace</div>
    ${rules.length ? `<h4>Rules verified while the fault was active</h4>${rules.map((r) => `<span class="rule">${esc(r)}</span>`).join("")}` : ""}
    <table><tr><th>TCP</th><th class="num">Baseline (${esc(b.seconds ?? "–")} s)</th><th class="num">Under fault (${esc(f.seconds ?? "–")} s)</th></tr>
    ${rows.map(([label, key, unit]) => `<tr><td>${label}</td><td class="num">${b[key] ?? "–"}${b[key] != null ? unit : ""}</td><td class="num">${deltaCell(b[key], f[key], unit)}</td></tr>`).join("")}</table>
    <h4>Per-hop time from traces: network + transport vs inside the service</h4>
    ${(n.hops || []).length ? `<table><tr><th>Hop</th><th class="num">Network, baseline</th><th class="num">Network, fault</th><th class="num">Server, baseline</th><th class="num">Server, fault</th></tr>${n.hops.map((h) => `<tr><td><code>${esc(h.edge)}</code></td><td class="num">${h.baseline_network_ms ?? "–"} ms</td><td class="num">${deltaCell(h.baseline_network_ms, h.fault_network_ms, " ms")}</td><td class="num">${h.baseline_server_ms ?? "–"} ms</td><td class="num">${deltaCell(h.baseline_server_ms, h.fault_server_ms, " ms")}</td></tr>`).join("")}</table>
      <p class="muted">Network time = client span minus server span for the same call: time on the wire plus the client and server transport stacks.</p>` : '<p class="muted">No traced hop touched this service (or calls failed before reaching it).</p>'}`;
}

/** Render the agent activity feed with model call count and time. */
function renderFeed() {
  if (!feed.length) { $("#feed").innerHTML = `<p class="muted">Agent steps stream here.</p>`; return; }
  const calls = feed.filter((f) => f.kind === "llm_call");
  const secs = calls.reduce((a, c) => a + (c.seconds || 0), 0);
  $("#feed").innerHTML = `<div class="muted small" style="margin:0">${calls.length} model calls · ${Math.round(secs)} s model time</div>` + feed.map((s) => {
    const who = esc(s.agent || "agent");
    switch (s.kind) {
      case "start": return `<div class="step ${who}"><div class="k">${who}</div>Started LangGraph ${who} agent · <b>${esc(s.provider)}</b> ${esc(s.model)}</div>`;
      case "plan": return `<div class="step"><div class="k">${who} · plan</div><pre>${esc(s.text)}</pre></div>`;
      case "tool_call": return `<div class="step call"><div class="k">${who} → tool</div><code>${esc(s.tool)}(${esc(JSON.stringify(s.args))})</code></div>`;
      case "tool_result": return `<div class="step result"><div class="k">${esc(s.tool)} result</div><pre>${esc(s.text)}</pre></div>`;
      case "thought": return `<div class="step ${who}"><div class="k">${who} · findings</div>${esc(s.text)}</div>`;
      case "report_ready": return `<div class="step"><div class="k">advisor</div>Report: ${esc(s.text)}</div>`;
      case "fix": return `<div class="step call"><div class="k">fixer</div>Proposed fix: ${esc(s.text)}</div>`;
      case "validation": return `<div class="step ${s.ok ? "" : "error"}"><div class="k">validator</div>${s.ok ? "✓ Fix validated: " : "✕ Rejected, sending back to fixer: "}${esc(s.text)}</div>`;
      case "llm_retry": return `<div class="step"><div class="k">${who} · model retry</div>${esc(s.text)}</div>`;
      case "llm_call": return "";
      case "error": return `<div class="step error"><div class="k">${who} error</div>${esc(s.text)}</div>`;
      default: return "";
    }
  }).join("");
  $("#feed").scrollTop = $("#feed").scrollHeight;
}

// ================================================================ findings
/** Render a unified diff with added and removed lines coloured. */
function diffHtml(diff) {
  return `<div class="diff">${diff.split("\n").map((l) => `<div class="${l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l) || " "}</div>`).join("")}</div>`;
}

/**
 * Render the AI analysis: summary, root cause, recommendations, proposed code fix with validation,
 * and auto-fix controls.
 */
function renderFindings() {
  if (!findings) return;
  const a = findings.analysis, run = findings.run;
  if (!a || !a.report) { $("#report").innerHTML = `<p class="muted">No analysis for this run yet. <button data-analyse="${esc(run?.run_id)}" data-needs-idle>Run AI analysis</button></p>`; return; }
  const r = a.report, rv = a.review || {}, fix = a.fix, v = fix?.validation || {};
  const verdictCls = r.hypothesis_verdict.startsWith("partially") ? "partially" : r.hypothesis_verdict;
  const deployed = fix && Object.keys(status?.active_fixes || {}).includes(fix.file);
  let html = `
    <div class="report-head"><h3>${esc(r.headline)}</h3>
      <span class="badge ${verdictCls}">Hypothesis ${esc(r.hypothesis_verdict)}</span><span class="badge">Confidence: ${esc(r.confidence)}</span></div>
    <p class="muted" style="margin-top:-6px">${esc(r.verdict_reason)} <span class="pill">${esc(run.title)} · ${esc(run.target)} · ${esc(run.run_id)}</span></p>
    <div class="facts">
      <div class="fact"><h4>Root cause</h4><p>${esc(r.root_cause)}</p></div>
      <div class="fact"><h4>Customer impact</h4><p>${esc(r.customer_impact)}</p></div>
      <div class="fact"><h4>Blast radius</h4><p>${esc(r.blast_radius)}</p></div>
    </div>
    <div class="ev"><h4>Weaknesses found (ranked)</h4><ul class="findings-list">${(r.findings || []).map((f) => `<li><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span> <b>${esc(f.title)}</b> — ${esc(f.detail)}</li>`).join("")}</ul></div>
    <div class="ev"><h4>Evidence</h4><ul>${r.evidence.map((e) => `<li>${(rv.tools_called || []).includes(e.source) ? "✅" : "⚠️"} ${esc(e.claim)} <span class="src">— ${esc(e.source)}</span></li>`).join("")}</ul></div>
    <h4 class="sub-h" style="margin-top:0">Recommendations</h4>
    <div class="recs">${r.recommendations.map((x) => `<div class="rec"><span class="prio ${esc(x.priority)}">${esc(x.priority)}</span> <span class="comp">${esc(x.component)}</span>
      <div class="title">${esc(x.title)}</div><dl><dt>Problem</dt><dd>${esc(x.problem)}</dd><dt>Change</dt><dd>${esc(x.change)}</dd><dt>Verify by re-running</dt><dd>${esc(x.verification)}</dd></dl></div>`).join("")}</div>`;
  if (fix) {
    html += `<div class="fixcard"><h3>Suggested code fix: ${esc(fix.title)}</h3>
      <div class="meta"><span class="pill">${esc(fix.file)}</span><span class="pill ${v.ok ? "ok" : "bad"}">${v.ok ? `✓ ${esc(v.syntax?.tool || "validated")}: ${esc(v.syntax?.output)}` : `✕ ${esc(v.error)}`}</span>
        <span class="pill">${esc(v.deploy || "")}</span><span class="pill">attempts: ${rv.fix_attempts ?? "?"}</span>${deployed ? '<span class="pill ok">deployed</span>' : ""}</div>
      <p>${esc(fix.explanation)}</p><p class="muted"><b>Expected effect:</b> ${esc(fix.expected_effect)}</p>
      ${v.diff ? diffHtml(v.diff) : ""}
      <div class="row">${v.ok && v.hot_patchable ? `<button class="primary" data-autofix="${esc(run.run_id)}" data-needs-idle ${deployed ? "disabled" : ""}>Apply auto-fix &amp; re-run experiment</button>` : ""}
        ${deployed ? `<button data-rollback="${esc(fix.file.split("/")[0])}" data-needs-idle>Roll back fix</button>` : ""}
        ${v.ok && !v.hot_patchable ? `<span class="muted">Compiled service: patch saved to artifacts/demo-runs/${esc(run.run_id)}.fix.patch for an image rebuild.</span>` : ""}</div>
      ${findings.autofix && !findings.comparison ? `<p class="muted">Auto-fix: ${esc(findings.autofix.stage)} ${esc(findings.autofix.message || "")}</p>` : ""}
      ${compareHtml(findings.comparison)}</div>`;
  }
  html += `<div class="review">Reviewer: ${rv.evidence_grounded ?? "?"}/${rv.evidence_total ?? "?"} evidence items cite tools the agent actually called (${esc((rv.tools_called || []).join(", "))}).
    ${rv.percentages_unmatched?.length ? `Percentages without a matching measurement: ${esc(rv.percentages_unmatched.join(", "))}%.` : "All quoted percentages match measured values."} Model: ${esc(a.provider)} · ${esc(a.model)}.
    <a href="/api/report.md?run=${esc(run.run_id)}">Download this run's findings (.md)</a></div>`;
  $("#report").innerHTML = html;
  setBusy(busy);
}

/** Render the before/after comparison of an auto-fix re-run. */
function compareHtml(c) {
  if (!c) return "";
  const b = c.before, a = c.after;
  const rowv = (label, bv, av, better) => {
    const cls = bv === av || bv == null || av == null ? "" : (better(av, bv) ? "better" : "worse");
    return `<tr><td>${label}</td><td>${fmt(bv)}</td><td class="${cls}">${fmt(av)}</td></tr>`;
  };
  const hi = (x, y) => x > y, lo = (x, y) => x < y;
  return `<h4 class="sub-h">Before vs after the fix (same experiment re-run)</h4>
    <table class="compare"><tr><th>Metric</th><th>Before (${esc(c.before_run)})</th><th>After (${esc(c.after_run)})</th></tr>
    ${rowv("Resilience score", b.score, a.score, hi)}${rowv("Path availability under fault %", b.parts?.availability, a.parts?.availability, hi)}
    ${rowv("Error handling score", b.parts?.error_handling, a.parts?.error_handling, hi)}${rowv("Request success under fault %", b.fault_success, a.fault_success, hi)}
    ${rowv("Failures under fault", b.fault_failures, a.fault_failures, lo)}${rowv("Misleading 4xx (blames customer)", b.misleading_4xx, a.misleading_4xx, lo)}
    ${rowv("Timeouts", b.timeouts, a.timeouts, lo)}${rowv("Time to recover s", b.time_to_recover_s, a.time_to_recover_s, lo)}
    <tr><td>Failure status mix</td><td>${esc(JSON.stringify(b.status_mix))}</td><td>${esc(JSON.stringify(a.status_mix))}</td></tr></table>
    <p><b>Score change: ${c.score_delta >= 0 ? "+" : ""}${c.score_delta}</b></p>`;
}

// ================================================================ campaign
/** Render campaign progress and, when finished, its overall report. */
function renderCampaign() {
  if (!campaign) return;
  const exps = campaign.experiments || [];
  let html = `<ol>${exps.map((e, i) => `<li>${esc(e.title || e.scenario)} on ${esc(e.target)} (${esc(e.variant)}) — ${i < (campaign.index ?? 0) || campaign.report ? "done" : i === campaign.index ? "<b>running</b>" : "pending"}</li>`).join("")}</ol>`;
  const rep = campaign.report;
  if (rep) {
    const cr = rep.report, score = rep.overall_score;
    html = `<div class="gauge"><div class="big" style="color:${score >= 75 ? "var(--ok)" : score >= 60 ? "var(--warn)" : "var(--bad)"}">${score}<small style="font-size:18px">/100</small></div>
      <div><b>Overall resilience score</b><div class="muted">mean of ${rep.runs?.length || exps.length} experiments · ${esc(rep.provider || "")} ${esc(rep.model || "")}</div></div></div>` + (cr ? `
      <p>${esc(cr.executive_summary)}</p><p class="muted">${esc(cr.score_explanation)}</p>
      <table><tr><th>#</th><th>Severity</th><th>Weakness</th><th>Services</th><th>Impact</th><th>Recommended fix</th></tr>
      ${cr.weaknesses.map((w) => `<tr><td>${w.rank}</td><td><span class="sev ${esc(w.severity)}">${esc(w.severity)}</span></td><td><b>${esc(w.title)}</b></td><td>${esc(w.services.join(", "))}</td><td>${esc(w.impact)}</td><td>${esc(w.fix)}</td></tr>`).join("")}</table>
      <p class="muted">Next experiments: ${esc(cr.next_experiments.join("; "))}</p>` : "") + html;
    if (campaign.id) $("#report-dl").href = `/api/report.md?campaign=${campaign.id}`;
  }
  $("#campaign-out").innerHTML = html;
}

/** Render the table of recorded runs with score and Open/Analyse buttons. */
function renderHistory() {
  const runs = status?.runs || [];
  $("#history").innerHTML = runs.length ? `<table><tr><th>When</th><th>Experiment</th><th>Target</th><th>Status</th><th>Score</th><th></th></tr>${runs.map((r) => `
    <tr><td>${esc(new Date(r.started_at).toLocaleTimeString())}</td><td>${esc(r.title)} <span class="muted">${esc(r.variant)}</span>${r.label ? ` <span class="pill ai">${esc(r.label)}</span>` : ""}</td>
    <td>${esc(r.target)}</td><td>${esc(r.status)}</td><td><span class="scorebar" style="width:${r.score * 0.8}px"></span> ${r.score} ${esc(r.grade)}</td>
    <td><button data-open="${esc(r.run_id)}">Open</button>${r.analysed ? "" : ` <button data-analyse="${esc(r.run_id)}" data-needs-idle>Analyse</button>`}</td></tr>`).join("")}</table>` : `<p class="muted">No runs yet.</p>`;
  if (!campaign?.id && runs.length && !$("#report-dl").dataset.set) $("#report-dl").href = `/api/report.md?run=${runs[0].run_id}`;
  const cs = status?.campaigns || [];
  if (cs.length && !campaign) loadCampaign(cs[0].id);
  setBusy(busy);
}

/** Load a saved campaign and render it. */
async function loadCampaign(id) {
  const doc = await (await fetch(`/api/campaigns/${id}`)).json();
  campaign = { id, experiments: doc.experiments, index: doc.runs.length, report: doc.report };
  renderCampaign();
}

/** Load a recorded run into every panel. */
async function openRun(id) {
  const doc = await (await fetch(`/api/runs/${id}`)).json();
  live = freshLive();
  live.run = doc.run; live.record = doc.run; live.records = doc.run.requests || []; live.timeline = doc.run.timeline || {};
  live.guard = doc.run.status === "aborted" ? doc.run.abort_reason : null;
  findings = { run: doc.run, analysis: doc.analysis, comparison: doc.comparison };
  $("#hypothesis").textContent = doc.run.hypothesis;
  $("#report-dl").href = `/api/report.md?run=${id}`; $("#report-dl").dataset.set = "1";
  line(`Showing recorded run ${id} (${doc.run.status})`);
  renderMeta(); renderLive(); renderEvidence(); renderTopology(); renderFindings();
  location.hash = "#live";
}

// ================================================================= actions
$("#scenario").addEventListener("change", (e) => pickScenario(e.target.value));
$("#target").addEventListener("change", () => loadShopView());
$("#variant").addEventListener("change", () => {});
$("#presenter").addEventListener("change", () => { if ($("#presenter").checked) applyPresenterDefaults(); renderShopView(); });
$("#end-fault").addEventListener("click", async () => { $("#end-fault").disabled = true; await post("/api/end-fault", {}); });
$("#target").addEventListener("change", () => { if (!live.run) $("#hypothesis").textContent = status.scenarios[$("#scenario").value].hypothesis.replaceAll("{target}", $("#target").value); });
$("#net-refresh").addEventListener("click", () => renderNetwork(true));
$("#tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) { evTab = e.target.dataset.tab; renderEvidence(); } });
$("#run").addEventListener("click", () => {
  startJob("/api/run", designerParams());
});
$("#plan-btn").addEventListener("click", () => { feed = []; renderFeed(); $("#plan-out").innerHTML = `<p class="muted">Planner agent is reading the dependency map…</p>`; startJob("/api/plan", {}, { reset: false }); });
$("#campaign-btn").addEventListener("click", () => {
  const exps = plan.experiments.slice(0, 5).map((e) => ({ scenario: e.scenario, target: e.target, variant: e.variant, fault_s: Math.min(e.fault_s, 25), title: e.title }));
  campaign = { experiments: exps, index: 0 }; renderCampaign();
  startJob("/api/campaign", { experiments: exps });
  location.hash = "#live";
});
$("#kill").addEventListener("click", async () => {
  const res = await post("/api/abort", {});
  if (res) line(`Kill switch: ${res.restored.length ? res.restored.join("; ") : "nothing was active"} · flags clean: ${res.flags_clean}`, "warn");
  loadStatus();
});
document.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.load) loadProposal(plan.experiments[+t.dataset.load]);
  if (t.dataset.run) { const p = plan.experiments[+t.dataset.run]; loadProposal(p); startJob("/api/run", { ...designerParams(), proposed_by: "planner" }); location.hash = "#live"; }
  if (t.dataset.open) openRun(t.dataset.open);
  if (t.dataset.analyse) { await openRun(t.dataset.analyse); feed = []; startJob("/api/analyse", { run_id: t.dataset.analyse }, { reset: false }); }
  if (t.dataset.autofix) { feed = []; renderFeed(); findings.comparison = null; findings.autofix = { stage: "starting" }; renderFindings(); startJob("/api/autofix", { run_id: t.dataset.autofix }); }
  if (t.dataset.rollback) { t.disabled = true; line(`Rolling back fix on ${t.dataset.rollback}…`); const r = await post("/api/rollback", { service: t.dataset.rollback }); if (r) line(`Fix rolled back: ${r.service} recreated from the original image`); await loadStatus(); renderFindings(); }
});

// Hero, copy-command and keyboard shortcuts (P plan, R open the designer, Shift+K kill switch).
$("#hero-plan").addEventListener("click", () => { location.hash = "#plan"; $("#plan-btn").click(); });
$("#hero-run").addEventListener("click", () => { location.hash = "#experiment"; $("#scenario").focus({ preventScroll: true }); });
$("#copy-cmd").addEventListener("click", async () => {
  const label = $("#copy-cmd span"), text = label.textContent;
  try { await navigator.clipboard.writeText(text); label.textContent = "copied ✓"; } catch { label.textContent = "copy failed"; }
  setTimeout(() => (label.textContent = text), 1400);
});
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  if (e.key === "K" && e.shiftKey) { e.preventDefault(); $("#kill").click(); }
  else if (e.key.toLowerCase() === "p" && !e.shiftKey && !$("#plan-btn").disabled) { e.preventDefault(); $("#hero-plan").click(); }
  else if (e.key.toLowerCase() === "r" && !e.shiftKey && !$("#run").disabled) { e.preventDefault(); $("#hero-run").click(); }
});

(async () => {
  await loadStatus();
  const p = await (await fetch("/api/plan")).json();
  if (p.experiments) { plan = p; renderPlan(); }
  renderLive(); renderTopology(); renderNetwork();
  if (status.busy) connect();
  setInterval(() => { if (!busy) loadStatus(); }, 15000);
})();
