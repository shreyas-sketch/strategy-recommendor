"""Master admin, sub-admins, Google sign-in (pre-approved emails only), email-only participants."""

import base64
import json
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import oauth
from app.main import create_app

MASTER_PW = "master-password-123"


def make_app(env, web, monkeypatch, **envvars):
    for k, v in envvars.items():
        monkeypatch.setenv(k, v)
    return create_app(env.settings, env.service, jobs=web.jobs, start_scheduler=False)


def client(app, **headers):
    return TestClient(app, headers={"X-Requested-With": "fetch", **headers}, follow_redirects=False)


@pytest.fixture
def master_app(env, web, monkeypatch):
    return make_app(env, web, monkeypatch, MASTER_ADMIN_EMAIL="Boss@Example.com", MASTER_ADMIN_PASSWORD=MASTER_PW)


def login_master(app):
    c = client(app)
    assert c.post("/api/admin/auth/login", json={"email": "boss@example.com", "password": MASTER_PW}).status_code == 200
    return c


def test_master_bootstrap_and_login(env, master_app):
    c = client(master_app)
    assert c.get("/api/admin/me").status_code == 401
    assert c.post("/api/admin/auth/login", json={"email": "boss@example.com", "password": "nope"}).status_code == 401
    c = login_master(master_app)
    me = c.get("/api/admin/me").json()
    assert me["role"] == "master" and me["email"] == "boss@example.com"
    cookie = c.post("/api/admin/auth/login", json={"email": "boss@example.com", "password": MASTER_PW}).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.put("/api/admin/settings", json={"effort": "low"}).status_code == 200
    c.post("/api/admin/auth/logout")
    assert c.get("/api/admin/me").status_code == 401


def test_bootstrap_does_not_overwrite_changed_password(env, web, monkeypatch, master_app):
    c = login_master(master_app)
    assert c.post("/api/admin/me/password", json={"current_password": MASTER_PW,
                                                  "new_password": "brand-new-password-1"}).status_code == 200
    app2 = make_app(env, web, monkeypatch, MASTER_ADMIN_EMAIL="boss@example.com", MASTER_ADMIN_PASSWORD=MASTER_PW)
    assert client(app2).post("/api/admin/auth/login", json={"email": "boss@example.com", "password": MASTER_PW}).status_code == 401
    app3 = make_app(env, web, monkeypatch, MASTER_ADMIN_PASSWORD_RESET="1")      # explicit recovery
    assert client(app3).post("/api/admin/auth/login", json={"email": "boss@example.com", "password": MASTER_PW}).status_code == 200


def test_weak_master_password_refused(env, web, monkeypatch):
    with pytest.raises(RuntimeError, match="at least 12"):
        make_app(env, web, monkeypatch, MASTER_ADMIN_EMAIL="a@b.co", MASTER_ADMIN_PASSWORD="short")


def test_sub_admin_permissions(master_app):
    m = login_master(master_app)
    r = m.post("/api/admin/admins", json={"email": "helper@example.com", "name": "Helper"})
    assert r.status_code == 201 and "/admin/invite#" in r.json()["invite_url"]
    token = r.json()["invite_url"].split("#")[1]
    sub = client(master_app)
    assert sub.post("/api/admin/auth/accept-invite", json={"token": token, "password": "short-pw"}).status_code == 422
    assert sub.post("/api/admin/auth/accept-invite", json={"token": token, "password": "helper-password-1"}).status_code == 200
    assert sub.get("/api/admin/me").json()["role"] == "admin"
    # allowed: participants and strategies
    assert sub.post("/api/admin/participants", json={"name": "P", "phone": "9000000011"}).status_code == 201
    assert sub.get("/api/admin/strategies").status_code == 200
    assert sub.get("/api/admin/settings").status_code == 200
    # master-only: model/budget, admins, WhatsApp sends
    assert sub.put("/api/admin/settings", json={"active_model": "claude-opus-5-5"}).status_code == 403
    assert sub.get("/api/admin/admins").status_code == 403
    assert sub.post("/api/admin/jobs/send/run").status_code == 403
    assert sub.post("/api/admin/notifications/test", json={"phone": "9000000011"}).status_code == 403
    # master can disable the sub-admin, which ends their session; master itself can't be removed
    admins = m.get("/api/admin/admins").json()["admins"]
    sub_id = next(a["id"] for a in admins if a["role"] == "admin")
    master_id = next(a["id"] for a in admins if a["role"] == "master")
    assert m.patch(f"/api/admin/admins/{sub_id}", json={"active": False}).status_code == 200
    assert sub.get("/api/admin/me").status_code == 401
    assert m.delete(f"/api/admin/admins/{master_id}").status_code == 409


def test_admin_token_still_works_as_master(master_app):
    t = client(master_app, **{"X-Admin-Token": "secret"})
    assert t.get("/api/admin/me").json()["role"] == "master"
    assert t.get("/api/admin/admins").status_code == 200


# ---- Google ------------------------------------------------------------------
def id_token(**claims) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    box = {}

    class R:
        ok = True

        def json(self):
            return {"id_token": id_token(**box["claims"])}

    def fake_post(url, data, timeout):
        box["posted"] = (url, data)
        return R()

    monkeypatch.setattr(oauth.requests, "post", fake_post)
    return box


def google_login(app, box, role="participant", **override):
    c = client(app)
    r = c.get(f"/auth/google/start?role={role}")
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith(oauth.AUTH_URL) and q["redirect_uri"] == ["http://testserver/auth/google/callback"]
    claims = {"aud": "cid.apps.googleusercontent.com", "iss": "https://accounts.google.com",
              "exp": time.time() + 300, "nonce": q["nonce"][0], "email": "Asha@Gmail.com", "email_verified": True}
    claims.update(override.pop("claims", {}))
    box["claims"] = claims
    state = override.pop("state", q["state"][0])
    return c, c.get(f"/auth/google/callback?code=abc&state={state}")


def test_google_login_preapproved_participant(env, web, google):
    a = web.admin()
    r = a.post("/api/admin/participants", json={"name": "Asha", "email": "asha@gmail.com"})
    assert r.status_code == 201 and r.json()["invite_url"] is None        # Google users need no invite link
    c, cb = google_login(web.app, google)
    assert cb.status_code == 303 and cb.headers["location"] == "/"
    me = c.get("/api/me").json()
    assert me["name"] == "Asha" and me["email"] == "asha@gmail.com" and me["phone"] is None
    assert google["posted"][1]["grant_type"] == "authorization_code" and google["posted"][1]["code"] == "abc"


def test_google_refuses_unknown_or_bad_tokens(env, web, google):
    web.admin().post("/api/admin/participants", json={"name": "Asha", "email": "asha@gmail.com"})
    c, cb = google_login(web.app, google, claims={"email": "stranger@gmail.com"})
    assert cb.headers["location"] == "/login?error=not_registered" and c.get("/api/me").status_code == 401
    for bad in ({"claims": {"aud": "someone-else"}}, {"claims": {"email_verified": False}},
                {"claims": {"nonce": "forged"}}, {"claims": {"exp": time.time() - 10}}, {"state": "tampered"}):
        c, cb = google_login(web.app, google, **bad)
        assert cb.headers["location"] == "/login?error=google_failed", bad
        assert c.get("/api/me").status_code == 401


def test_google_admin_login(env, web, google, monkeypatch):
    app = make_app(env, web, monkeypatch, MASTER_ADMIN_EMAIL="asha@gmail.com")   # Google-only master (no password)
    c, cb = google_login(app, google, role="admin")
    assert cb.headers["location"] == "/admin"
    assert c.get("/api/admin/me").json()["role"] == "master"
    c2, cb2 = google_login(app, google, role="admin", claims={"email": "nobody@gmail.com"})
    assert cb2.headers["location"] == "/admin?error=not_admin" and c2.get("/api/admin/me").status_code == 401


def test_google_not_configured(web, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    r = client(web.app).get("/auth/google/start")
    assert r.headers["location"] == "/login?error=google_not_configured"
    assert client(web.app).get("/api/auth/config").json() == {"google": False}


# ---- participants with optional phone/email ------------------------------------
def test_participant_contact_rules(env, web):
    a = web.admin()
    assert a.post("/api/admin/participants", json={"name": "Nobody"}).status_code == 422
    assert a.post("/api/admin/participants", json={"name": "X", "email": "not-an-email"}).status_code == 422
    r = a.post("/api/admin/participants", json={"name": "Both", "phone": "9000000021", "email": "both@gmail.com"}).json()
    assert r["invite_url"] and r["email"] == "both@gmail.com"
    assert a.post("/api/admin/participants", json={"name": "Dup", "email": "BOTH@gmail.com"}).status_code == 409
    g = a.post("/api/admin/participants", json={"name": "G", "email": "g@gmail.com"}).json()
    assert a.post(f"/api/admin/participants/{g['id']}/invite").status_code == 409
    assert a.patch(f"/api/admin/participants/{g['id']}", json={"email": ""}).status_code == 422   # would leave no contact
    assert a.patch(f"/api/admin/participants/{g['id']}", json={"phone": "9000000022"}).status_code == 200
    imp = a.post("/api/admin/participants/import",
                 json={"csv_text": "name,phone,email\nA,,a1@gmail.com\nB,9000000023,\nC,,\n"}).json()
    assert len(imp["created"]) == 2 and imp["errors"][0]["line"] == 4


def test_whatsapp_skips_people_without_phone(env, web):
    store = env.service.store
    uid = store.create_user("Mail only", None, "m@gmail.com")
    store.update_user(uid, notify_opt_in=1)
    assert all(u["id"] != uid for u in store.opted_in_users())


def test_migrates_old_users_table(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
            password_hash TEXT, active INTEGER NOT NULL DEFAULT 1, notify_opt_in INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL, last_login TEXT);
        INSERT INTO users (name, phone, created) VALUES ('Old', '919000000001', '2026-01-01');
    """)
    con.commit(); con.close()
    from app.store import Store
    s = Store(db)
    assert s.get_user_by_phone("919000000001")["name"] == "Old"
    uid = s.create_user("New", None, "new@gmail.com")
    s.save_portfolio(uid, [{"symbol": "INFY", "quantity": 1, "avg_price": None}], [], "x")   # FKs still work
    assert s.get_portfolio(uid)["holdings"][0]["symbol"] == "INFY"
