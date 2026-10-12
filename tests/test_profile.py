from app.profile import TradingProfile, apply_profile, capital_band, ineligibility, needs_fno, split

BEGINNER = {"risk_tolerance": "medium", "experience": "beginner", "capital_inr": 60000, "fno_enabled": False}
PRO = {"risk_tolerance": "high", "experience": "advanced", "capital_inr": 1_000_000, "fno_enabled": True}
CSV = b"symbol,quantity,avg_price\nRELIANCE,10,2450\nINFY,5,1500\n"


def test_ineligibility_reasons(env):
    straddle = env.settings.strategies["short_straddle"]
    reasons = ineligibility(straddle, TradingProfile(**BEGINNER))
    assert len(reasons) == 4 and any("F&O" in r for r in reasons) and any("high risk" in r for r in reasons) and any("₹3 lakh" in r for r in reasons)
    assert ineligibility(straddle, TradingProfile(**PRO)) == []
    assert needs_fno(straddle) and not needs_fno(env.settings.strategies["ema_trend_follow"])


def test_split_without_profile_keeps_everything(env):
    strategies = env.settings.strategies_for("1d")
    eligible, excluded = split(strategies, None)
    assert len(eligible) == len(strategies) and excluded == []
    eligible, excluded = split(strategies, TradingProfile(**BEGINNER))
    assert [s.id for s in eligible] == ["ema_trend_follow"]
    assert {e["strategy_id"] for e in excluded} == {"bb_mean_reversion", "long_strangle", "orb_breakout", "short_straddle"}


def test_capital_band():
    assert capital_band(60000) == "under ₹1 lakh" and capital_band(3_000_000) == "₹25 lakh–1 crore"


def test_apply_profile_repicks_and_explains():
    rec = {"top_strategy_id": "a", "confidence": "high", "why_top_over_others": "a wins",
           "rankings": [{"strategy_id": "a", "fit_score": 90, "verdict": "recommended", "why": "fits"},
                        {"strategy_id": "b", "fit_score": 70, "verdict": "conditional", "why": "ok too"}],
           "deciding_factors": [{"factor": "x", "favors": ["a", "b"], "hurts": []}]}
    out = apply_profile(rec, {"b"}, [{"strategy_id": "a", "reasons": ["high risk"]}], {"a": "Alpha", "b": "Beta"})
    assert out["top_strategy_id"] == "b" and "Alpha" in out["why_top_over_others"] and "high risk" in out["why_top_over_others"]
    assert [r["strategy_id"] for r in out["rankings"]] == ["b"] and out["deciding_factors"][0]["favors"] == ["b"]
    assert rec["top_strategy_id"] == "a"          # input (shared cache) untouched
    none_fit = apply_profile({**rec, "rankings": rec["rankings"][:1]}, set(), [{"strategy_id": "a", "reasons": ["x"]}], {})
    assert none_fit["top_strategy_id"] == "NO_TRADE"


def test_profile_endpoint_and_validation(web):
    c = web.participant()
    assert c.get("/api/me").json()["profile"] is None
    assert c.put("/api/me/profile", json={**BEGINNER, "risk_tolerance": "yolo"}).status_code == 422
    assert c.put("/api/me/profile", json={**BEGINNER, "capital_inr": -5}).status_code == 422
    assert c.put("/api/me/profile", json=BEGINNER).status_code == 200
    assert c.get("/api/me").json()["profile"]["experience"] == "beginner"


def test_daily_pick_respects_profile(env, web):
    c = web.participant()
    c.post("/api/portfolio", files={"file": ("p.csv", CSV, "text/csv")})
    c.put("/api/me/profile", json=BEGINNER)
    out = c.get("/api/me/today").json()
    rec = out["recommendation"]
    # the fake model picks the first catalog strategy (bb_mean_reversion) — not allowed for a beginner
    assert rec["top_strategy_id"] == "ema_trend_follow"
    assert [r["strategy_id"] for r in rec["rankings"]] == ["ema_trend_follow"]
    assert {e["strategy_id"] for e in rec["excluded"]} >= {"short_straddle", "bb_mean_reversion"}
    msg = env.client.calls[-1]["messages"][0]["content"]
    assert '"eligible_strategy_ids": [\n  "ema_trend_follow"\n ]' in msg
    assert '"capital_band": "under ₹1 lakh"' in msg and "60000" not in msg   # band, not the exact amount
    # the system prompt is the full catalog -> identical for every participant (prompt cache)
    assert '"short_straddle"' in env.client.calls[-1]["system"][0]["text"]


def test_profile_change_invalidates_todays_pick(env, web):
    c = web.participant()
    c.put("/api/me/profile", json=PRO)
    first = c.get("/api/me/today").json()
    assert first["recommendation"]["excluded"] == []
    c.put("/api/me/profile", json=BEGINNER)
    second = c.get("/api/me/today").json()
    assert second["recommendation"]["excluded"]


def test_nothing_eligible_means_sit_out_without_ai(env, web):
    c = web.participant()
    c.put("/api/me/profile", json={**BEGINNER, "capital_inr": 1000})
    calls = len(env.client.calls)
    rec = c.get("/api/me/today").json()["recommendation"]
    assert rec["top_strategy_id"] == "NO_TRADE" and len(rec["excluded"]) == len(env.settings.strategies_for("1d"))
    assert len(env.client.calls) == calls


def test_market_tool_overlay_is_per_person(env, web):
    beginner = web.participant("Asha", "9000000001")
    beginner.put("/api/me/profile", json=BEGINNER)
    other = web.participant("Bilal", "9000000002")           # no profile
    a = beginner.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1w"}).json()
    b = other.post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1w"}).json()
    assert len(env.client.calls) == 1                        # one shared AI call
    assert a["recommendation"]["top_strategy_id"] == "ema_trend_follow"
    assert "does not suit your profile" in a["recommendation"]["why_top_over_others"]
    assert b["recommendation"]["top_strategy_id"] == "bb_mean_reversion"
    assert b["recommendation"]["excluded"] == []


def test_admin_sets_strategy_suitability(env, web):
    a = web.admin()
    ema = next(s for s in a.get("/api/admin/strategies").json()["strategies"] if s["id"] == "ema_trend_follow")
    assert ema["min_capital_inr"] == 50000
    r = a.put("/api/admin/strategies/ema_trend_follow", json={**ema, "risk_level": "high", "min_experience": "advanced"})
    assert r.status_code == 200 and env.settings.strategies["ema_trend_follow"].risk_level == "high"
    assert a.put("/api/admin/strategies/ema_trend_follow", json={**ema, "risk_level": "extreme"}).status_code == 422
    rows = a.get("/api/admin/participants").json()["participants"]
    assert all("profile" not in r for r in rows)
