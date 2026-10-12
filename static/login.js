$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("error").classList.add("hidden");
  try {
    await api("/api/auth/login", { method: "POST", json: { phone: $("phone").value, password: $("password").value } });
    location.href = "/";
  } catch (err) { showError($("error"), err); }
});
