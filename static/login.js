$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("error").classList.add("hidden");
  try {
    await api("/api/auth/login", { method: "POST", json: { phone: $("phone").value, password: $("password").value } });
    location.href = "/";
  } catch (err) { showError($("error"), err); }
});

fetch("/api/auth/config").then((r) => r.json()).then((c) => $("google-box").classList.toggle("hidden", !c.google)).catch(() => {});
const loginErrors = { not_registered: "That Google account isn't registered. Ask the admin to add your Gmail address.",
                      google_failed: "Google sign-in failed — please try again.",
                      google_not_configured: "Google sign-in isn't available yet — use your mobile number." };
const loginErr = new URLSearchParams(location.search).get("error");
if (loginErr) { showError($("error"), loginErrors[loginErr] || "Sign-in failed."); history.replaceState(null, "", "/login"); }
