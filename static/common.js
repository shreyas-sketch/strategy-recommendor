// Shared helpers for participant pages.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const headers = { "X-Requested-With": "fetch", ...(opts.headers || {}) };
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/auth/")) { location.href = "/login"; throw new Error("Please sign in"); }
  if (!res.ok) {
    const d = body.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => x.msg).join("; ") : d || res.statusText);
  }
  return body;
}

function showError(el, err) { el.textContent = err.message || String(err); el.classList.remove("hidden"); }
