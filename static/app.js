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

async function loadToday() {
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
