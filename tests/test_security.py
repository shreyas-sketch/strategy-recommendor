"""Data isolation, sessions, invites, CSRF and rate limiting."""

TEMPLATE = b"symbol,quantity,avg_price\nRELIANCE,10,2450\nINFY,5,1500\n"


def upload(c, data=TEMPLATE, name="p.csv"):
    return c.post("/api/portfolio", files={"file": (name, data, "text/csv")})


def test_participants_never_see_each_others_portfolio(web):
    a = web.participant("Asha", "9000000001")
    b = web.participant("Bilal", "9000000002")
    assert upload(a).status_code == 200
    assert b.get("/api/portfolio").json()["portfolio"] is None
    assert a.get("/api/portfolio").json()["portfolio"]["holdings"][0]["symbol"] == "INFY"
    # B deleting "their" portfolio cannot touch A's
    b.delete("/api/portfolio")
    assert a.get("/api/portfolio").json()["portfolio"] is not None
    # /api/me only describes the caller, with a masked phone
    me_b = b.get("/api/me").json()
    assert me_b["name"] == "Bilal" and me_b["has_portfolio"] is False
    assert "9000000002" not in me_b["phone"]


def test_personal_recommendation_is_per_user(env, web):
    a = web.participant("Asha", "9000000001")
    b = web.participant("Bilal", "9000000002")
    upload(a)
    ra = a.get("/api/me/today").json()
    rb = b.get("/api/me/today").json()
    assert ra["meta"]["has_portfolio"] is True and "context" in ra
    assert rb["meta"]["has_portfolio"] is False and "context" not in rb
    syms = {h["symbol"] for h in ra["context"]["holdings"]}
    assert syms == {"RELIANCE", "INFY"}


def test_claude_never_receives_name_or_phone(env, web):
    a = web.participant("Zubeida Unique", "9123456789")
    upload(a)
    a.get("/api/me/today")
    call = env.client.calls[-1]
    text = call["system"][0]["text"] + call["messages"][0]["content"]
    assert "Zubeida" not in text and "9123456789" not in text and "RELIANCE" in text


def test_admin_list_shows_counts_not_holdings(web):
    a = web.participant("Asha", "9000000001")
    upload(a)
    rows = web.admin().get("/api/admin/participants").json()["participants"]
    row = next(r for r in rows if r["name"] == "Asha")
    assert row["holdings_count"] == 2
    assert "holdings" not in row and "RELIANCE" not in str(rows)


def test_csrf_header_required(web):
    a = web.participant()
    from fastapi.testclient import TestClient
    bare = TestClient(web.app)
    bare.cookies = a.cookies
    assert bare.post("/api/auth/logout").status_code == 403
    assert bare.delete("/api/portfolio").status_code == 403


def test_session_cookie_flags(web):
    c = web.client()
    web.participant("X", "9000000003", "pass-word-123")
    r = c.post("/api/auth/login", json={"phone": "+91 90000 00003", "password": "pass-word-123"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_security_headers(web):
    r = web.client().get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert web.participant().get("/api/me").headers["cache-control"] == "no-store"


def test_deactivated_user_loses_access(web):
    a = web.participant()
    web.admin().patch(f"/api/admin/participants/{a.user_id}", json={"active": False})
    assert a.get("/api/me").status_code == 401


def test_deleted_user_data_is_gone(env, web):
    a = web.participant()
    upload(a)
    web.admin().delete(f"/api/admin/participants/{a.user_id}")
    assert env.service.store.get_portfolio(a.user_id) is None
    assert a.get("/api/me").status_code == 401


def test_invite_flow_single_use(web):
    admin = web.admin()
    r = admin.post("/api/admin/participants", json={"name": "Chen", "phone": "9000000009"})
    assert r.status_code == 201
    url = r.json()["invite_url"]
    assert "/invite#" in url
    token = url.split("#", 1)[1]
    c = web.client()
    assert c.post("/api/auth/accept-invite", json={"token": token, "password": "short"}).status_code == 422
    assert c.post("/api/auth/accept-invite", json={"token": token, "password": "long-enough-1"}).status_code == 200
    assert c.get("/api/me").json()["name"] == "Chen"
    # token cannot be reused
    assert web.client().post("/api/auth/accept-invite",
                             json={"token": token, "password": "another-pass-1"}).status_code == 400
    assert admin.post("/api/admin/participants", json={"name": "Dup", "phone": "9000000009"}).status_code == 409


def test_reinvite_revokes_old_password_and_sessions(web):
    a = web.participant("Asha", "9000000001", "old-password-1")
    url = web.admin().post(f"/api/admin/participants/{a.user_id}/invite").json()["invite_url"]
    assert a.get("/api/me").status_code == 401
    r = web.client().post("/api/auth/login", json={"phone": "9000000001", "password": "old-password-1"})
    assert r.status_code == 401
    assert url.split("#")[1]


def test_login_rate_limited(web):
    web.participant("Asha", "9000000001", "right-password-1")
    c = web.client()
    codes = [c.post("/api/auth/login", json={"phone": "9000000001", "password": "wrong"}).status_code
             for _ in range(12)]
    assert codes[0] == 401 and codes[-1] == 429


def test_bulk_import(web):
    csv_text = "name,phone\nA One,9000000101\nB Two,+91 90000 00102\nBad,123\n"
    r = web.admin().post("/api/admin/participants/import", json={"csv_text": csv_text}).json()
    assert len(r["created"]) == 2 and r["errors"][0]["line"] == 4
    assert all("/invite#" in x["invite_url"] for x in r["created"])


def test_per_user_analysis_quota(env, web):
    env.settings.update_admin({"per_user_daily_analyses": 2})
    c = web.participant()
    codes = [c.post("/api/recommend", json={"instrument": sym, "horizon": "1d"}).status_code
             for sym in ("AAA", "BBB", "CCC")]
    assert codes == [200, 200, 429]
    # cached results do not count
    assert c.post("/api/recommend", json={"instrument": "AAA", "horizon": "1d"}).status_code == 200


def test_upload_size_limit(web):
    c = web.participant()
    r = upload(c, b"symbol,quantity\n" + b"X,1\n" * 300_000)
    assert r.status_code in (413, 422)
