let horizon = "1d";
let todayLoaded = false;

function showTab(tab) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  ["today", "market", "settings"].forEach((t) => $(`tab-${t}`).classList.toggle("hidden", t !== tab));
  $("error").classList.add("hidden");
  if (tab === "today" && !todayLoaded) loadToday();
  try { sessionStorage.setItem("sr_tab", tab); } catch {}
}

async function loadMe() {
  const me = await api("/api/me");
  $("who").textContent = me.name;
  $("opt-in").checked = me.notify_opt_in;
  $("opt-in").disabled = !me.phone;
  $("opt-in-label").textContent = me.phone ? `Send me today's strategy on WhatsApp (${me.phone}) at ${me.notify_time}`
    : "WhatsApp messages need a phone number — ask the admin to add yours.";
  $("opt-in-note").textContent = me.notifications_live
    ? "Messages go out on trading days. You can turn this off anytime."
    : "Daily messages are not switched on by the admin yet — your choice is saved for when they are.";
  return me;
}

const inr = (x) => (x === null || x === undefined ? "—" : (x < 0 ? "-₹" : "₹") + Math.round(Math.abs(x)).toLocaleString("en-IN"));

function researchHTML(v) {
  const at = v.published_at ? v.published_at.slice(11, 16) : v.publish_time;
  const head = `<div class="muted small">Today's strategy · ${esc(v.day)} · ${v.published_at ? "published" : "publishes"} at ${esc(at)} IST</div>`;
  if (v.status === "closed") return `<div class="card">${head}<div class="name-lg">Market closed today</div>
    <p class="muted small">The next pick is published on ${esc(v.next_session)} at ${esc(v.publish_time)} IST.</p></div>`;
  if (v.status === "disabled") return `<div class="card">${head}<p class="muted">The daily strategy pick is switched off by the admin.</p></div>`;
  if (v.status !== "published") return `<div class="card">${head}<div class="name-lg">${v.status === "pending" ? `Published at ${esc(v.publish_time)} IST` : "Being prepared…"}</div>
    <p class="muted small">${v.status === "pending" ? "Come back after the publish time to see which strategy to run today." : "Today's pick is being computed. Refresh in a minute."}</p></div>`;
  const r = v.recommendation;
  const top = r.ranking.find((x) => x.id === r.top_strategy);
  const regimes = r.markets.map((m) => `${esc(m.symbol)}: ${esc(m.regime)}, ${esc(m.direction)}`).join(" · ");
  const body = top ? `<div class="name-lg">${esc(top.name)}</div>
      <div class="grid">
        <div class="stat"><div class="k">Expected net today</div><div class="v nocap">${inr(top.expected_rs)}</div><div class="small muted">on ${inr(top.margin)} margin · range ${inr(top.low_rs)} to ${inr(top.high_rs)}</div></div>
        <div class="stat"><div class="k">Chance of a profitable day</div><div class="v nocap">${top.p_win != null ? Math.round(top.p_win * 100) + "%" : "—"}</div><div class="small muted">model estimate, not a guarantee</div></div>
        <div class="stat"><div class="k">Evidence</div><div class="v nocap">${top.regime_days ?? 0} / ${top.n_days}</div><div class="small muted">similar days / all days</div></div>
      </div>
      <ul class="reasons">${(top.why || []).slice(0, 3).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
      ${r.also_qualify.length ? `<p class="small">Also qualifies: ${r.also_qualify.map((id) => esc((r.ranking.find((x) => x.id === id) || {}).name || id)).join(", ")}</p>` : ""}`
    : `<div class="name-lg">No strategy today</div><p class="small">No strategy meets the current selection criteria, so the recommendation is to sit out.</p>`;
  const others = r.ranking.filter((x) => x.id !== r.top_strategy).map((x) => `<li><b>${esc(x.name)}</b>: ${esc((x.why || []).find((w) => /Ranks below|does not qualify/.test(w)) || (x.why || [])[0] || "")}</li>`).join("");
  const warn = r.warnings.length ? `<div class="notice" style="margin-top:10px"><ul class="reasons">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
  return `<div class="card top${top ? "" : " notrade"}">${head}${body}
    <p class="small muted">Market at the ${esc(r.cutoff_day)} close — ${regimes || "unknown"}</p>${warn}
    ${others ? `<details><summary class="small">Why not the others</summary><ul class="reasons">${others}</ul></details>` : ""}
    <p class="small muted">${esc(r.disclaimer || "")}</p></div>`;
}

async function loadResearch() {
  try { $("research-today").innerHTML = researchHTML(await api("/api/me/research-today")); }
  catch (err) { $("research-today").innerHTML = `<div class="card error">${esc(err.message)}</div>`; }
}

async function loadToday() {
  loadResearch();
  $("today-loading").classList.remove("hidden");
  $("today-result").innerHTML = "";
  try {
    const payload = await api("/api/me/today");
    $("today-result").innerHTML = recHTML(payload, { daily: true });
    todayLoaded = true;
  } catch (err) { showError($("error"), err); }
  finally { $("today-loading").classList.add("hidden"); }
}

$("opt-in").onchange = async (e) => {
  try { await api("/api/me/notifications", { method: "PUT", json: { opt_in: e.target.checked } }); }
  catch (err) { e.target.checked = !e.target.checked; showError($("error"), err); }
};

$("pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/me/password", { method: "POST", json: { current_password: $("pw-cur").value, new_password: $("pw-new").value } });
    $("pw-msg").textContent = "Password updated. Other devices were signed out."; e.target.reset();
  } catch (err) { $("pw-msg").textContent = err.message; }
});

$("logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); location.href = "/login"; };

let marketOpts = null;
const DAY_RULE = { expiry_day: "trades only on expiry day", expiry_and_day_before: "trades only on expiry day and the day before" };

function marketHint() {
  if (!marketOpts) return;
  const sel = $("instrument-select").value;
  $("instrument").classList.toggle("hidden", sel !== "__other");
  const inst = marketOpts.instruments.find((i) => i.key === sel);
  if (!inst) { $("market-hint").textContent = "Strategies without a fixed index are judged on the stock you type."; return; }
  const here = inst.strategies.filter((st) => st.horizons.includes(horizon));
  $("market-hint").textContent = here.length
    ? `Judged for ${inst.label} (${horizon}): ` + here.map((st) => st.name + (DAY_RULE[st.active_days] && horizon === "1d" ? ` — ${DAY_RULE[st.active_days]}` : "")).join("; ")
    : `No strategy for ${inst.label} is meant for the ${horizon} horizon — try another horizon.`;
}

async function initMarket() {
  marketOpts = await api("/api/options");
  $("instrument-select").innerHTML = marketOpts.instruments.map((i) =>
    `<option value="${esc(i.key)}">${esc(i.label)} (${i.strategies.length} strateg${i.strategies.length === 1 ? "y" : "ies"})</option>`).join("") +
    (marketOpts.any_symbol ? '<option value="__other">Other NSE stock…</option>' : "");
  $("horizons").innerHTML = marketOpts.horizons.map((h) => `<button data-h="${esc(h.key)}" class="${h.key === horizon ? "on" : ""}">${esc(h.label)}</button>`).join("");
  $("horizons").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    horizon = b.dataset.h;
    [...$("horizons").children].forEach((x) => x.classList.toggle("on", x === b));
    marketHint();
  });
  $("instrument-select").onchange = marketHint;
  marketHint();
}

$("go").onclick = async () => {
  $("go").disabled = true; $("go").textContent = "Analyzing…"; $("error").classList.add("hidden");
  try {
    const payload = await api("/api/recommend", { method: "POST", json: { instrument: $("instrument-select").value === "__other" ? $("instrument").value.trim() : $("instrument-select").value, horizon } });
    $("market-result").innerHTML = recHTML(payload);
  } catch (err) { showError($("error"), err); $("market-result").innerHTML = ""; }
  finally { $("go").disabled = false; $("go").textContent = "Analyze market"; }
};

$("tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) showTab(b.dataset.tab); });

(async () => {
  try { await loadMe(); } catch { return; }   // 401 -> api() already redirects to /login
  initMarket();
  let tab = "today";
  try { tab = sessionStorage.getItem("sr_tab") || "today"; } catch {}
  if (!$(`tab-${tab}`)) tab = "today";   // e.g. a remembered tab that no longer exists
  showTab(tab);
})();
