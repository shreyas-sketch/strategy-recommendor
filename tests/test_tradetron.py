from pathlib import Path

import pytest

from app.config import Strategy
from app.tradetron import TradetronError, analyse, load_export

FIX = Path(__file__).parent / "fixtures" / "tradetron"
PRO = {"risk_tolerance": "high", "experience": "advanced", "capital_inr": 1_000_000, "fno_enabled": True}


def draft(name):
    return analyse(load_export((FIX / f"{name}.json").read_bytes())[0])


def test_supertrend_option_buyer():
    r = draft("bnf_supertrend")
    d = r["draft"]
    Strategy(**d)
    assert d["underlyings"] == ["BANKNIFTY"] and d["instruments"] == ["options"]
    assert d["category"] == "trend" and d["risk_level"] == "medium" and d["min_capital_inr"] == 100000
    assert "BUY 1 lot NIFTY BANK CE ATM+2 strike" in d["logic"] and "PE ATM-2" in d["logic"]
    assert "Supertrend(10, 3)" in d["logic"] and "closed by 15:14" in d["logic"]
    assert "the time is before 09:30" in d["logic"]


def test_atm_roll_straddle_seller():
    d = draft("atm_roll")["draft"]
    assert d["underlyings"] == ["NIFTY50", "SENSEX"] and d["category"] == "volatility_selling"
    assert d["risk_level"] == "high" and d["min_experience"] == "advanced" and d["min_capital_inr"] == 600000
    assert d["ideal_conditions"]["trend"] == ["sideways"]
    assert "combined premium of NIFTY 50 current week ATM CE + NIFTY 50 current week ATM PE" in d["logic"]
    assert "days from today to the NIFTY 50 current weekly expiry equals 1" in d["logic"]
    assert "2 set(s) with 2 lot(s) per leg" in d["logic"]


def test_kc_runtime_signals_flagged():
    r = draft("kc_trend_ride")
    assert r["draft"]["underlyings"] == ["BANKNIFTY"] and r["draft"]["risk_level"] == "high"
    assert any("BANKNIFTY_key1" in w for w in r["warnings"])
    assert "SELL 1 lot NIFTY BANK PE ATM" in r["draft"]["logic"]


def test_rejects_other_formats():
    with pytest.raises(TradetronError):
        analyse({"_format": "something-else"})
    with pytest.raises(TradetronError):
        load_export(b"not json")


def test_import_endpoint_review_then_save(env, web):
    a = web.admin()
    r = a.post("/api/admin/strategies/import", files={"file": ("x.json", (FIX / "bnf_supertrend.json").read_bytes(), "application/json")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["existing_id"] is None and body["draft"]["source"]["format"] == "tt-strategy/v2"
    assert "bnf_15_min_super_trend_intraday_instant" not in env.settings.strategies      # not saved yet
    assert list((env.settings.data_dir / "imports").glob("*.json"))
    d = body["draft"]
    d["ideal_conditions"]["volatility"] = ["high"]                                       # admin review
    assert a.post("/api/admin/strategies", json=d).status_code == 201
    # re-import: logic refreshed, reviewed fields kept
    r2 = a.post("/api/admin/strategies/import", files={"file": ("x.json", (FIX / "bnf_supertrend.json").read_bytes(), "application/json")}).json()
    assert r2["existing_id"] == d["id"] and r2["draft"]["ideal_conditions"]["volatility"] == ["high"]
    assert a.post("/api/admin/strategies/import", files={"file": ("x.json", b"{}", "application/json")}).status_code == 422


def _install_tradetron(env, web):
    a = web.admin()
    for name in ("bnf_supertrend", "atm_roll", "kc_trend_ride"):
        d = a.post("/api/admin/strategies/import",
                   files={"file": ("f.json", (FIX / f"{name}.json").read_bytes(), "application/json")}).json()["draft"]
        assert a.post("/api/admin/strategies", json=d).status_code == 201


def test_market_tool_only_judges_strategies_on_their_index(env, web):
    _install_tradetron(env, web)
    c = web.participant()
    bank = c.post("/api/recommend", json={"instrument": "BANKNIFTY", "horizon": "1d"}).json()
    ids = {r["strategy_id"] for r in bank["recommendation"]["rankings"]}
    assert "atm_roll_aggressive_diversified_kapoor" not in ids
    assert {"bnf_15_min_super_trend_intraday_instant", "kc_trend_ride_nifty_bank_option_selling"} <= ids
    # an index no strategy trades -> only generic (example) strategies; none at all -> clear message
    for sid in list(env.settings.strategies):
        if not env.settings.strategies[sid].underlyings:
            env.settings.delete_strategy(sid)
    r = c.post("/api/recommend", json={"instrument": "RELIANCE", "horizon": "1d"})
    assert r.status_code == 422 and "BANKNIFTY" in r.json()["detail"]


def test_daily_pick_uses_each_strategys_index(env, web):
    _install_tradetron(env, web)
    for sid in [s for s, st in env.settings.strategies.items() if not st.underlyings]:
        env.settings.delete_strategy(sid)
    a = web.participant("Asha", "9000000001")
    a.put("/api/me/profile", json=PRO)
    out = a.get("/api/me/today").json()
    assert set(out["markets"]) == {"BANKNIFTY", "NIFTY50", "SENSEX"}
    msg = env.client.calls[-1]["messages"][0]["content"]
    assert '"underlying_markets"' in msg and '"weekday"' in msg and '"portfolio": null' in msg
    # same profile, no portfolio -> the AI answer is shared
    b = web.participant("Bilal", "9000000002")
    b.put("/api/me/profile", json=PRO)
    calls = len(env.client.calls)
    shared = b.get("/api/me/today").json()
    assert len(env.client.calls) == calls and shared["meta"]["shared"] is True
    assert shared["recommendation"]["top_strategy_id"] == out["recommendation"]["top_strategy_id"]


def test_rule_engine_scores_on_own_underlying(env, web):
    from app.analysis.regime import rule_scores
    _install_tradetron(env, web)
    up = {"trend": "up", "trend_strength": "strong", "volatility": "normal", "squeeze": False, "label": "strong up"}
    flat = {"trend": "sideways", "trend_strength": "weak", "volatility": "low", "squeeze": False, "label": "sideways"}
    markets = {"BANKNIFTY": {"label": "NIFTY BANK", "regime": up}, "NIFTY50": {"label": "NIFTY 50", "regime": flat},
               "SENSEX": {"label": "SENSEX", "regime": flat}}
    strategies = [s for s in env.settings.strategies.values() if s.underlyings]
    scores = {s["strategy_id"]: s for s in rule_scores(None, strategies, markets)}
    assert scores["bnf_15_min_super_trend_intraday_instant"]["rule_score"] == 100     # trend on BANKNIFTY
    assert scores["atm_roll_aggressive_diversified_kapoor"]["rule_score"] == 100      # calm NIFTY + SENSEX
    assert scores["atm_roll_aggressive_diversified_kapoor"]["on"] == "NIFTY 50, SENSEX"


def test_options_list_only_traded_indices(env, web):
    _install_tradetron(env, web)
    for sid in [k for k, st in env.settings.strategies.items() if not st.underlyings]:
        env.settings.delete_strategy(sid)
    opts = web.participant().get("/api/options").json()
    assert opts["any_symbol"] is False
    assert [i["key"] for i in opts["instruments"]] == ["NIFTY50", "BANKNIFTY", "SENSEX"]
    bank = next(i for i in opts["instruments"] if i["key"] == "BANKNIFTY")
    assert len(bank["strategies"]) == 2
