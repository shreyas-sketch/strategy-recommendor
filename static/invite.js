// The invite token lives in the URL fragment, which browsers never send to servers.
const token = location.hash.slice(1);
history.replaceState(null, "", "/invite");   // keep it out of history and screenshots
if (!token) showError($("error"), "This invite link is incomplete. Ask the admin to send it again.");

$("invite-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("error").classList.add("hidden");
  if ($("pw1").value !== $("pw2").value) return showError($("error"), "Passwords do not match");
  try {
    await api("/api/auth/accept-invite", { method: "POST", json: { token, password: $("pw1").value } });
    location.href = "/";
  } catch (err) { showError($("error"), err); }
});
