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
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Requested-With": "fetch", "X-Admin-Token": token, ...(opts.headers || {}) } });
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
  const [cfg, usage] = await Promise.all([api("/api/admin/settings"), api("/api/admin/usage"), loadStrategies(),
                                          loadParticipants(), loadNotifications()]);
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
  $("q-analyses").value = s.per_user_daily_analyses;
  $("q-uploads").value = s.per_user_daily_uploads;
  $("n-bench").value = s.portfolio_benchmark;

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
  per_user_daily_analyses: Number($("q-analyses").value || 0),
  per_user_daily_uploads: Number($("q-uploads").value || 0),
}, e.target);

// ---- participants ------------------------------------------------------
let lastInvites = [];
function showInvites(list) {
  lastInvites = list;
  $("invites").classList.toggle("hidden", !list.length);
  $("invite-table").innerHTML = `<tr><th>Name</th><th>Phone</th><th>Invite link</th></tr>` + list.map((x) =>
    `<tr><td>${esc(x.name)}</td><td>${esc(x.phone)}</td><td><div class="invite-box">${esc(x.invite_url)}</div></td></tr>`).join("");
}
$("invite-csv").onclick = () => {
  // Quote, and neutralise spreadsheet formulas (=, +, -, @) so the file is safe to open in Excel.
  const q = (v) => { let t = String(v); if (/^[=+\-@]/.test(t)) t = "'" + t; return `"${t.replace(/"/g, '""')}"`; };
  const csv = "name,phone,invite_url\n" + lastInvites.map((x) => [x.name, x.phone, x.invite_url].map(q).join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "invite_links.csv"; a.click(); URL.revokeObjectURL(a.href);
};

async function loadParticipants() {
  const { participants } = await api("/api/admin/participants");
  $("participants").innerHTML = `<tr><th>Name</th><th>Phone</th><th>Status</th><th>Profile</th><th>Portfolio</th><th>WhatsApp</th><th>Last login</th><th></th></tr>` +
    (participants.length ? participants.map((p) => `<tr>
      <td>${esc(p.name)}</td><td>${esc(p.phone)}</td>
      <td>${!p.active ? '<span class="pill avoid">disabled</span>' : p.activated ? '<span class="pill recommended">active</span>' : '<span class="pill conditional">invited</span>'}</td>
      <td>${p.has_profile ? "set" : '<span class="muted">—</span>'}</td>
      <td>${p.holdings_count ? `${p.holdings_count} holdings` : '<span class="muted">—</span>'}</td>
      <td>${p.notify_opt_in ? "opted in" : '<span class="muted">off</span>'}${p.last_notification ? ` <span class="muted small">(${esc(p.last_notification)})</span>` : ""}</td>
      <td class="small">${esc(p.last_login || "—")}</td>
      <td class="actions"><button data-invite="${p.id}">New invite</button> <button data-toggle="${p.id}" data-active="${p.active}">${p.active ? "Disable" : "Enable"}</button> <button class="danger" data-remove="${p.id}" data-name="${esc(p.name)}">Delete</button></td>
    </tr>`).join("") : `<tr><td colspan="8" class="muted">No participants yet.</td></tr>`);
}

$("add-participant").addEventListener("submit", async (e) => {
  e.preventDefault(); clearError();
  try {
    const r = await api("/api/admin/participants", { method: "POST", body: JSON.stringify({ name: $("p-name").value, phone: $("p-phone").value }) });
    showInvites([r]); e.target.reset(); await loadParticipants();
  } catch (err) { showError(err); }
});
$("import-btn").onclick = async () => {
  clearError();
  try {
    const r = await api("/api/admin/participants/import", { method: "POST", body: JSON.stringify({ csv_text: $("import-csv").value }) });
    showInvites(r.created);
    if (r.errors.length) showError(r.errors.map((x) => `line ${x.line}: ${x.error}`).join("; "));
    await loadParticipants();
  } catch (err) { showError(err); }
};
$("participants").addEventListener("click", async (e) => {
  const d = e.target.dataset;
  try {
    if (d.invite && confirm("Create a new invite link? Their current password stops working.")) {
      const r = await api(`/api/admin/participants/${d.invite}/invite`, { method: "POST" });
      const row = e.target.closest("tr").children;
      showInvites([{ name: row[0].textContent, phone: row[1].textContent, invite_url: r.invite_url }]);
    } else if (d.toggle) {
      await api(`/api/admin/participants/${d.toggle}`, { method: "PATCH", body: JSON.stringify({ active: d.active !== "1" }) });
    } else if (d.remove && confirm(`Delete ${d.name} and all their data (portfolio, history)? This cannot be undone.`)) {
      await api(`/api/admin/participants/${d.remove}`, { method: "DELETE" });
    } else return;
    await loadParticipants();
  } catch (err) { showError(err); }
});

// ---- notifications ------------------------------------------------------
async function loadNotifications() {
  const n = await api("/api/admin/notifications");
  $("n-enabled").checked = n.enabled; $("n-time").value = n.notify_time; $("n-lead").value = n.prepare_lead_minutes;
  $("n-weekdays").checked = n.weekdays_only; $("n-campaign").value = n.campaign; $("n-holidays").value = n.holidays.join("\n");
  const warn = !n.aisensy_configured ? "AISENSY_API_KEY is not set on the server. " : !n.campaign ? "Set the AiSensy campaign name. " : "";
  $("notify-status").innerHTML = `${warn ? `<span class="pill avoid">${esc(warn)}</span> ` : ""}${n.opted_in} participant(s) opted in · ${n.enabled ? `sends daily at ${esc(n.notify_time)} IST` : "currently off"}`;
  $("n-log").innerHTML = `<tr><th>Time</th><th>Participant</th><th>Status</th><th>Detail</th></tr>` +
    (n.recent.map((r) => `<tr><td class="small">${esc(new Date(r.ts).toLocaleString())}</td><td>${esc(r.name || "test")}</td><td>${esc(r.status)}</td><td class="small">${esc(r.detail)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">Nothing sent yet.</td></tr>`);
  $("n-jobs").innerHTML = `<tr><th>Job</th><th>Day</th><th>Started</th><th>Result</th></tr>` +
    (n.jobs.map((j) => `<tr><td>${esc(j.kind)}</td><td>${esc(j.day)}</td><td class="small">${esc(j.started)}</td><td class="small">${esc(j.summary || (j.finished ? "" : "running…"))}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">No runs yet.</td></tr>`);
}
$("n-save").onclick = (e) => save({
  notify_enabled: $("n-enabled").checked, notify_time: $("n-time").value, prepare_lead_minutes: Number($("n-lead").value || 0),
  notify_weekdays_only: $("n-weekdays").checked, aisensy_campaign: $("n-campaign").value.trim(),
  portfolio_benchmark: $("n-bench").value.trim().toUpperCase() || "NIFTY50",
  market_holidays: $("n-holidays").value.split(/\s+/).map((x) => x.trim()).filter(Boolean),
}, e.target);
$("n-test").onclick = async () => {
  try { const r = await api("/api/admin/notifications/test", { method: "POST", body: JSON.stringify({ phone: $("n-test-phone").value }) });
        alert(r.ok ? "Sent. Check the phone." : `Failed: ${r.detail}`); await loadNotifications(); }
  catch (err) { showError(err); }
};
for (const kind of ["prepare", "send"]) {
  $(`n-${kind}`).onclick = async () => {
    if (kind === "send" && !confirm("Send today's WhatsApp messages now to everyone opted in who hasn't received one today?")) return;
    try { await api(`/api/admin/jobs/${kind}/run`, { method: "POST" }); setTimeout(loadNotifications, 3000); alert(`Started: ${kind}. The log updates as it runs.`); }
    catch (err) { showError(err); }
  };
}
$("reload").onclick = async () => {
  try { await api("/api/admin/strategies/reload", { method: "POST" }); await loadStrategies(); }
  catch (e) { showError(e); }
};

// ---- strategy manager ------------------------------------------------
let strategies = [];
let editingId = null;  // null = creating
let currentSource = null;  // provenance of an imported draft
let drafts = [];

document.querySelectorAll(".checks[data-group]").forEach((box) => {
  const values = box.dataset.values.split(",");
  const labels = (box.dataset.labels || box.dataset.values).split(",");
  box.innerHTML = values.map((v, i) => `<label><input type="checkbox" value="${v}"> ${esc(labels[i])}</label>`).join("");
});
const getChecks = (g) => [...document.querySelectorAll(`.checks[data-group="${g}"] input:checked`)].map((i) => i.value);
const setChecks = (g, vals) => document.querySelectorAll(`.checks[data-group="${g}"] input`).forEach((i) => { i.checked = (vals || []).includes(i.value); });

async function loadStrategies() {
  strategies = (await api("/api/admin/strategies")).strategies;
  $("strategy-list").innerHTML = `<tr><th>Name</th><th>ID</th><th>Category</th><th>Trades on</th><th>Horizons</th><th>Suits</th><th></th></tr>` +
    strategies.map((st) => `<tr><td>${esc(st.name)}${st.source ? ' <span class="pill conditional">Tradetron</span>' : ""}</td><td class="muted">${esc(st.id)}</td><td>${esc(st.category)}</td>
      <td>${esc((st.underlyings || []).join(", ") || "any")}</td>
      <td>${esc(st.horizons.join(", "))}</td>
      <td class="small">${esc(st.risk_level)} risk · ${esc(st.min_experience)}+ · ₹${Number(st.min_capital_inr).toLocaleString("en-IN")}+</td>
      <td class="actions"><button data-edit="${esc(st.id)}">Edit</button> <button class="danger" data-del="${esc(st.id)}">Delete</button></td></tr>`).join("");
}

function openForm(st, { asDraft = false, existingId = null } = {}) {
  editingId = asDraft ? existingId : (st ? st.id : null);
  currentSource = st?.source || null;
  $("form-title").textContent = asDraft ? `Review import: ${st.name}${existingId ? " (updates existing)" : ""}` : st ? `Edit: ${st.name}` : "Add strategy";
  $("f-underlyings").value = (st?.underlyings || []).join(", ");
  $("f-name").value = st?.name || ""; $("f-id").value = st?.id || ""; $("f-category").value = st?.category || "trend";
  $("f-description").value = st?.description || ""; $("f-logic").value = st?.logic || "";
  $("f-avoid").value = (st?.avoid_conditions || []).join("\n"); $("f-risk").value = st?.risk_notes || "";
  setChecks("horizons", st?.horizons || ["1d", "1w", "1m"]); setChecks("instruments", st?.instruments || []);
  const c = st?.ideal_conditions || {};
  setChecks("trend", c.trend); setChecks("trend_strength", c.trend_strength); setChecks("volatility", c.volatility);
  $("f-squeeze").value = String(c.squeeze ?? "any");
  $("f-risklevel").value = st?.risk_level || "medium";
  $("f-mincap").value = st?.min_capital_inr ?? 0;
  $("f-minexp").value = st?.min_experience || "beginner";
  $("strategy-form-card").classList.remove("hidden");
  $("strategy-form-card").scrollIntoView({ behavior: "smooth" });
}

$("add-strategy").onclick = () => openForm(null);

function renderDrafts() {
  $("drafts").classList.toggle("hidden", !drafts.length);
  $("draft-table").innerHTML = `<tr><th>File</th><th>Strategy</th><th>Trades on</th><th>Notes</th><th></th></tr>` +
    drafts.map((d, i) => `<tr><td class="small">${esc(d.file)}</td>
      <td><b>${esc(d.draft.name)}</b><div class="muted small">${esc(d.draft.description)}</div></td>
      <td>${esc((d.draft.underlyings || []).join(", ") || "—")}</td>
      <td><ul class="warn-list">${d.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></td>
      <td class="actions"><button class="primary" data-review="${i}">Review &amp; save</button> <button data-discard="${i}">Discard</button></td></tr>`).join("");
}
$("tt-files").onchange = async (e) => {
  clearError();
  for (const f of e.target.files) {
    const fd = new FormData(); fd.append("file", f);
    try {
      const res = await fetch("/api/admin/strategies/import", { method: "POST", body: fd, headers: { "X-Requested-With": "fetch", "X-Admin-Token": token } });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(`${f.name}: ${body.detail || res.statusText}`);
      drafts.push(body);
    } catch (err) { showError(err); }
  }
  e.target.value = "";
  renderDrafts();
};
$("draft-table").addEventListener("click", (e) => {
  const r = e.target.dataset.review, d = e.target.dataset.discard;
  if (r !== undefined) openForm(drafts[r].draft, { asDraft: true, existingId: drafts[r].existing_id });
  if (d !== undefined) { drafts.splice(Number(d), 1); renderDrafts(); }
});
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
    underlyings: $("f-underlyings").value.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean),
    source: currentSource,
    risk_level: $("f-risklevel").value,
    min_capital_inr: Math.round(Number($("f-mincap").value || 0)),
    min_experience: $("f-minexp").value,
  };
  if (!body.horizons.length) return showError("Pick at least one horizon.");
  $("f-save").disabled = true;
  try {
    await api(editingId ? `/api/admin/strategies/${encodeURIComponent(editingId)}` : "/api/admin/strategies",
              { method: editingId ? "PUT" : "POST", body: JSON.stringify(body) });
    $("strategy-form-card").classList.add("hidden");
    drafts = drafts.filter((d) => !(d.draft.id === body.id || (editingId && d.existing_id === editingId)));
    renderDrafts();
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
