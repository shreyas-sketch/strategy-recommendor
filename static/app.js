const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let horizon = "1d";

async function init() {
  const opts = await (await fetch("/api/options")).json();
  $("instrument-list").innerHTML = opts.instruments.map((i) => `<option value="${esc(i.key)}">${esc(i.label)}</option>`).join("");
  $("horizons").innerHTML = opts.horizons.map((h) => `<button data-h="${h.key}" class="${h.key === horizon ? "on" : ""}">${esc(h.label)}</button>`).join("");
  $("horizons").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    horizon = b.dataset.h;
    [...$("horizons").children].forEach((x) => x.classList.toggle("on", x === b));
  });
  $("go").addEventListener("click", analyze);
}

async function analyze() {
  $("go").disabled = true; $("go").textContent = "Analyzing…";
  $("error").classList.add("hidden");
  try {
    const res = await fetch("/api/recommend", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instrument: $("instrument").value.trim(), horizon }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    render(body);
  } catch (err) {
    $("error").textContent = err.message; $("error").classList.remove("hidden");
    $("result").classList.add("hidden");
  } finally {
    $("go").disabled = false; $("go").textContent = "Analyze market";
  }
}

function render({ recommendation: r, snapshot: s, strategies, meta }) {
  const name = (id) => strategies[id]?.name || id;
  const top = r.rankings.find((x) => x.strategy_id === r.top_strategy_id);
  const noTrade = r.top_strategy_id === "NO_TRADE";

  $("top").className = "card top" + (noTrade ? " notrade" : "");
  $("top").innerHTML = noTrade
    ? `<div class="muted small">Recommendation · ${esc(meta.instrument_label)} · ${esc(meta.horizon)}</div>
       <div class="name">Sit this one out</div><div>No strategy fits current conditions well.</div>`
    : `<div class="muted small">Best fit · ${esc(meta.instrument_label)} · ${esc(meta.horizon)} · confidence <b>${esc(r.confidence)}</b></div>
       <div class="name">${esc(name(r.top_strategy_id))}</div>
       <div>${esc(top?.why)}</div>
       <div class="muted small" style="margin-top:6px">Watch out: ${esc(top?.watch_out)}</div>`;

  renderComparison(r, name, noTrade);

  const g = s.regime, p = s.primary;
  const stats = [
    ["Trend", g.trend], ["Strength", g.trend_strength], ["Volatility", `${g.volatility} (${g.volatility_score})`],
    ["Squeeze", g.squeeze ? "yes" : "no"], ["ADX", p.adx], ["Return", `${p.window_return_pct}%`],
  ];
  $("regime").innerHTML = stats.map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join("");
  $("summary").textContent = r.market_summary;

  $("rankings").innerHTML = r.rankings.map((a) => `
    <div class="strat">
      <div class="head"><b>${esc(name(a.strategy_id))}</b><span class="pill ${esc(a.verdict)}">${esc(a.verdict)} · ${a.fit_score}</span></div>
      <div class="bar"><i style="width:${Math.max(0, Math.min(100, a.fit_score))}%"></i></div>
      <div class="small">${esc(a.why)}</div>
      <div class="small muted">Risk: ${esc(a.watch_out)}</div>
      ${a.vs_top && a.strategy_id !== r.top_strategy_id ? `<div class="small vs">vs top pick: ${esc(a.vs_top)}</div>` : ""}
    </div>`).join("");
  $("reassess").innerHTML = (r.reassess_if || []).map((x) => `<li>${esc(x)}</li>`).join("");
  $("raw").textContent = JSON.stringify(s, null, 2);

  const engine = meta.engine === "claude" ? `AI: ${meta.model}` : "Rule engine";
  const age = meta.cached ? ` · cached ${Math.round(meta.cache_age_s / 60)} min ago` : "";
  $("meta").textContent = `${engine} · data: ${meta.data_provider} · as of ${new Date(s.as_of).toLocaleString()}${age}`;

  $("notice").textContent = meta.note || "";
  $("notice").classList.toggle("hidden", !meta.note);
  $("result").classList.remove("hidden");
}

function renderComparison(r, name, noTrade) {
  const factors = r.deciding_factors || [];
  $("compare-card").classList.toggle("hidden", !r.why_top_over_others && !factors.length);
  $("compare-title").textContent = noTrade ? "Why no strategy is recommended" : `Why ${name(r.top_strategy_id)} over the others`;
  $("compare-why").textContent = r.why_top_over_others || "";
  if (!factors.length) { $("matrix").innerHTML = ""; return; }
  const ids = r.rankings.map((a) => a.strategy_id);
  const head = `<tr><th>Market fact</th>${ids.map((id) => `<th class="c ${id === r.top_strategy_id ? "pick" : ""}">${esc(name(id))}</th>`).join("")}</tr>`;
  const rows = factors.map((f) => `<tr><td>${esc(f.factor)}</td>${ids.map((id) => {
    const mark = f.favors.includes(id) ? '<span class="yes">✓</span>' : f.hurts.includes(id) ? '<span class="no">✗</span>' : "";
    return `<td class="c ${id === r.top_strategy_id ? "pick" : ""}">${mark}</td>`;
  }).join("")}</tr>`).join("");
  const scores = `<tr><td><b>Fit score</b></td>${r.rankings.map((a) => `<td class="c ${a.strategy_id === r.top_strategy_id ? "pick" : ""}"><b>${a.fit_score}</b></td>`).join("")}</tr>`;
  $("matrix").innerHTML = head + rows + scores;
}

init();
