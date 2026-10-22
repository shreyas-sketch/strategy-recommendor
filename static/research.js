// Strategy research pages (admin only). Uses the admin session cookie, or the ADMIN_TOKEN kept by /admin.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const token = (() => { try { return sessionStorage.getItem("sr_admin") || ""; } catch { return ""; } })();
const headers = (extra = {}) => ({ "X-Requested-With": "fetch", ...(token ? { "X-Admin-Token": token } : {}), ...extra });
const API = "/api/admin/research";

async function api(path, opts = {}) {
  const h = headers(opts.json !== undefined ? { "Content-Type": "application/json" } : {});
  const res = await fetch(API + path, { ...opts, headers: h, credentials: "same-origin",
    body: opts.json !== undefined ? JSON.stringify(opts.json) : opts.body });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) { $("signin").classList.remove("hidden"); $("panel").classList.add("hidden"); throw new Error("Admin sign-in required"); }
  if (!res.ok) { const d = body.detail; throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || res.statusText); }
  return body;
}
function showError(e) { $("error").textContent = e.message || String(e); $("error").classList.remove("hidden"); window.scrollTo(0, 0); }
function clearError() { $("error").classList.add("hidden"); }

// ---- formatting ---------------------------------------------------------
const num = (x, d = 0) => (x === null || x === undefined || Number.isNaN(x) ? "—" : Number(x).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d }));
const rs = (x, d = 0) => (x === null || x === undefined ? "—" : (x < 0 ? "-₹" : "₹") + num(Math.abs(x), d));
const pct = (x, d = 0) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`);
const cls = (x) => (x > 0 ? "pos" : x < 0 ? "neg" : "");
const pill = (text, kind) => `<span class="pill ${kind}">${esc(text)}</span>`;
const table = (head, rows, empty = "Nothing yet.") => `<tr>${head.map((h) => `<th${h.num ? ' class="num"' : ""}>${esc(h.t ?? h)}</th>`).join("")}</tr>` +
  (rows.length ? rows.join("") : `<tr><td colspan="${head.length}" class="muted">${esc(empty)}</td></tr>`);
const H = (t) => ({ t, num: true });

// Inline SVG line chart: series = {name: [values]}, x = labels (dates).
function lineChart(x, series, { height = 220 } = {}) {
  const names = Object.keys(series).filter((k) => series[k].length);
  if (!names.length || x.length < 2) return '<p class="muted small">Not enough data to draw.</p>';
  const W = 900, Hh = height, P = { l: 64, r: 10, t: 10, b: 24 };
  const all = names.flatMap((k) => series[k]).filter((v) => v !== null);
  let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  if (hi === lo) hi = lo + 1;
  const sx = (i) => P.l + (i / (x.length - 1)) * (W - P.l - P.r);
  const sy = (v) => P.t + (1 - (v - lo) / (hi - lo)) * (Hh - P.t - P.b);
  const lines = names.map((k, n) => {
    const pts = series[k].map((v, i) => (v === null ? null : `${sx(i).toFixed(1)},${sy(v).toFixed(1)}`)).filter(Boolean).join(" ");
    return `<polyline class="s${n % 8}" fill="none" stroke-width="${n === 0 ? 2.2 : 1.4}" points="${pts}"><title>${esc(k)}</title></polyline>`;
  }).join("");
  const ticks = [lo, (lo + hi) / 2, hi].map((v) => `<text x="${P.l - 6}" y="${sy(v) + 4}" text-anchor="end" class="axis">${esc(rs(v))}</text>`).join("");
  const zero = `<line x1="${P.l}" x2="${W - P.r}" y1="${sy(0)}" y2="${sy(0)}" class="zero"/>`;
  const xl = [0, Math.floor((x.length - 1) / 2), x.length - 1].map((i) => `<text x="${sx(i)}" y="${Hh - 6}" text-anchor="${i === 0 ? "start" : i === x.length - 1 ? "end" : "middle"}" class="axis">${esc(x[i])}</text>`).join("");
  const legend = names.map((k, n) => `<span class="lg"><i class="s${n % 8}"></i>${esc(k)}</span>`).join("");
  return `<svg viewBox="0 0 ${W} ${Hh}" class="chart" role="img" preserveAspectRatio="none">${zero}${lines}${ticks}${xl}</svg><div class="legend">${legend}</div>`;
}

// ---- tabs ---------------------------------------------------------------
const loaders = {};
let current = "dashboard";
$("tabs").addEventListener("click", (e) => {
  const t = e.target.dataset.tab; if (!t) return;
  current = t;
  [...$("tabs").children].forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  document.querySelectorAll("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== t));
  try { sessionStorage.setItem("sr_research_tab", t); } catch {}
  load(t);
});
async function load(t) {
  clearError();
  try { await loaders[t](); } catch (e) { if (e.message !== "Admin sign-in required") showError(e); }
}

let registry = [];
let symbols = ["NIFTY50", "SENSEX", "BANKNIFTY"];
const nameOf = (id) => (registry.find((s) => s.id === id) || {}).name || id;

// ---- dashboard -----------------------------------------------------------
loaders.dashboard = async () => {
  const s = await api("/status");
  registry = s.strategies;
  const rec = s.latest_recommendation;
  const stat = (k, v, sub = "") => `<div class="stat"><div class="k">${esc(k)}</div><div class="v nocap">${v}</div>${sub ? `<div class="small muted">${sub}</div>` : ""}</div>`;
  const range = s.market.length ? s.market.map((m) => `${esc(m.symbol)} ${esc(m.interval)}: ${esc(m.first_day)} → ${esc(m.last_day)}`).join("<br>") : "—";
  $("dash-stats").innerHTML = [
    stat("Latest data update", s.last_update ? esc(new Date(s.last_update).toLocaleString()) : "never"),
    stat("Market data range", `<span class="small">${range}</span>`),
    stat("Usable market records", num(s.usable_market_records)),
    stat("Strategy records (days)", num(s.strategy_records)),
    stat("Analysis status", esc(s.analysis_status), `Engine: ${esc(s.engine_label)}`),
    stat("Today's published pick", s.published_today ? (s.published_today.top_strategy ? esc(nameOf(s.published_today.top_strategy)) : "No strategy") : "not yet",
         s.publish_enabled ? (s.published_today ? `published ${esc(s.published_today.at.slice(11, 16))} IST` : `publishes at ${esc(s.publish_time)} IST`) : "daily publishing is off"),
    stat("Latest recommendation", rec ? `${esc(rec.target_day)}` : "none",
         rec ? (rec.top_strategy ? esc(nameOf(rec.top_strategy)) : "No strategy met the criteria") : ""),
  ].join("");
  $("dash-warnings").innerHTML = s.warnings.map((w) => `<li>${esc(w)}</li>`).join("");
  $("dash-market").innerHTML = table(["Index", "Interval", H("Bars"), H("Sessions"), "From", "To"],
    s.market.map((m) => `<tr><td>${esc(m.symbol)}</td><td>${esc(m.interval)}</td><td class="num">${num(m.bars)}</td><td class="num">${num(m.days)}</td><td>${esc(m.first_day)}</td><td>${esc(m.last_day)}</td></tr>`), "No market data stored.");
  $("dash-strategies").innerHTML = table(["Strategy", "Index", H("Margin"), H("Days stored"), "From", "To", "Status"],
    s.strategies.map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.underlying)}</td><td class="num">${rs(x.margin)}</td><td class="num">${num(x.days)}</td>
      <td>${esc(x.first_day || "—")}</td><td>${esc(x.last_day || "—")}</td><td>${x.active ? (x.days ? pill("active", "recommended") : pill("no P&L yet", "conditional")) : pill("inactive", "avoid")}</td></tr>`));
};

// ---- recommendation -------------------------------------------------------------
function renderRec(r) {
  if (!r) { $("rec-body").innerHTML = '<div class="card muted">No recommendation yet. Upload data, or press “Recompute now”.</div>'; $("rec-meta").textContent = ""; return; }
  const p = r.payload || r;
  $("rec-meta").textContent = `For ${p.target_day} · data up to the ${p.cutoff_day} close · ${p.engine_label} · ${p.model_version}`;
  if (p.locked) $("rec-meta").textContent += ` · ${p.locked}`;
  const top = p.ranking.find((x) => x.id === p.top_strategy);
  const head = top ? `<div class="card top">
      <div class="muted small">Recommended for ${esc(p.target_day)}</div>
      <div class="name">${esc(top.name)}</div>
      <div class="grid">
        <div class="stat"><div class="k">Expected net (its margin ${rs(top.margin)})</div><div class="v nocap">${rs(top.expected_rs)}</div><div class="small muted">80% range ${rs(top.low_rs)} to ${rs(top.high_rs)}</div></div>
        <div class="stat"><div class="k">Per ₹1L of margin</div><div class="v nocap">${rs(top.expected)}</div><div class="small muted">score ${rs(top.score)}</div></div>
        <div class="stat"><div class="k">Profitable-day probability (model estimate)</div><div class="v nocap">${top.p_win != null ? pct(top.p_win) : "—"}</div></div>
        <div class="stat"><div class="k">Bad day (worst 5% avg)</div><div class="v nocap">${rs(top.cvar5_rs)}</div><div class="small muted">1 in 20 days worse than ${rs(top.var5_rs)}</div></div>
        <div class="stat"><div class="k">Evidence</div><div class="v nocap">${num(top.regime_days)} / ${num(top.n_days)}</div><div class="small muted">days in this regime / all days</div></div>
      </div>
      ${p.also_qualify.length ? `<p class="small">Also qualifies: ${p.also_qualify.map((id) => esc(nameOf(id))).join(", ")}</p>` : ""}
    </div>` : `<div class="card top notrade"><div class="muted small">${esc(p.target_day)}</div><div class="name">No strategy meets the current selection criteria</div>
      <p class="small muted">The tool does not force a pick when the evidence does not support one.</p></div>`;
  const warn = p.warnings.length ? `<div class="notice" style="margin-bottom:16px"><b>Read before acting:</b><ul class="reasons">${p.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
  const markets = p.markets.map((m) => `<div class="card"><h2>${esc(m.symbol)} at the ${esc(m.data_day)} close: ${esc(m.regime)}, ${esc(m.direction)}</h2>
      <div class="table-scroll"><table>${table(["Reading", H("Value")], m.readings.map((x) => `<tr><td>${esc(x.label)}</td><td class="num">${esc(x.shown)}</td></tr>`))}</table></div></div>`).join("");
  const rows = p.ranking.map((x) => `<div class="strat"><div class="head"><b>${x.rank ? `#${x.rank} ` : ""}${esc(x.name)}</b>
      ${x.status !== "ok" ? pill(x.status.replaceAll("_", " "), "avoid") : x.id === p.top_strategy ? pill("recommended", "recommended") : x.score > 0 ? pill("qualifies", "conditional") : pill("below threshold", "avoid")}</div>
      <div class="small muted">${esc(x.underlying)} · margin ${rs(x.margin)} · regime ${esc(x.regime)}${x.score != null ? ` · score ${rs(x.score)} per ₹1L` : ""}${x.p_win != null ? ` · profitable-day estimate ${pct(x.p_win)}` : ""}</div>
      <ul class="reasons">${(x.why || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`).join("");
  const others = Object.entries(p.other_engines || {}).map(([e, o]) => `<tr><td>${esc(o.label)}</td><td>${o.top_strategy ? esc(nameOf(o.top_strategy)) : '<span class="muted">no pick</span>'}</td></tr>`);
  $("rec-body").innerHTML = head + warn + `<div class="card"><h2>All strategies, ranked</h2>${rows}</div>` + markets +
    `<div class="card"><h2>What the other methods would pick</h2><p class="muted small">For comparison only; see History &amp; validation for how each method has done.</p><table>${table(["Method", "Pick"], others)}</table></div>` +
    `<p class="muted small">${esc(p.disclaimer)}</p>`;
}
loaders.recommendation = async () => {
  if (!registry.length) registry = (await api("/strategies")).strategies;
  renderRec((await api("/recommendation")).recommendation);
};
$("rec-publish").onclick = async (e) => {
  if (!confirm("Refresh daily candles, compute today's pick and publish it to participants now?")) return;
  e.target.disabled = true; clearError();
  try {
    const r = await api("/publish", { method: "POST" });
    if (!r.published) throw new Error(`Not published: ${r.reason}`);
    renderRec((await api("/recommendation")).recommendation);
  } catch (err) { showError(err); } finally { e.target.disabled = false; }
};
$("rec-run").onclick = async (e) => {
  e.target.disabled = true; clearError();
  try { renderRec((await api("/recommendation/run", { method: "POST" })).recommendation); } catch (err) { showError(err); }
  finally { e.target.disabled = false; }
};

// ---- performance ---------------------------------------------------------------------
loaders.performance = async () => {
  const q = new URLSearchParams();
  if ($("perf-start").value) q.set("start", $("perf-start").value);
  if ($("perf-end").value) q.set("end", $("perf-end").value);
  const [perf, rel] = await Promise.all([api(`/performance?${q}`), api("/relationships")]);
  const list = perf.strategies.filter((s) => s.stats);
  $("perf-table").innerHTML = table(["Strategy", H("Days"), H("Win rate (days)"), H("Gross P&L"), H("Costs"), H("Net P&L"), H("Net % of margin"),
    H("Avg win day"), H("Avg loss day"), H("Profit factor"), H("Max drawdown"), H("Sharpe"), H("Streaks W/L")],
    list.map((s) => { const t = s.stats; return `<tr><td>${esc(s.name)}<div class="small muted">${esc(s.first_day)} → ${esc(s.last_day)}</div></td>
      <td class="num">${num(t.days)}</td><td class="num">${pct(t.win_rate, 1)}</td><td class="num">${rs(t.gross_pnl)}</td><td class="num">${rs(-t.costs)}${t.cost_estimated_days ? "*" : ""}</td>
      <td class="num ${cls(t.net_pnl)}">${rs(t.net_pnl)}</td><td class="num">${num(t.net_return_pct_margin, 1)}%</td><td class="num">${rs(t.avg_win_day)}</td><td class="num">${rs(t.avg_loss_day)}</td>
      <td class="num">${num(t.profit_factor, 2)}</td><td class="num">${rs(t.max_drawdown)}<div class="small muted">${num(t.max_drawdown_pct_margin, 1)}% of margin</div></td>
      <td class="num">${num(t.sharpe, 2)}</td><td class="num">${t.longest_win_streak} / ${t.longest_loss_streak}</td></tr>`; }), perf.message || "No P&L in this period.") +
    (list.some((s) => s.stats.cost_estimated_days) ? `<tr><td colspan="13" class="small muted">* includes estimated costs for days without a charges column.</td></tr>` : "");
  // equity chart aligned on the union of dates
  const days = [...new Set(list.flatMap((s) => s.equity.map((p) => p[0])))].sort();
  const series = {};
  for (const s of list) {
    const m = new Map(s.equity); let last = null;
    series[s.name] = days.map((d) => { if (m.has(d)) last = m.get(d); return last; });
  }
  $("perf-chart").innerHTML = lineChart(days, series);
  $("perf-regimes").innerHTML = list.map((s) => `<h3 class="sub">${esc(s.name)} <span class="muted small">(regime of ${esc(s.underlying)}${s.by_regime && s.by_regime.length ? "" : ""})</span></h3>
    <div class="table-scroll"><table>${table(["Regime", H("Days"), H("Avg per ₹1L"), H("± s.e."), H("Profitable days"), H("Net P&L")],
      (s.by_regime || []).map((r) => `<tr><td>${esc(r.regime)}</td><td class="num">${r.days}</td><td class="num ${cls(r.mean_per_lakh)}">${rs(r.mean_per_lakh)}</td><td class="num">${rs(r.se)}</td><td class="num">${pct(r.win_rate)}</td><td class="num">${rs(r.net)}</td></tr>`)
      .concat((s.by_direction || []).map((r) => `<tr class="muted"><td>${esc(r.direction)} (trend direction)</td><td class="num">${r.days}</td><td class="num">${rs(r.mean_per_lakh)}</td><td class="num">${rs(r.se)}</td><td class="num">${pct(r.win_rate)}</td><td class="num">${rs(r.net)}</td></tr>`)))}</table></div>`).join("") || '<p class="muted">No data.</p>';
  $("rel-note").textContent = rel.note || rel.message || "";
  const relRows = () => (rel.tests || []).filter((t) => !$("rel-only").checked || t.evidence !== "none").map((t) => `<tr><td>${esc(t.strategy_name)}</td><td>${esc(t.feature_label)}<div class="small muted">${esc(t.symbol)}</div></td>
      <td class="num">${rs(t.low_mean)} <span class="small muted">(${t.low_n})</span></td><td class="num">${rs(t.high_mean)} <span class="small muted">(${t.high_n})</span></td>
      <td class="num ${cls(t.diff)}">${rs(t.diff)}</td><td class="num">${num(t.p, 3)}</td><td class="num">${num(t.q, 3)}</td>
      <td>${t.evidence === "supported" ? pill("supported", "recommended") : t.evidence === "suggestive" ? pill("suggestive", "conditional") : '<span class="muted small">none</span>'}</td></tr>`);
  const drawRel = () => { $("rel-table").innerHTML = table(["Strategy", "Previous-session feature", H("Bottom third: avg next day per ₹1L (n)"), H("Top third (n)"), H("Difference"), H("p"), H("q (adjusted)"), "Evidence"], relRows(),
      rel.count ? "No pattern passes the evidence filter — untick to see all tests." : "Not enough overlapping market and P&L data yet."); };
  $("rel-only").onchange = drawRel; drawRel();
  $("perf-breakdown").innerHTML = list.map((s) => `<h3 class="sub">${esc(s.name)}</h3><div class="table-scroll"><table>${table(["Month", ...s.by_month.map((m) => m.month)].map((h, i) => (i ? H(h) : h)),
      [`<tr><td>Net P&L</td>${s.by_month.map((m) => `<td class="num ${cls(m.net)}">${rs(m.net)}</td>`).join("")}</tr>`, `<tr><td>% of margin</td>${s.by_month.map((m) => `<td class="num">${num(m.pct_margin, 1)}%</td>`).join("")}</tr>`])}</table></div>
      <div class="table-scroll"><table>${table(["Weekday", H("Days"), H("Net P&L"), H("Profitable days")], s.by_weekday.map((w) => `<tr><td>${esc(w.weekday)}</td><td class="num">${w.days}</td><td class="num ${cls(w.net)}">${rs(w.net)}</td><td class="num">${pct(w.win_rate)}</td></tr>`))}</table></div>`).join("") || '<p class="muted">No data.</p>';
};
$("perf-apply").onclick = () => load("performance");

// ---- validation -----------------------------------------------------------------------
const LABELS = { B_regime_shrunk: "Regime-aware, shrunk (main)", A_best_so_far: "Best average so far", A_trailing60: "Best last-60-days average",
  B_regime_raw: "Best raw average in this regime", C_ridge: "Ridge regression on features", equal_weight: "Run all eligible strategies (equal weight)" };
loaders.validation = async () => {
  $("bt-note").textContent = "Running walk-forward validation…";
  const [bt, hist] = await Promise.all([api("/backtest"), api("/history")]);
  if (!bt.days) {
    $("bt-note").textContent = bt.message || "Nothing to validate yet."; $("bt-table").innerHTML = ""; $("bt-chart").innerHTML = "";
  } else {
    $("bt-note").textContent = `${bt.days} sessions evaluated, ${bt.first_day} → ${bt.last_day}. ${bt.note}`;
    const s = bt.summary;
    const order = ["B_regime_shrunk", "A_best_so_far", "A_trailing60", "B_regime_raw", "C_ridge", "equal_weight"];
    $("bt-table").innerHTML = table(["Method", H("Total per ₹1L"), H("Avg / day"), H("Days with a pick"), H("Profitable pick days"), H("Picked the best"), H("Pick in top 2"), H("Max drawdown"), H("Sharpe"), H("p vs random")],
      order.filter((k) => s[k]).map((k) => { const m = s[k]; return `<tr${k === "B_regime_shrunk" ? ' class="hl"' : ""}><td>${esc(LABELS[k])}</td><td class="num ${cls(m.total)}">${rs(m.total)}</td><td class="num">${rs(m.mean)}</td>
        <td class="num">${m.picked_days} / ${m.days}</td><td class="num">${pct(m.profitable_pick_rate)}</td><td class="num">${k === "equal_weight" ? "—" : pct(m.top1_hit_rate)}</td><td class="num">${k === "equal_weight" ? "—" : pct(m.top2_hit_rate)}</td>
        <td class="num">${rs(m.max_drawdown)}</td><td class="num">${num(m.sharpe, 2)}</td><td class="num">${m.p_vs_random != null ? num(m.p_vs_random, 3) : "—"}</td></tr>`; })
      .concat([`<tr class="muted"><td>Random eligible strategy</td><td class="num">${rs(s.random.total)}</td><td colspan="8" class="small">90% of random-pick paths ended between ${rs(s.random.p05)} and ${rs(s.random.p95)}</td></tr>`]));
    const series = {}; for (const k of order) if (bt.curves.series[k]) series[LABELS[k]] = bt.curves.series[k];
    $("bt-chart").innerHTML = lineChart(bt.curves.days, series);
    const main = s.B_regime_shrunk;
    $("bt-quarters").innerHTML = table(["Quarter", H("Sessions"), H("Total per ₹1L")], Object.entries(main.by_quarter).map(([q, v]) => `<tr><td>${esc(q)}</td><td class="num">${v.days}</td><td class="num ${cls(v.total)}">${rs(v.total)}</td></tr>`));
    $("bt-regimes").innerHTML = table(["NIFTY regime", H("Sessions"), H("Total per ₹1L")], Object.entries(main.by_regime).map(([q, v]) => `<tr><td>${esc(q)}</td><td class="num">${v.days}</td><td class="num ${cls(v.total)}">${rs(v.total)}</td></tr>`));
    $("bt-log").innerHTML = table(["Day", "Regime", "Main engine pick", H("Result per ₹1L"), "Best that day"], bt.log.slice().reverse().map((d) => { const p = d.picks.B_regime_shrunk; return `<tr><td>${esc(d.day)}</td><td class="small">${esc(d.regime)}</td>
      <td>${p.pick ? esc(nameOf(p.pick)) : '<span class="muted">no pick</span>'}</td><td class="num ${cls(p.ret)}">${p.pick ? rs(p.ret) : "—"}</td><td>${d.best ? esc(nameOf(d.best)) : "—"}</td></tr>`; }));
  }
  const h = hist.summary;
  const stat = (k, v) => `<div class="stat"><div class="k">${esc(k)}</div><div class="v nocap">${v}</div></div>`;
  $("hist-stats").innerHTML = [stat("Recommendations stored", h.recommendations), stat("With actual results", h.evaluated),
    stat("Profitable picks", `${h.profitable_picks} / ${h.picked_and_evaluated}`), stat("Picked the day's best", `${h.best_pick_hits} / ${h.picked_and_evaluated}`),
    stat("Total of picks per ₹1L", rs(h.total_per_lakh))].join("");
  $("hist-table").innerHTML = table(["For", "Data cutoff", "Model", "Pick", "Ranking", "Regime", H("Actual per ₹1L"), "Actual best", H("Pick's actual rank")],
    hist.rows.map((r) => `<tr><td>${esc(r.target_day)}</td><td>${esc(r.cutoff_day)}</td><td class="small">${esc(r.model_version)}</td>
      <td>${r.top_strategy ? esc(nameOf(r.top_strategy)) : '<span class="muted">none</span>'}</td>
      <td class="small">${r.ranking.filter((x) => x.rank).map((x) => `${x.rank}. ${esc(x.name)}`).join("<br>")}</td>
      <td class="small">${Object.entries(r.regimes).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>")}</td>
      <td class="num ${r.actual ? cls(r.actual.pick_ret) : ""}">${r.actual ? (r.top_strategy ? rs(r.actual.pick_ret) : "—") : '<span class="muted small">pending</span>'}</td>
      <td>${r.actual && r.actual.best ? esc(nameOf(r.actual.best)) : ""}</td><td class="num">${r.actual && r.actual.pick_rank ? `${r.actual.pick_rank} of ${r.actual.strategies_reported}` : ""}</td></tr>`),
    "No recommendations stored yet.");
};

// ---- data -------------------------------------------------------------------------------
function showResult(el, r) {
  el.classList.remove("hidden");
  const counts = `Added ${num(r.added)} · already stored ${num(r.skipped)} · conflicting ${num(r.conflicting)} · rows in file ${num(r.rows_in_file)}`;
  const per = r.strategies ? `<ul class="reasons">${Object.entries(r.strategies).map(([id, x]) => `<li>${esc(nameOf(id))}: ${x.days} days (${esc(x.first_day)} → ${esc(x.last_day)}), ${x.added} new</li>`).join("")}</ul>` : "";
  const map = Object.entries(r.mapping || {}).map(([k, v]) => `${esc(k)} ← “${esc(v)}”`).join(" · ");
  el.innerHTML = `<div class="notice ok">Imported. ${counts}${r.interval ? ` · ${esc(r.interval)} bars, ${esc(r.first_day)} → ${esc(r.last_day)}, ${r.sessions} sessions` : ""}</div>
    ${r.basis ? `<p class="small">P&amp;L read as: ${esc(r.basis)}</p>` : ""}${per}
    <p class="small muted">Columns used: ${map}</p>
    ${r.warnings.length ? `<ul class="warn-list">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    ${r.refresh && r.refresh.recommendation_for ? `<p class="small">Recommendation for ${esc(r.refresh.recommendation_for)} recomputed.</p>` : ""}`;
}
async function upload(form, fields, resultEl) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) fd.append(k, v);
  const res = await fetch(API + "/import", { method: "POST", body: fd, headers: headers(), credentials: "same-origin" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  showResult(resultEl, body); form.reset(); await loaders.data();
}
$("up-market").addEventListener("submit", async (e) => {
  e.preventDefault(); clearError(); const btn = e.target.querySelector("button"); btn.disabled = true;
  try { await upload(e.target, { file: $("m-file").files[0], kind: "market", target: $("m-symbol").value, replace: $("m-replace").checked }, $("m-result")); }
  catch (err) { showError(err); } finally { btn.disabled = false; }
});
$("up-pnl").addEventListener("submit", async (e) => {
  e.preventDefault(); clearError(); const btn = e.target.querySelector("button"); btn.disabled = true;
  try { await upload(e.target, { file: $("p-file").files[0], kind: "strategy_pnl", target: $("p-strategy").value, replace: $("p-replace").checked }, $("p-result")); }
  catch (err) { showError(err); } finally { btn.disabled = false; }
});
$("fetch-daily").onclick = async (e) => {
  e.target.disabled = true; clearError();
  try {
    const r = await api("/fetch-daily", { method: "POST", json: { symbols } });
    $("m-result").classList.remove("hidden");
    $("m-result").innerHTML = `<ul class="reasons">${Object.entries(r.results).map(([k, v]) => `<li>${esc(k)}: ${v.error ? `<span class="neg">${esc(v.error)}</span>` : `${esc(v.provider)} — ${v.added} new day(s)${v.last_day ? `, up to ${esc(v.last_day)}` : ""}${v.note ? ` (${esc(v.note)})` : ""}`}</li>`).join("")}</ul>`;
    await loaders.data();
  } catch (err) { showError(err); } finally { e.target.disabled = false; }
};

loaders.data = async () => {
  const [reg, imp, mk] = await Promise.all([api("/strategies"), api("/imports"), api("/market")]);
  registry = reg.strategies; symbols = reg.symbols;
  const opts = symbols.map((s) => `<option>${esc(s)}</option>`).join("");
  $("m-symbol").innerHTML = opts; $("ra-und").innerHTML = opts;
  $("p-strategy").innerHTML = `<option value="">Use the file's strategy column</option>` + registry.map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  $("reg-table").innerHTML = table(["Name", "Index", H("Margin (₹)"), H("Cost / active day (₹)"), "Active", H("Days"), ""],
    registry.map((s) => `<tr data-id="${esc(s.id)}"><td><input data-f="name" value="${esc(s.name)}" size="22"><div class="small muted">${esc(s.id)}${s.tradetron_id ? ` · Tradetron ${esc(s.tradetron_id)}` : ""}</div>${s.notes ? `<div class="small muted">${esc(s.notes)}</div>` : ""}</td>
      <td><select data-f="underlying">${symbols.map((x) => `<option${x === s.underlying ? " selected" : ""}>${esc(x)}</option>`).join("")}</select></td>
      <td class="num"><input data-f="margin" type="number" min="1" value="${s.margin}" size="8"></td>
      <td class="num"><input data-f="cost_per_active_day" type="number" min="0" step="0.1" value="${s.cost_per_active_day}" size="6"></td>
      <td><input data-f="active" type="checkbox"${s.active ? " checked" : ""}></td><td class="num">${num(s.days)}</td>
      <td class="actions"><button data-save="${esc(s.id)}">Save</button>${s.days ? "" : ` <button class="danger" data-del="${esc(s.id)}">Delete</button>`}</td></tr>`));
  $("imp-table").innerHTML = table(["Uploaded", "File", "Type", "For", H("Rows"), H("Added"), H("Already stored"), H("Conflicting"), "Dates", "Notes"],
    imp.imports.map((i) => `<tr><td class="small">${esc(new Date(i.uploaded_at).toLocaleString())}<div class="muted">${esc(i.uploaded_by)}</div></td>
      <td class="small">${esc(i.filename)}<div class="muted">kept as ${esc(i.stored_as.slice(0, 12))}…</div></td><td>${esc(i.kind === "market" ? `market ${i.details.interval || ""}` : "strategy P&L")}</td>
      <td class="small">${esc(i.kind === "market" ? i.target : i.target.split(",").map(nameOf).join(", "))}</td><td class="num">${num(i.rows_in_file)}</td><td class="num">${num(i.rows_added)}</td>
      <td class="num">${num(i.rows_skipped)}</td><td class="num">${num(i.rows_conflicting)}</td><td class="small">${esc(i.first_date)} → ${esc(i.last_date)}</td>
      <td>${(i.details.warnings || []).length ? `<details><summary class="small">${i.details.warnings.length} note(s)</summary><ul class="warn-list">${i.details.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></details>` : ""}</td></tr>`), "No files imported yet.");
  $("market-view").innerHTML = mk.markets.map((m) => `<h3 class="sub">${esc(m.symbol)} — ${m.days} sessions with features (${esc(m.first_day)} → ${esc(m.last_day)})</h3>
    <p class="small muted">Regime counts: ${Object.entries(m.regime_counts).map(([k, v]) => `${esc(k)} ${v}`).join(" · ")}</p>
    <div class="table-scroll"><table>${table(["Day", H("Close"), H("Day return"), H("ATR %"), H("Trend efficiency"), "Regime", "Direction"],
      m.recent.slice().reverse().slice(0, 10).map((r) => `<tr><td>${esc(r.day)}</td><td class="num">${num(r.close, 2)}</td><td class="num ${cls(r.ret_1d)}">${pct(r.ret_1d, 2)}</td><td class="num">${pct(r.atr14_pct, 2)}</td><td class="num">${num(r.eff10, 2)}</td><td>${esc(r.regime)}</td><td>${esc(r.direction)}</td></tr>`))}</table></div>`).join("") || '<p class="muted">No market data yet.</p>';
};
$("reg-table").addEventListener("click", async (e) => {
  const id = e.target.dataset.save || e.target.dataset.del; if (!id) return;
  clearError();
  try {
    if (e.target.dataset.del) {
      if (!confirm(`Delete ${nameOf(id)}?`)) return;
      await api(`/strategies/${encodeURIComponent(id)}`, { method: "DELETE" });
    } else {
      const tr = e.target.closest("tr"); const f = (k) => tr.querySelector(`[data-f="${k}"]`);
      const s = registry.find((x) => x.id === id);
      await api(`/strategies/${encodeURIComponent(id)}`, { method: "PUT", json: { ...s, name: f("name").value, underlying: f("underlying").value,
        margin: Number(f("margin").value), cost_per_active_day: Number(f("cost_per_active_day").value), active: f("active").checked } });
    }
    await loaders.data();
  } catch (err) { showError(err); }
});
$("reg-add").addEventListener("submit", async (e) => {
  e.preventDefault(); clearError();
  try {
    await api("/strategies", { method: "POST", json: { id: $("ra-id").value.trim(), name: $("ra-name").value.trim(), underlying: $("ra-und").value,
      margin: Number($("ra-margin").value), cost_per_active_day: Number($("ra-cost").value || 0) } });
    e.target.reset(); await loaders.data();
  } catch (err) { showError(err); }
});
$("backup").onclick = async () => {
  clearError();
  try {
    const res = await fetch(API + "/backup", { headers: headers(), credentials: "same-origin" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const a = document.createElement("a"); a.href = URL.createObjectURL(await res.blob());
    a.download = `research-backup-${new Date().toISOString().slice(0, 10)}.zip`; a.click(); URL.revokeObjectURL(a.href);
  } catch (err) { showError(err); }
};
$("restore-file").onchange = async (e) => {
  const f = e.target.files[0]; e.target.value = ""; if (!f) return;
  if (!confirm(`Replace ALL research data with the contents of ${f.name}? Download a backup first if unsure.`)) return;
  clearError();
  try {
    const fd = new FormData(); fd.append("file", f);
    const res = await fetch(API + "/restore", { method: "POST", body: fd, headers: headers(), credentials: "same-origin" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);
    alert(`Restored (${body.raw_files} original files).`); await loaders.data();
  } catch (err) { showError(err); }
};

// ---- settings -----------------------------------------------------------------------------
loaders.settings = async () => {
  const s = await api("/settings");
  $("s-engine").innerHTML = s.engines.map((e) => `<option value="${esc(e.id)}"${e.id === s.engine ? " selected" : ""}>${esc(e.label)}</option>`).join("");
  $("s-config").innerHTML = Object.entries(s.config).filter(([k]) => k !== "seed").map(([k, v]) => `<div><label for="c-${k}">${esc(k)}</label><input id="c-${k}" data-k="${esc(k)}" type="number" step="any" value="${v}"></div>`).join("");
  $("s-version").textContent = `Model version ${s.model_version}`;
  $("s-pick-enabled").checked = s.schedule.research_pick_enabled; $("s-pick-time").value = s.schedule.research_pick_time;
  $("s-fetch").checked = s.schedule.research_fetch_daily;
  const p = s.published_today;
  $("s-published").textContent = p ? `Today (${s.today}) published at ${p.at.slice(11, 16)} IST with data up to ${p.cutoff_day}.` : `Not published yet today (${s.today}).`;
};
$("s-save").onclick = async (e) => {
  clearError(); e.target.disabled = true;
  try {
    const config = {}; document.querySelectorAll("#s-config input").forEach((i) => { config[i.dataset.k] = Number(i.value); });
    const schedule = { research_pick_enabled: $("s-pick-enabled").checked, research_pick_time: $("s-pick-time").value,
                       research_fetch_daily: $("s-fetch").checked };
    await api("/settings", { method: "PUT", json: { engine: $("s-engine").value, config, schedule } }); await loaders.settings();
    alert("Saved. The next recommendation and validation use these settings.");
  } catch (err) { showError(err); } finally { e.target.disabled = false; }
};

// ---- start ------------------------------------------------------------------------------------
(async () => {
  try {
    const me = await fetch("/api/admin/me", { headers: headers(), credentials: "same-origin" });
    if (!me.ok) { $("signin").classList.remove("hidden"); return; }
    const who = await me.json();
    document.body.dataset.role = who.role;
    $("who").textContent = `${who.name || who.email} · ${who.role === "master" ? "master admin" : "admin"}`;
    $("panel").classList.remove("hidden");
    let t = "dashboard"; try { t = sessionStorage.getItem("sr_research_tab") || t; } catch {}
    const btn = [...$("tabs").children].find((b) => b.dataset.tab === t);
    (btn || $("tabs").children[0]).click();
  } catch (e) { showError(e); }
})();
