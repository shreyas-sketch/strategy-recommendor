"""WhatsApp login codes sent through the AiSensy login-code campaign."""

import re

from app.notify import AISensyClient, send_login_code


def last_code(web):
    return web.aisensy.sent[-1]["params"][0]


def test_code_login_flow(env, web):
    store = env.service.store
    uid = store.create_user("Asha Rao", "919000000001")
    c = web.client()
    assert c.get("/api/auth/config").json()["whatsapp_code"] is True
    r = c.post("/api/auth/code/request", json={"phone": "90000 00001"})
    assert r.status_code == 200
    msg = web.aisensy.sent[-1]
    assert msg["campaign"] == "hub_login_code_" and msg["phone"] == "919000000001"
    code = last_code(web)
    assert re.fullmatch(r"\d{6}", code) and msg["buttons"][0]["parameters"][0]["text"] == code
    assert c.post("/api/auth/code/verify", json={"phone": "9000000001", "code": "000000" if code != "000000" else "111111"}).status_code == 401
    assert c.post("/api/auth/code/verify", json={"phone": "9000000001", "code": code}).status_code == 200
    assert c.get("/api/me").json()["name"] == "Asha Rao"
    # single use
    assert web.client().post("/api/auth/code/verify", json={"phone": "9000000001", "code": code}).status_code == 401
    assert store.get_user(uid)["last_login"]


def test_unknown_number_gets_same_answer_and_no_message(env, web):
    c = web.client()
    sent_before = len(web.aisensy.sent)
    r = c.post("/api/auth/code/request", json={"phone": "9111111111"})
    assert r.status_code == 200 and "If this number is registered" in r.json()["message"]
    assert len(web.aisensy.sent) == sent_before


def test_code_locks_after_wrong_attempts(env, web):
    env.service.store.create_user("Asha", "919000000001")
    c = web.client()
    c.post("/api/auth/code/request", json={"phone": "9000000001"})
    code = last_code(web)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        c.post("/api/auth/code/verify", json={"phone": "9000000001", "code": wrong})
    assert c.post("/api/auth/code/verify", json={"phone": "9000000001", "code": code}).status_code == 401


def test_code_requests_rate_limited_per_phone(env, web):
    env.service.store.create_user("Asha", "919000000001")
    c = web.client()
    codes = [c.post("/api/auth/code/request", json={"phone": "9000000001"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_disabled_without_campaign(env, web):
    env.settings.update_admin({"aisensy_otp_campaign": ""})
    c = web.client()
    assert c.get("/api/auth/config").json()["whatsapp_code"] is False
    assert c.post("/api/auth/code/request", json={"phone": "9000000001"}).status_code == 503


def test_retry_without_button_when_template_has_none(monkeypatch):
    calls = []

    class R:
        def __init__(self, ok, text=""):
            self.ok, self.status_code, self.text = ok, 200 if ok else 400, text

    def fake_post(url, json, timeout):
        calls.append(json)
        return R("buttons" not in json, "Template has no buttons")

    import app.notify as notify
    monkeypatch.setattr(notify.requests, "post", fake_post)
    ok, _ = send_login_code(AISensyClient(api_key="k"), "hub_login_code_", "919000000001", "A", "123456")
    assert ok and len(calls) == 2 and calls[0]["templateParams"] == ["123456"] and "buttons" not in calls[1]


def test_admin_test_code_endpoint(env, web):
    r = web.admin().post("/api/admin/notifications/test-code", json={"phone": "9000000001"})
    assert r.json()["ok"] is True and web.aisensy.sent[-1]["params"] == ["123456"]
