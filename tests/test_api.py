def test_participant_flow(env, web):
    c = web.participant()
    opts = c.get("/api/options").json()
    assert any(i["key"] == "NIFTY50" for i in opts["instruments"])
    r = c.post("/api/recommend", json={"instrument": "RELIANCE", "horizon": "1w"})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["instrument"] == "RELIANCE"


def test_pages_served(web):
    c = web.client()
    for path in ("/", "/login", "/invite", "/admin", "/healthz"):
        assert c.get(path).status_code == 200


def test_market_tool_requires_login(web):
    c = web.client()
    assert c.get("/api/options").status_code == 401
    assert c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d"}).status_code == 401


def test_bad_symbol_rejected(web):
    r = web.participant().post("/api/recommend", json={"instrument": "../etc", "horizon": "1d"})
    assert r.status_code == 422


def test_participant_cannot_force_refresh(env, web):
    c = web.participant()
    c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d"})
    c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d", "force_refresh": True})
    assert len(env.client.calls) == 1
    web.admin().post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d", "force_refresh": True})
    assert len(env.client.calls) == 2


def test_admin_requires_token(web):
    c = web.client()
    assert c.get("/api/admin/settings").status_code == 401
    assert c.get("/api/admin/settings", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_admin_switch_model(env, web):
    a = web.admin()
    r = a.put("/api/admin/settings", json={"active_model": "claude-opus-5-5", "effort": "high"})
    assert r.status_code == 200
    assert a.get("/api/admin/settings").json()["settings"]["active_model"] == "claude-opus-5-5"
    assert a.put("/api/admin/settings", json={"active_model": "gpt-4"}).status_code == 422
    web.participant().post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d"})
    assert env.client.calls[-1]["model"] == "claude-opus-5-5"
    usage = a.get("/api/admin/usage").json()
    assert usage["by_model"][0]["model"] == "claude-opus-5-5"
