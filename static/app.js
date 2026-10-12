let horizon = "1d";
let todayLoaded = false;

function showTab(tab) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  ["today", "portfolio", "market", "settings"].forEach((t) => $(`tab-${t}`).classList.toggle("hidden", t !== tab));
  $("error").classList.add("hidden");
  if (tab === "today" && !todayLoaded) loadToday();
  if (tab === "portfolio") loadPortfolio();
  try { sessionStorage.setItem("sr_tab", tab); } catch {}
}

async function loadMe() {
  const me = await api("/api/me");
  $("who").textContent = me.name;
  $("today-empty").classList.toggle("hidden", me.has_portfolio);
  $("opt-in").checked = me.notify_opt_in;
  $("opt-in-label").textContent = `Send me today's strategy on WhatsApp (${me.phone}) at ${me.notify_time}`;
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
    $("today-result").innerHTML = recHTML(payload, { personal: true });
    todayLoaded = true;
  } catch (err) { showError($("error"), err); }
  finally { $("today-loading").classList.add("hidden"); }
}

async function loadPortfolio() {
  const { portfolio } = await api("/api/portfolio");
  $("holdings-card").classList.toggle("hidden", !portfolio);
  if (!portfolio) return;
  $("holdings-meta").textContent = `${portfolio.holdings.length} holdings · from ${portfolio.source_name || "upload"} · ${new Date(portfolio.uploaded).toLocaleString()}`;
  $("holdings").innerHTML = `<tr><th>Symbol</th><th class="num">Quantity</th><th class="num">Avg price</th></tr>` +
    portfolio.holdings.map((h) => `<tr><td>${esc(h.symbol)}</td><td class="num">${esc(h.quantity)}</td><td class="num">${esc(h.avg_price ?? "—")}</td></tr>`).join("");
  const u = portfolio.unresolved || [];
  $("unresolved").classList.toggle("hidden", !u.length);
  $("unresolved").textContent = u.length ? `Could not match ${u.length} row(s) to an NSE symbol and skipped them: ${u.join(", ")}. Use the template CSV with trading symbols to include them.` : "";
}

$("upload").onclick = async () => {
  const f = $("file").files[0];
  if (!f) return showError($("error"), "Choose a file first");
  const fd = new FormData(); fd.append("file", f);
  $("upload").disabled = true;
  try {
    await api("/api/portfolio", { method: "POST", body: fd });
    $("file").value = ""; todayLoaded = false;
    await Promise.all([loadPortfolio(), loadMe()]);
  } catch (err) { showError($("error"), err); }
  finally { $("upload").disabled = false; }
};

$("delete-portfolio").onclick = async () => {
  if (!confirm("Delete your uploaded portfolio?")) return;
  try { await api("/api/portfolio", { method: "DELETE" }); todayLoaded = false; await Promise.all([loadPortfolio(), loadMe()]); }
  catch (err) { showError($("error"), err); }
};

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

async function initMarket() {
  const opts = await api("/api/options");
  $("instrument-list").innerHTML = opts.instruments.map((i) => `<option value="${esc(i.key)}">${esc(i.label)}</option>`).join("");
  $("horizons").innerHTML = opts.horizons.map((h) => `<button data-h="${esc(h.key)}" class="${h.key === horizon ? "on" : ""}">${esc(h.label)}</button>`).join("");
  $("horizons").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    horizon = b.dataset.h;
    [...$("horizons").children].forEach((x) => x.classList.toggle("on", x === b));
  });
}

$("go").onclick = async () => {
  $("go").disabled = true; $("go").textContent = "Analyzing…"; $("error").classList.add("hidden");
  try {
    const payload = await api("/api/recommend", { method: "POST", json: { instrument: $("instrument").value.trim(), horizon } });
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
  showTab(tab);
})();
