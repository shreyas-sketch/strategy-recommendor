// Renders a recommendation payload (market check or the shared daily pick) as HTML. All values are escaped.

function recHTML(payload, { daily = false } = {}) {
  const { recommendation: r, strategies, meta } = payload;
  const name = (id) => strategies[id]?.name || id;
  const noTrade = r.top_strategy_id === "NO_TRADE";
  const top = r.rankings.find((x) => x.strategy_id === r.top_strategy_id);
  const forDay = (d) => d ? new Date(d + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) : "";
  const scope = daily ? `For trading on ${esc(forDay(meta.day))}`
    : `${esc(meta.instrument_label)} · ${esc(meta.horizon)}${meta.trading_day ? ` · for trading on ${esc(forDay(meta.trading_day))}` : ""}`;

  const notice = meta.note ? `<div class="notice" style="margin-bottom:16px">${esc(meta.note)}</div>` : "";
  const topCard = noTrade
    ? `<section class="card top notrade"><div class="muted small">Recommendation · ${scope}</div>
         <div class="name">Sit this one out</div><div>${esc(r.why_top_over_others)}</div></section>`
    : `<section class="card top"><div class="muted small">Best fit · ${scope} · confidence <b>${esc(r.confidence)}</b></div>
         <div class="name">${esc(name(r.top_strategy_id))}${r.run_on ? ` <span class="muted runon">on ${esc(r.run_on)}</span>` : ""}</div>
         <div>${esc(r.why || top?.why)}</div>
         <div class="muted small" style="margin-top:6px">Watch out: ${esc(top?.watch_out)}</div></section>`;

  const summary = daily
    ? `<section class="card"><h2>Today's markets</h2><p style="margin-top:0">${esc(r.market_summary || "")}</p>
         ${marketsTable(payload.markets)}
         ${expiryLine(payload.expiries, payload.markets)}</section>`
    : regimeCard(payload.snapshot, r.market_summary);

  const ranked = r.rankings.map((a) => `
    <div class="strat">
      <div class="head"><b>${esc(name(a.strategy_id))}</b><span class="pill ${esc(a.verdict)}">${esc(a.verdict)} · ${Number(a.fit_score)}</span></div>
      <div class="bar"><i style="width:${Math.max(0, Math.min(100, Number(a.fit_score)))}%"></i></div>
      <div class="small">${esc(a.why)}</div>
      <div class="small muted">Risk: ${esc(a.watch_out)}</div>
      ${a.vs_top && a.strategy_id !== r.top_strategy_id ? `<div class="small vs">vs top pick: ${esc(a.vs_top)}</div>` : ""}
    </div>`).join("");

  const footer = daily
    ? `${meta.engine === "claude" ? "AI analysis" : "Rule engine"} · markets analysed: ${esc((meta.markets || [meta.benchmark]).join(", "))} · data: ${esc(meta.data_provider)}`
    : `${meta.engine === "claude" ? `AI: ${esc(meta.model)}` : "Rule engine"} · data: ${esc(meta.data_provider)} · as of ${esc(new Date(payload.snapshot.as_of).toLocaleString())}${meta.cached ? ` · cached ${Math.round(meta.cache_age_s / 60)} min ago` : ""}`;

  const excluded = (r.excluded || []).length ? `<section class="card"><h2>Not trading in this session</h2>
      <p class="muted small" style="margin-top:0">Some strategies only trade on expiry days (exchange calendar via Groww).</p>
      ${r.excluded.map((x) => `<div class="strat"><b>${esc(name(x.strategy_id))}</b> <span class="pill conditional">schedule</span>
        <ul class="small reasons">${x.reasons.map((why) => `<li>${esc(why)}</li>`).join("")}</ul></div>`).join("")}</section>` : "";

  return notice + topCard + comparisonHTML(r, name, noTrade) + summary +
    (ranked ? `<section class="card"><h2>${(r.excluded || []).length ? "Strategies trading this session, ranked" : "All strategies, ranked"}</h2>${ranked}</section>` : "") +
    excluded +
    ((r.reassess_if || []).length ? `<section class="card"><h2>Re-check if…</h2><ul>${r.reassess_if.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></section>` : "") +
    `<p class="muted small">${footer}</p>`;
}

function comparisonHTML(r, name, noTrade) {
  const factors = r.deciding_factors || [];
  if (!r.why_top_over_others && !factors.length) return "";
  const ids = r.rankings.map((a) => a.strategy_id);
  const pick = (id) => (id === r.top_strategy_id ? "pick" : "");
  const table = factors.length ? `<div class="table-scroll"><table class="matrix">
      <tr><th>Fact</th>${ids.map((id) => `<th class="c ${pick(id)}">${esc(name(id))}</th>`).join("")}</tr>
      ${factors.map((f) => `<tr><td>${esc(f.factor)}</td>${ids.map((id) => `<td class="c ${pick(id)}">${
        f.favors.includes(id) ? '<span class="yes">✓</span>' : f.hurts.includes(id) ? '<span class="no">✗</span>' : ""}</td>`).join("")}</tr>`).join("")}
      <tr><td><b>Fit score</b></td>${r.rankings.map((a) => `<td class="c ${pick(a.strategy_id)}"><b>${Number(a.fit_score)}</b></td>`).join("")}</tr>
    </table></div><p class="muted small" style="margin-bottom:0">✓ helps the strategy · ✗ works against it · blank = neutral</p>` : "";
  return `<section class="card"><h2>${noTrade ? "Why no strategy is recommended" : `Why ${esc(name(r.top_strategy_id))} over the others`}</h2>
    <p style="margin-top:0">${esc(r.why_top_over_others)}</p>${table}</section>`;
}

function expiryText(e) {
  if (!e || typeof e !== "object") return null;
  if (e.is_expiry_day) return `${e.next_expiry_type} expiry today`;
  return `${e.next_expiry_type} expiry ${e.next_expiry_weekday} ${e.next_expiry} (${e.trading_days_to_expiry} trading day${e.trading_days_to_expiry === 1 ? "" : "s"})`;
}

function expiryLine(expiries, markets) {
  const rows = Object.entries(expiries || {}).map(([k, e]) => [markets?.[k]?.instrument || k, expiryText(e)]).filter(([, t]) => t);
  return rows.length ? `<p class="small muted">Expiries: ${rows.map(([l, t]) => `<b>${esc(l)}</b> — ${esc(t)}`).join(" · ")}</p>` : "";
}

function regimeCard(s, summary) {
  const g = s.regime, p = s.primary;
  const stats = [["Trend", g.trend], ["Strength", g.trend_strength], ["Volatility", `${g.volatility} (${g.volatility_score})`],
    ["Squeeze", g.squeeze ? "yes" : "no"], ["ADX", p.adx], ["Return", `${p.window_return_pct}%`]];
  const exp = expiryText(s.expiry);
  if (exp) stats.push(["Next expiry", exp]);
  return `<section class="card"><h2>Market regime</h2><div class="grid">${stats.map(([k, v]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>
    <p style="margin:12px 0 0">${esc(summary)}</p></section>`;
}

function marketsTable(markets) {
  const rows = Object.values(markets || {}).map((m) => `<tr><td>${esc(m.instrument)}</td>
    <td>${esc(m.regime?.trend || "—")}${m.regime?.trend_strength ? ` <span class="muted small">(${esc(m.regime.trend_strength)})</span>` : ""}</td>
    <td>${esc(m.regime?.volatility || "—")}</td><td class="num">${esc(m.primary?.adx ?? "—")}</td>
    <td class="num">${m.primary?.window_return_pct ?? "—"}${m.primary?.window_return_pct != null ? "%" : ""}</td></tr>`).join("");
  return rows ? `<div class="table-scroll"><table><tr><th>Index</th><th>Trend</th><th>Volatility</th><th class="num">ADX</th><th class="num">Return</th></tr>${rows}</table></div>` : "";
}
