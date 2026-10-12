$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("error").classList.add("hidden");
  try {
    await api("/api/auth/login", { method: "POST", json: { phone: $("phone").value, password: $("password").value } });
    location.href = "/";
  } catch (err) { showError($("error"), err); }
});

function showCodeForm(on) {
  $("code-form").classList.toggle("hidden", !on);
  $("login-form").classList.toggle("hidden", on);
}
fetch("/api/auth/config").then((r) => r.json()).then((c) => {
  $("google-box").classList.toggle("hidden", !c.google);
  $("use-code-wrap").classList.toggle("hidden", !c.whatsapp_code);
  if (c.whatsapp_code) showCodeForm(true);
}).catch(() => {});
$("use-password").onclick = (e) => { e.preventDefault(); showCodeForm(false); };
$("use-code").onclick = (e) => { e.preventDefault(); showCodeForm(true); };

let codeSent = false;
$("code-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("error").classList.add("hidden");
  $("code-btn").disabled = true;
  try {
    if (!codeSent) {
      const r = await api("/api/auth/code/request", { method: "POST", json: { phone: $("code-phone").value } });
      codeSent = true;
      $("code-msg").textContent = r.message; $("code-msg").classList.remove("hidden");
      $("code-step").classList.remove("hidden"); $("code").required = true; $("code").focus();
      $("code-btn").textContent = "Sign in";
    } else {
      await api("/api/auth/code/verify", { method: "POST", json: { phone: $("code-phone").value, code: $("code").value } });
      location.href = "/";
    }
  } catch (err) { showError($("error"), err); }
  finally { $("code-btn").disabled = false; }
});
$("code-phone").addEventListener("input", () => {   // changing the number restarts the flow
  if (!codeSent) return;
  codeSent = false; $("code-step").classList.add("hidden"); $("code").required = false;
  $("code-msg").classList.add("hidden"); $("code-btn").textContent = "Get a WhatsApp code";
});
const loginErrors = { not_registered: "That Google account isn't registered. Ask the admin to add your Gmail address.",
                      google_failed: "Google sign-in failed — please try again.",
                      google_not_configured: "Google sign-in isn't available yet — use your mobile number." };
const loginErr = new URLSearchParams(location.search).get("error");
if (loginErr) { showError($("error"), loginErrors[loginErr] || "Sign-in failed."); history.replaceState(null, "", "/login"); }
