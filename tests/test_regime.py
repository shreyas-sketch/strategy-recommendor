from app.analysis.regime import build_snapshot, rule_scores
from conftest import make_candles


def test_trend_detected(env):
    snap = build_snapshot(make_candles("trend_up"), None, "1m")
    assert snap["regime"]["trend"] == "up"
    assert snap["regime"]["trend_strength"] in ("strong", "moderate")
    scores = rule_scores(snap["regime"], env.settings.strategies_for("1m"))
    assert scores[0]["strategy_id"] == "ema_trend_follow"


def test_range_detected(env):
    snap = build_snapshot(make_candles("range"), None, "1m")
    assert snap["regime"]["trend"] == "sideways"
    scores = {s["strategy_id"]: s["rule_score"] for s in rule_scores(snap["regime"], env.settings.strategies_for("1w"))}
    assert scores["bb_mean_reversion"] > scores["ema_trend_follow"]


def test_volatility_spike_detected():
    snap = build_snapshot(make_candles("volatile"), None, "1m")
    assert snap["regime"]["volatility"] in ("high", "extreme")


def test_snapshot_is_json_safe():
    import json
    snap = build_snapshot(make_candles("range", freq="15min"), make_candles("range"), "1d")
    json.dumps(snap, allow_nan=False)
    assert snap["daily_context"] is not None
