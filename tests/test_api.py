from fastapi.testclient import TestClient

from app.main import create_app


def client(env):
    return TestClient(create_app(env.settings, env.service))


def test_participant_flow(env):
    c = client(env)
    assert c.get("/").status_code == 200
    opts = c.get("/api/options").json()
    assert any(i["key"] == "NIFTY50" for i in opts["instruments"])
    r = c.post("/api/recommend", json={"instrument": "RELIANCE", "horizon": "1w"})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["instrument"] == "RELIANCE"


def test_bad_symbol_rejected(env):
    r = client(env).post("/api/recommend", json={"instrument": "../etc", "horizon": "1d"})
    assert r.status_code == 422


def test_participant_cannot_force_refresh(env):
    c = client(env)
    c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d"})
    c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d", "force_refresh": True})
    assert len(env.client.calls) == 1
    c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d", "force_refresh": True},
           headers={"X-Admin-Token": "secret"})
    assert len(env.client.calls) == 2


def test_admin_requires_token(env):
    c = client(env)
    assert c.get("/api/admin/settings").status_code == 401
    assert c.get("/api/admin/settings", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_admin_switch_model(env, tmp_path):
    c = client(env)
    h = {"X-Admin-Token": "secret"}
    r = c.put("/api/admin/settings", json={"active_model": "claude-opus-5-5", "effort": "high"}, headers=h)
    assert r.status_code == 200
    assert c.get("/api/admin/settings", headers=h).json()["settings"]["active_model"] == "claude-opus-5-5"
    assert c.put("/api/admin/settings", json={"active_model": "gpt-4"}, headers=h).status_code == 422
    c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d"})
    assert env.client.calls[-1]["model"] == "claude-opus-5-5"
    usage = c.get("/api/admin/usage", headers=h).json()
    assert usage["by_model"][0]["model"] == "claude-opus-5-5"
