const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (x, d = 2) => "$" + Number(x || 0).toFixed(d);
// Typical request: ~3k input + ~1.5k output tokens (incl. thinking). Real numbers appear in the table below.
const EST_IN = 3000, EST_OUT = 1500;
let token = "";
let selectedModel = null;

function store(k, v) { try { v === undefined ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch {} }
function load(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } }

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Admin-Token": token, ...(opts.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") : d || res.statusText);
  }
  return body;
}
function showError(e) { $("error").textContent = e.message || e; $("error").classList.remove("hidden"); }
function clearError() { $("error").classList.add("hidden"); }

async function refresh() {
  clearError();
  const [cfg, usage] = await Promise.all([api("/api/admin/settings"), api("/api/admin/usage"), loadStrategies()]);
  const s = cfg.settings;
  selectedModel = s.active_model;

  $("models").innerHTML = cfg.models.map((m) => {
    const est = (EST_IN * m.input_per_mtok + EST_OUT * m.output_per_mtok) / 1e6;
    return `<div class="model ${m.id === selectedModel ? "on" : ""}" data-id="${esc(m.id)}">
      <div class="t">${esc(m.label)}</div>
      <div class="small muted">${esc(m.tagline)}</div>
      <div class="small" style="margin-top:8px">${usd(m.input_per_mtok)} in / ${usd(m.output_per_mtok)} out per 1M tokens</div>
      <div class="small">≈ ${usd(est, 4)} per analysis · ≈ ${usd(est * 1000, 2)} per 1,000</div>
    </div>`;
  }).join("");
  $("effort").value = s.effort;

  const e = cfg.engine_now;
  $("engine-now").textContent = e.engine === "claude" ? `Right now analyses run on ${e.model}.${e.note ? " " + e.note : ""}`
    : e.note || `Right now analyses use the ${e.engine} engine.`;

  $("budget").value = s.monthly_budget_usd;
  $("on-exceeded").value = s.on_budget_exceeded;
  $("c1d").value = s.cache_minutes["1d"] ?? 0;
  $("c1w").value = s.cache_minutes["1w"] ?? 0;
  $("c1m").value = s.cache_minutes["1m"] ?? 0;
  $("providers").value = s.provider_order.join(", ");

  const budget = usage.budget_usd;
  const pct = budget > 0 ? Math.min(100, (usage.total_usd / budget) * 100) : 0;
  $("spend").textContent = `${usd(usage.total_usd, 4)} spent in ${usage.month}`;
  $("budget-label").textContent = budget > 0 ? `of ${usd(budget)} budget (${pct.toFixed(0)}%)` : "no budget limit";
  $("meter").querySelector("i").style.width = (budget > 0 ? pct : 0) + "%";
  $("meter").classList.toggle("hot", pct >= 80);

  $("by-model").innerHTML = `<tr><th>Model</th><th class="num">Calls</th><th class="num">Input tok</th><th class="num">Output tok</th><th class="num">Cost</th><th class="num">Avg / call</th></tr>` +
    (usage.by_model.length ? usage.by_model.map((r) => `<tr><td>${esc(r.model)}</td><td class="num">${r.calls}</td>
      <td class="num">${(r.input_tokens || 0).toLocaleString()}</td><td class="num">${(r.output_tokens || 0).toLocaleString()}</td>
      <td class="num">${usd(r.cost_usd, 4)}</td><td class="num">${usd(r.cost_usd / r.calls, 5)}</td></tr>`).join("")
      : `<tr><td colspan="6" class="muted">No AI calls yet this month.</td></tr>`);

  $("recent").innerHTML = `<tr><th>Time</th><th>Model</th><th>Instrument</th><th>Horizon</th><th class="num">Tokens in/out</th><th class="num">Cost</th></tr>` +
    (usage.recent.length ? usage.recent.map((r) => `<tr><td>${esc(new Date(r.ts).toLocaleString())}</td>
      <td>${esc(r.served_model && r.served_model !== r.model ? `${r.model} → ${r.served_model}` : r.model)}</td>
      <td>${esc(r.instrument)}</td><td>${esc(r.horizon)}</td>
      <td class="num">${r.input_tokens}/${r.output_tokens}</td><td class="num">${usd(r.cost_usd, 5)}</td></tr>`).join("")
      : `<tr><td colspan="6" class="muted">Nothing yet.</td></tr>`);
}

async function save(changes, btn) {
  btn.disabled = true;
  try { await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(changes) }); await refresh(); }
  catch (e) { showError(e); }
  finally { btn.disabled = false; }
}

$("models").addEventListener("click", (e) => {
  const card = e.target.closest(".model"); if (!card) return;
  selectedModel = card.dataset.id;
  [...$("models").children].forEach((c) => c.classList.toggle("on", c === card));
});
$("save-model").onclick = (e) => save({ active_model: selectedModel, effort: $("effort").value }, e.target);
$("save-budget").onclick = (e) => save({
  monthly_budget_usd: Number($("budget").value || 0),
  on_budget_exceeded: $("on-exceeded").value,
  cache_minutes: { "1d": Number($("c1d").value || 0), "1w": Number($("c1w").value || 0), "1m": Number($("c1m").value || 0) },
  provider_order: $("providers").value.split(",").map((x) => x.trim()).filter(Boolean),
}, e.target);
$("reload").onclick = async () => {
  try { await api("/api/admin/strategies/reload", { method: "POST" }); await loadStrategies(); }
  catch (e) { showError(e); }
};

// ---- strategy manager ------------------------------------------------
let strategies = [];
let editingId = null;  // null = creating

document.querySelectorAll(".checks[data-group]").forEach((box) => {
  const values = box.dataset.values.split(",");
  const labels = (box.dataset.labels || box.dataset.values).split(",");
  box.innerHTML = values.map((v, i) => `<label><input type="checkbox" value="${v}"> ${esc(labels[i])}</label>`).join("");
});
const getChecks = (g) => [...document.querySelectorAll(`.checks[data-group="${g}"] input:checked`)].map((i) => i.value);
const setChecks = (g, vals) => document.querySelectorAll(`.checks[data-group="${g}"] input`).forEach((i) => { i.checked = (vals || []).includes(i.value); });

async function loadStrategies() {
  strategies = (await api("/api/admin/strategies")).strategies;
  $("strategy-list").innerHTML = `<tr><th>Name</th><th>ID</th><th>Category</th><th>Horizons</th><th></th></tr>` +
    strategies.map((st) => `<tr><td>${esc(st.name)}</td><td class="muted">${esc(st.id)}</td><td>${esc(st.category)}</td>
      <td>${esc(st.horizons.join(", "))}</td>
      <td class="actions"><button data-edit="${esc(st.id)}">Edit</button> <button class="danger" data-del="${esc(st.id)}">Delete</button></td></tr>`).join("");
}

function openForm(st) {
  editingId = st ? st.id : null;
  $("form-title").textContent = st ? `Edit: ${st.name}` : "Add strategy";
  $("f-name").value = st?.name || ""; $("f-id").value = st?.id || ""; $("f-category").value = st?.category || "trend";
  $("f-description").value = st?.description || ""; $("f-logic").value = st?.logic || "";
  $("f-avoid").value = (st?.avoid_conditions || []).join("\n"); $("f-risk").value = st?.risk_notes || "";
  setChecks("horizons", st?.horizons || ["1d", "1w", "1m"]); setChecks("instruments", st?.instruments || []);
  const c = st?.ideal_conditions || {};
  setChecks("trend", c.trend); setChecks("trend_strength", c.trend_strength); setChecks("volatility", c.volatility);
  $("f-squeeze").value = String(c.squeeze ?? "any");
  $("strategy-form-card").classList.remove("hidden");
  $("strategy-form-card").scrollIntoView({ behavior: "smooth" });
}

$("add-strategy").onclick = () => openForm(null);
$("f-cancel").onclick = () => $("strategy-form-card").classList.add("hidden");
$("f-name").addEventListener("input", () => {
  if (editingId === null) $("f-id").value = $("f-name").value.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40);
});
$("strategy-list").addEventListener("click", async (e) => {
  const edit = e.target.dataset.edit, del = e.target.dataset.del;
  if (edit) openForm(strategies.find((st) => st.id === edit));
  if (del && confirm(`Delete strategy "${del}"? This removes its file.`)) {
    try { await api(`/api/admin/strategies/${encodeURIComponent(del)}`, { method: "DELETE" }); await loadStrategies(); }
    catch (err) { showError(err); }
  }
});
$("strategy-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearError();
  const sq = $("f-squeeze").value;
  const body = {
    id: $("f-id").value.trim(), name: $("f-name").value.trim(), category: $("f-category").value,
    horizons: getChecks("horizons"), instruments: getChecks("instruments"),
    description: $("f-description").value.trim(), logic: $("f-logic").value.trim(),
    ideal_conditions: { trend: getChecks("trend"), trend_strength: getChecks("trend_strength"),
                        volatility: getChecks("volatility"), squeeze: sq === "any" ? "any" : sq === "true" },
    avoid_conditions: $("f-avoid").value.split("\n").map((x) => x.trim()).filter(Boolean),
    risk_notes: $("f-risk").value.trim(),
  };
  if (!body.horizons.length) return showError("Pick at least one horizon.");
  $("f-save").disabled = true;
  try {
    await api(editingId ? `/api/admin/strategies/${encodeURIComponent(editingId)}` : "/api/admin/strategies",
              { method: editingId ? "PUT" : "POST", body: JSON.stringify(body) });
    $("strategy-form-card").classList.add("hidden");
    await loadStrategies();
  } catch (err) { showError(err); }
  finally { $("f-save").disabled = false; }
});
$("test").onclick = async (e) => {
  e.target.disabled = true; $("out").textContent = "Running…"; $("out").classList.remove("hidden");
  try {
    const r = await api("/api/recommend", { method: "POST", body: JSON.stringify({ instrument: $("test-inst").value.trim(), horizon: $("test-h").value, force_refresh: true }) });
    $("out").textContent = JSON.stringify({ meta: r.meta, recommendation: r.recommendation }, null, 2);
    await refresh();
  } catch (err) { $("out").textContent = err.message; }
  finally { e.target.disabled = false; }
};
$("signin").onclick = async () => {
  token = $("token").value.trim();
  try { await refresh(); store("sr_admin", token); $("login").classList.add("hidden"); $("panel").classList.remove("hidden"); }
  catch (e) { showError(e); }
};

token = load("sr_admin");
if (token) refresh().then(() => { $("login").classList.add("hidden"); $("panel").classList.remove("hidden"); }).catch(() => store("sr_admin"));
