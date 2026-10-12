// The invite token lives in the URL fragment, which browsers never send to servers.
const token = location.hash.slice(1);
const forAdmin = location.pathname.startsWith("/admin");
history.replaceState(null, "", location.pathname);   // keep the token out of history and screenshots
if (forAdmin) document.querySelector("label[for=pw1]").textContent = "New password (min 12 characters)";
if (!token) showError($("error"), "This invite link is incomplete. Ask the admin to send it again.");

$("invite-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("error").classList.add("hidden");
  if ($("pw1").value !== $("pw2").value) return showError($("error"), "Passwords do not match");
  try {
    await api(forAdmin ? "/api/admin/auth/accept-invite" : "/api/auth/accept-invite",
              { method: "POST", json: { token, password: $("pw1").value } });
    location.href = forAdmin ? "/admin" : "/";
  } catch (err) { showError($("error"), err); }
});
