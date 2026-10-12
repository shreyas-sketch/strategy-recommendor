from app.config import CHEAPEST_MODEL


def test_claude_path_records_cost_and_sanitizes(env):
    out = env.service.recommend("NIFTY50", "1d")
    rec = out["recommendation"]
    ids = [r["strategy_id"] for r in rec["rankings"]]
    assert "made_up" not in ids
    assert set(ids) == {s.id for s in env.settings.strategies_for("1d")}
    assert out["meta"]["engine"] == "claude"
    # Sonnet 5.5 default: 3000*2 + 1000*10 per 1M
    assert abs(out["meta"]["cost_usd"] - 0.016) < 1e-9
    call = env.client.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["fallbacks"] == "default"
    assert call["output_config"] == {"effort": "medium"}


def test_haiku_has_no_server_fallback(env):
    env.settings.update_admin({"active_model": CHEAPEST_MODEL})
    env.service.recommend("NIFTY50", "1w")
    assert "fallbacks" not in env.client.calls[-1]


def test_cache_reuses_result(env):
    env.service.recommend("NIFTY50", "1m")
    out = env.service.recommend("NIFTY50", "1m")
    assert out["meta"]["cached"] is True
    assert len(env.client.calls) == 1
    env.service.recommend("NIFTY50", "1m", force_refresh=True)
    assert len(env.client.calls) == 2


def test_budget_downgrade(env):
    env.settings.update_admin({"monthly_budget_usd": 0.01, "cache_minutes": {"1d": 0, "1w": 0, "1m": 0}})
    env.service.recommend("NIFTY50", "1d")           # spends 0.016 on Sonnet
    out = env.service.recommend("NIFTY50", "1d")
    assert out["meta"]["model"] == CHEAPEST_MODEL
    assert env.client.calls[-1]["model"] == CHEAPEST_MODEL


def test_budget_rules_only(env):
    env.settings.update_admin({"monthly_budget_usd": 0.01, "on_budget_exceeded": "rules_only",
                               "cache_minutes": {"1d": 0, "1w": 0, "1m": 0}})
    env.service.recommend("NIFTY50", "1d")
    out = env.service.recommend("NIFTY50", "1d")
    assert out["meta"]["engine"] == "rules"
    assert len(env.client.calls) == 1


def test_refusal_falls_back_to_rules(env):
    env.client.stop_reason = "refusal"
    out = env.service.recommend("NIFTY50", "1d")
    assert out["meta"]["engine"] == "rules"
    assert out["recommendation"]["rankings"]


def test_claude_comparison_fields_sanitized(env):
    rec = env.service.recommend("NIFTY50", "1d")["recommendation"]
    assert rec["why_top_over_others"]
    assert all("made_up" not in f["favors"] for f in rec["deciding_factors"])
    assert all(r["vs_top"] for r in rec["rankings"])


def test_rules_fallback_explains_comparison(env):
    env.settings.update_admin({"monthly_budget_usd": 0.0001, "on_budget_exceeded": "rules_only"})
    env.service.store.record_usage(model="x", served_model="x", instrument="N", horizon="1d", input_tokens=0,
                                   output_tokens=0, cache_write_tokens=0, cache_read_tokens=0,
                                   cost_usd=1, request_id=None)
    rec = env.service.recommend("NIFTY50", "1w")["recommendation"]  # synthetic uptrend
    assert rec["top_strategy_id"] == "ema_trend_follow"
    assert "Runner-up" in rec["why_top_over_others"]
    others = [r for r in rec["rankings"] if r["strategy_id"] != "ema_trend_follow"]
    assert all("vs" in r["vs_top"] for r in others)
    assert any("ema_trend_follow" in f["favors"] for f in rec["deciding_factors"])
