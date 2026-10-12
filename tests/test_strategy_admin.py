H = {}
NEW = {
    "id": "vwap_pullback", "name": "VWAP Pullback", "category": "trend", "instruments": ["equity"],
    "horizons": ["1d"], "description": "Buy pullbacks to VWAP in an uptrend.",
    "logic": "Long when price retests VWAP from above with RSI > 50.",
    "ideal_conditions": {"trend": ["up"], "trend_strength": ["moderate", "strong"], "volatility": ["normal"],
                         "squeeze": "any"},
    "avoid_conditions": ["Sideways days"], "risk_notes": "Fails on trend reversals.",
}


def test_create_update_delete_strategy(env, web):
    assert web.client().post("/api/admin/strategies", json=NEW).status_code == 401
    c = web.admin()

    r = c.post("/api/admin/strategies", json=NEW, headers=H)
    assert r.status_code == 201, r.text
    path = env.settings.strategy_dir / "vwap_pullback.yaml"
    assert path.exists()
    assert "vwap_pullback" in {s["id"] for s in web.participant().get("/api/options").json()["strategies"]}
    assert c.post("/api/admin/strategies", json=NEW, headers=H).status_code == 409  # duplicate id

    # used by the next recommendation
    c.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d"})
    assert '"vwap_pullback"' in env.client.calls[-1]["system"][0]["text"]

    # rename via update: old file removed, new file written
    r = c.put("/api/admin/strategies/vwap_pullback", json={**NEW, "id": "vwap_pullback_v2", "name": "VWAP v2"},
              headers=H)
    assert r.status_code == 200, r.text
    assert not path.exists()
    assert env.settings.strategies["vwap_pullback_v2"].name == "VWAP v2"

    assert c.delete("/api/admin/strategies/vwap_pullback_v2", headers=H).status_code == 200
    assert "vwap_pullback_v2" not in env.settings.strategies
    assert c.delete("/api/admin/strategies/nope", headers=H).status_code == 404


def test_update_existing_example_keeps_its_file(env, web):
    c = web.admin()
    current = c.get("/api/admin/strategies", headers=H).json()["strategies"]
    ema = next(s for s in current if s["id"] == "ema_trend_follow")
    r = c.put("/api/admin/strategies/ema_trend_follow", json={**ema, "logic": "New rules"}, headers=H)
    assert r.status_code == 200
    assert env.settings.strategies["ema_trend_follow"].logic == "New rules"
    assert len(list(env.settings.strategy_dir.glob("ema_trend_follow*"))) == 1


def test_invalid_strategy_rejected(env, web):
    c = web.admin()
    assert c.post("/api/admin/strategies", json={**NEW, "id": "Bad Id!"}, headers=H).status_code == 422
    assert c.post("/api/admin/strategies", json={**NEW, "horizons": []}, headers=H).status_code == 422
    assert c.post("/api/admin/strategies", json={**NEW, "category": "magic"}, headers=H).status_code == 422
