"""Research module: imports, storage, features (no look-ahead), engine, validation and admin API.

All P&L here is synthetic test data generated in the tests themselves.
"""

import io
import zipfile
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.research.backtest import regimes_for, walk_forward
from app.research.engine import EngineConfig, attach_regimes, build_panel, score_day
from app.research.features import build_features, next_session
from app.research.ingest import IngestError, parse_dates, parse_market, parse_strategy_pnl
from app.research.service import DuplicateImport, ResearchService

TV_HEADER = ("time,open,high,low,close,VWAP,Up Trend,Down Trend,EMA,Volume,RSI,RSI-based MA,"
             "Regular Bullish,Regular Bullish Label,Regular Bearish,Regular Bearish Label")


def trading_days(start: str, n: int) -> list[date]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def tv_minutes(days: list[date], seed: int = 3, muhurat: date | None = None, partial_last: bool = False) -> bytes:
    """A TradingView-style 1-minute export (IST offsets, Supertrend split into Up/Down columns)."""
    rng = np.random.default_rng(seed)
    lines, price = [TV_HEADER], 25000.0
    for i, d in enumerate(days):
        start = pd.Timestamp(f"{d} 09:15", tz="Asia/Kolkata")
        n = 375
        if partial_last and i == len(days) - 1:
            n = 57
        stamps = [start + pd.Timedelta(minutes=k) for k in range(n)]
        if muhurat and d == muhurat:
            stamps = [pd.Timestamp(f"{d} 13:45", tz="Asia/Kolkata") + pd.Timedelta(minutes=k) for k in range(60)]
        cum_pv = cum_v = 0.0
        for t in stamps:
            o = price
            price = max(1000.0, price * (1 + rng.normal(0, 0.0006)))
            c = price
            h, l = max(o, c) + abs(rng.normal(0, 3)), min(o, c) - abs(rng.normal(0, 3))
            v = float(rng.integers(300_000, 900_000))
            cum_pv += (h + l + c) / 3 * v
            cum_v += v
            up = c - 30 if rng.random() > 0.5 else ""
            down = "" if up != "" else c + 30
            lines.append(f"{t.isoformat()},{o:.2f},{h:.2f},{l:.2f},{c:.2f},{cum_pv / cum_v:.4f},{up},{down},"
                         f"{c - 2:.2f},{v:.0f},{50 + rng.normal(0, 8):.3f},{50:.3f},,,,")
    return ("\n".join(lines) + "\n").encode()


def daily_csv(days: list[date], seed: int = 5) -> bytes:
    rng = np.random.default_rng(seed)
    c = 24000 * np.exp(np.cumsum(rng.normal(0, 0.008, len(days))))
    o = c * (1 + rng.normal(0, 0.002, len(days)))
    df = pd.DataFrame({"time": [d.isoformat() for d in days], "open": o, "high": np.maximum(o, c) * 1.004,
                       "low": np.minimum(o, c) * 0.996, "close": c})
    return df.to_csv(index=False).encode()


@pytest.fixture
def research(env):
    return ResearchService(env.settings)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
def test_parse_tradingview_minutes_flags_special_and_partial_sessions():
    days = trading_days("2025-10-20", 4)
    raw = tv_minutes(days, muhurat=days[1], partial_last=True)
    r = parse_market(raw, "NSE_NIFTY.csv")
    assert r["interval"] == "1m"
    assert r["sessions"] == 4
    assert r["mapping"]["st_up"] == "Up Trend" and r["mapping"]["vwap"] == "VWAP"
    flags = {s["day"]: s["flag"] for s in r["flagged_sessions"]}
    assert flags == {days[1].isoformat(): "special", days[3].isoformat(): "partial"}
    assert any("Empty column(s) ignored: Regular Bullish" in w for w in r["warnings"])
    first = r["rows"][0]
    assert first[1] == days[0].isoformat()
    assert pd.Timestamp(first[0], unit="s", tz="UTC").tz_convert("Asia/Kolkata").strftime("%H:%M") == "09:15"
    assert {row[9] for row in r["rows"]} <= {1, -1}            # Supertrend direction from Up/Down columns


def test_parse_market_rejects_other_intervals_and_missing_columns():
    with pytest.raises(IngestError, match="Missing required"):
        parse_market(b"time,open,close\n2025-01-01,1,2\n", "x.csv")
    rows = "\n".join(f"2025-01-01T09:{15 + 5 * i:02d}:00+05:30,1,2,0.5,1.5" for i in range(5))
    with pytest.raises(IngestError, match="5 minutes apart"):
        parse_market(f"time,open,high,low,close\n{rows}\n".encode(), "x.csv")


def test_parse_dates_resolves_day_first_and_month_first():
    assert parse_dates(pd.Series(["13-01-2026", "02-01-2026"]))[0].tolist() == ["2026-01-13", "2026-01-02"]
    assert parse_dates(pd.Series(["01/13/2026", "01/02/2026"]))[0].tolist() == ["2026-01-13", "2026-01-02"]
    out, note = parse_dates(pd.Series(["03/04/2026"]))
    assert out.tolist() == ["2026-04-03"] and "ambiguous" in note


def test_parse_pnl_daily_trade_log_and_net_with_charges():
    daily = "Date,P&L\n13-01-2026,\"₹1,250.50\"\n14-01-2026,(300)\n"
    r = parse_strategy_pnl(daily.encode(), "d.csv")
    assert r["groups"][None] == [("2026-01-13", 1250.5, None, None), ("2026-01-14", -300.0, None, None)]
    assert "estimated costs" in r["basis"]

    log = ("Strategy,Exit Time,Net P&L,Charges\n"
           "Nifty St adx i,2026-01-13 10:05,100,20\nNifty St adx i,2026-01-13 14:00,-50,20\n"
           "Index Intraday Basket,2026-01-13 11:00,400,10\n")
    r = parse_strategy_pnl(log.encode(), "log.csv")
    assert r["trade_level"]
    assert r["groups"]["Nifty St adx i"] == [("2026-01-13", 90.0, 40.0, 2)]     # gross = net + charges
    assert r["groups"]["Index Intraday Basket"] == [("2026-01-13", 410.0, 10.0, 1)]


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def test_import_preserves_original_and_never_duplicates(research):
    days = trading_days("2025-08-25", 3)
    raw = tv_minutes(days)
    r = research.import_file(raw, "nifty.csv", "market", "NIFTY50", "tester")
    assert r["added"] == 3 * 375 and r["skipped"] == 0
    stored = research.db.raw_dir / research.db.list_imports()[0]["stored_as"]
    assert stored.read_bytes() == raw                                   # byte-for-byte original

    with pytest.raises(DuplicateImport):
        research.import_file(raw, "renamed.csv", "market", "NIFTY50", "tester")

    # overlapping export: the same 3 days plus 2 new ones -> only the new bars are added
    more = tv_minutes(days + trading_days(days[-1].isoformat(), 3)[1:])
    r2 = research.import_file(more, "nifty_more.csv", "market", "NIFTY50", "tester")
    assert r2["added"] == 2 * 375 and r2["skipped"] == 3 * 375 and r2["conflicting"] == 0
    assert research.db.market_summary()[0]["bars"] == 5 * 375


def test_conflicting_rows_are_kept_unless_replace(research):
    research.import_file(b"Date,P&L\n2026-01-13,100\n2026-01-14,50\n", "a.csv", "strategy_pnl", "nifty_st_adx_i", "t")
    r = research.import_file(b"Date,P&L\n2026-01-14,75\n2026-01-15,10\n", "b.csv", "strategy_pnl", "nifty_st_adx_i", "t")
    assert (r["added"], r["conflicting"]) == (1, 1)
    assert research.db.one("SELECT gross_pnl FROM strategy_days WHERE day = '2026-01-14'")["gross_pnl"] == 50
    research.import_file(b"Date,P&L\n2026-01-14,75\n", "c.csv", "strategy_pnl", "nifty_st_adx_i", "t", replace=True)
    assert research.db.one("SELECT gross_pnl FROM strategy_days WHERE day = '2026-01-14'")["gross_pnl"] == 75


def test_unknown_strategy_names_save_nothing(research):
    with pytest.raises(IngestError, match="Mystery Algo"):
        research.import_file(b"Strategy,Date,P&L\nMystery Algo,2026-01-13,5\n", "x.csv", "strategy_pnl", None, "t")
    assert research.db.list_imports() == []
    assert list(research.db.raw_dir.iterdir()) == []


def test_seeded_strategies_match_tradetron_reports(research):
    s = {x["id"]: x for x in research.db.list_strategies()}
    assert set(s) == {"index_intraday_basket", "nifty_st_adx_i", "banknifty_1_2_intraday",
                      "breakout_buy_options_v1_nifty", "atm_roll_aggressive_diversified"}
    assert s["breakout_buy_options_v1_nifty"]["margin"] == 50000
    assert s["index_intraday_basket"]["underlying"] == "SENSEX"


def test_backup_restore_roundtrip(research):
    research.import_file(b"Date,P&L\n2026-01-13,100\n", "a.csv", "strategy_pnl", "nifty_st_adx_i", "t")
    blob = research.backup()
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert "research.db" in names and any(n.startswith("raw/") for n in names)
    research.import_file(b"Date,P&L\n2026-01-14,5\n", "b.csv", "strategy_pnl", "nifty_st_adx_i", "t")
    research.restore(blob)
    assert research.db.one("SELECT COUNT(*) AS n FROM strategy_days")["n"] == 1
    with pytest.raises(ValueError):
        research.restore(b"not a zip")
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("research.db", b"x")
        zf.writestr("BACKUP.json", b"{}")
        zf.writestr("raw/../../etc/passwd", b"x")
    with pytest.raises(ValueError, match="Unexpected file"):
        research.restore(evil.getvalue())


# ---------------------------------------------------------------------------
# features and leakage
# ---------------------------------------------------------------------------
def test_features_and_regimes_use_only_past_data(research):
    days = trading_days("2025-01-01", 160)
    research.import_file(daily_csv(days), "daily.csv", "market", "NIFTY50", "t")
    f1 = build_features(research.db, "NIFTY50")
    assert {"trending / high-vol", "range-bound / low-vol"} & set(f1["regime"])
    # change the last 20 days drastically: nothing before them may change
    rng = np.random.default_rng(99)
    changed = [(float(rng.uniform(10000, 40000)), d.isoformat()) for d in days[-20:]]
    for close, day in changed:
        research.db.write("UPDATE market_bars SET close = ?, high = ? * 1.01, low = ? * 0.99 WHERE day = ?",
                          (close, close, close, day))
    f2 = build_features(research.db, "NIFTY50")
    cut = days[-21].isoformat()
    cols = ["ret_1d", "atr14_pct", "eff10", "regime", "trend_threshold", "vol_threshold"]
    pd.testing.assert_frame_equal(f1.loc[:cut, cols], f2.loc[:cut, cols])


def test_minute_features_skip_special_and_partial_sessions(research):
    days = trading_days("2025-10-01", 30)
    research.import_file(tv_minutes(days, muhurat=days[10], partial_last=True), "m.csv", "market", "NIFTY50", "t")
    f = build_features(research.db, "NIFTY50")
    assert days[10].isoformat() not in f.index and days[-1].isoformat() not in f.index
    assert f["st_flips"].notna().any() and f["close_vs_vwap_pct"].notna().any() and f["relvol"].notna().any()
    assert next_session(days[-2].isoformat(), set()) == days[-1].isoformat()


def _panel(strategies, rets_by_sid, days, features=None):
    rows = [{"strategy_id": sid, "day": d, "gross_pnl": float(r), "charges": 0.0, "trades": 1}
            for sid, rets in rets_by_sid.items() for d, r in zip(days, rets)]
    panel = build_panel(strategies, rows)
    return attach_regimes(panel, strategies, features or {}, "NIFTY50")


STRATS = [{"id": "a", "name": "A", "underlying": "NIFTY50", "margin": 100000, "cost_per_active_day": 0},
          {"id": "b", "name": "B", "underlying": "NIFTY50", "margin": 100000, "cost_per_active_day": 0}]


def test_walk_forward_has_no_lookahead():
    days = [d.isoformat() for d in trading_days("2025-01-01", 120)]
    rng = np.random.default_rng(1)
    rets = {"a": rng.normal(200, 1000, 120), "b": rng.normal(100, 1000, 120)}
    cfg = EngineConfig(bootstrap=200)
    base = walk_forward(_panel(STRATS, rets, days), STRATS, {}, "NIFTY50", cfg, permutations=200)
    rets2 = {k: v.copy() for k, v in rets.items()}
    rets2["b"][-30:] = 50_000                                         # the future is wildly different
    alt = walk_forward(_panel(STRATS, rets2, days), STRATS, {}, "NIFTY50", cfg, permutations=200)
    n_same = len(base["log"]) - 30
    for x, y in zip(base["log"][:n_same], alt["log"][:n_same]):
        if x["day"] <= days[-31]:
            assert x["picks"] == y["picks"], x["day"]


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
def _score(panel, target, engine="B_regime_shrunk", **cfg):
    ctx = {s["id"]: {"regime": "unknown", "direction": "unknown", "symbol": None, "row": None} for s in STRATS}
    return score_day(panel, STRATS, target, ctx, engine, EngineConfig(**cfg), sorted(panel["day"].unique()))


def test_engine_picks_the_better_risk_adjusted_strategy():
    days = [d.isoformat() for d in trading_days("2025-01-01", 200)]
    rng = np.random.default_rng(2)
    panel = _panel(STRATS, {"a": rng.normal(800, 1500, 200), "b": rng.normal(-100, 1500, 200)}, days)
    res = _score(panel, "2025-12-31")
    assert res["top_strategy"] == "a"
    a = next(x for x in res["ranking"] if x["id"] == "a")
    assert a["low"] < a["expected"] < a["high"] and 0 < a["p_win"] < 1


def test_engine_abstains_when_nothing_is_reliably_positive():
    days = [d.isoformat() for d in trading_days("2025-01-01", 200)]
    rng = np.random.default_rng(3)
    panel = _panel(STRATS, {"a": rng.normal(-50, 2000, 200), "b": rng.normal(-200, 2000, 200)}, days)
    res = _score(panel, "2025-12-31")
    assert res["top_strategy"] is None and res["also_qualify"] == []


def test_high_win_rate_with_bad_tail_does_not_rank_first():
    days = [d.isoformat() for d in trading_days("2025-01-01", 200)]
    a = np.where(np.arange(200) % 10 == 0, -9000.0, 600.0)        # 90% winners, -360/day on average
    b = np.where(np.arange(200) % 2 == 0, 1500.0, -900.0)         # 50% winners, +300/day
    res = _score(_panel(STRATS, {"a": a, "b": b}, days), "2025-12-31")
    assert res["top_strategy"] == "b"


def test_insufficient_history_and_weekday_filter():
    days = [d.isoformat() for d in trading_days("2025-01-01", 100)]
    rets_a = np.array([0.0 if pd.Timestamp(d).weekday() == 4 else 500.0 for d in days])   # never trades on Friday
    panel = _panel(STRATS, {"a": rets_a, "b": np.full(100, 300.0)}, days)
    friday = next(d for d in (pd.Timestamp(days[-1]) + pd.Timedelta(days=i) for i in range(1, 8))
                                if d.weekday() == 4).strftime("%Y-%m-%d")
    res = _score(panel, friday)
    a = next(x for x in res["ranking"] if x["id"] == "a")
    assert a["status"] == "not_trading" and res["top_strategy"] == "b"
    short = _panel(STRATS, {"a": np.full(10, 500.0), "b": np.full(10, 500.0)}, days[:10])
    assert all(x["status"] == "insufficient_history" for x in _score(short, friday)["ranking"])


def test_shrinkage_pulls_thin_regimes_to_the_overall_average():
    days = [d.isoformat() for d in trading_days("2025-01-01", 200)]
    rng = np.random.default_rng(4)
    rets = rng.normal(100, 500, 200)
    panel = _panel(STRATS[:1], {"a": rets}, days)
    panel.loc[panel.index[-3:], "regime"] = "trending / high-vol"          # 3 days, each huge
    panel.loc[panel.index[-3:], "ret"] = 20_000.0
    ctx = {"a": {"regime": "trending / high-vol", "direction": "bullish", "symbol": "NIFTY50", "row": None}}
    res = score_day(panel, STRATS[:1], "2025-12-31", ctx, "B_regime_shrunk", EngineConfig(), list(days))
    a = res["ranking"][0]
    assert a["regime_days"] == 3 and a["regime_mean"] == 20_000
    assert a["expected"] < 0.2 * 20_000                                    # 3 lucky days do not dominate
    assert any("thin evidence" in n for n in a["notes"])


def test_costs_estimated_only_without_a_charges_column():
    strategies = [{"id": "a", "name": "A", "underlying": "NIFTY50", "margin": 200000, "cost_per_active_day": 100}]
    rows = [{"strategy_id": "a", "day": "2026-01-13", "gross_pnl": 1000, "charges": None, "trades": None},
            {"strategy_id": "a", "day": "2026-01-14", "gross_pnl": 0, "charges": None, "trades": None},
            {"strategy_id": "a", "day": "2026-01-15", "gross_pnl": 1000, "charges": 30, "trades": None}]
    p = build_panel(strategies, rows)
    assert p["net"].tolist() == [900, 0, 970]
    assert p["cost_estimated"].tolist() == [True, False, False]
    assert p["ret"].tolist() == pytest.approx([450, 0, 485])                           # per Rs 1 lakh of margin


# ---------------------------------------------------------------------------
# service end to end + API
# ---------------------------------------------------------------------------
def _load_history(research, n=140):
    days = trading_days("2025-03-03", n)
    research.import_file(daily_csv(days), "nifty_daily.csv", "market", "NIFTY50", "t")
    rng = np.random.default_rng(11)
    for sid, mu in (("nifty_st_adx_i", 400), ("breakout_buy_options_v1_nifty", -50)):
        df = pd.DataFrame({"Date": [d.strftime("%d-%m-%Y") for d in days], "P&L": rng.normal(mu, 1500, n).round(2)})
        research.import_file(df.to_csv(index=False).encode(), f"{sid}.csv", "strategy_pnl", sid, "t")
    return days


def test_daily_workflow_saves_recommendation_and_fills_actuals(research):
    days = _load_history(research)
    rec = research.latest_recommendation()
    assert rec["target_day"] == next_session(days[-1].isoformat(), set())
    assert rec["cutoff_day"] == days[-1].isoformat()
    payload = rec["payload"]
    assert {x["id"] for x in payload["ranking"]} >= {"nifty_st_adx_i", "atm_roll_aggressive_diversified"}
    assert payload["markets"][0]["symbol"] == "NIFTY50"
    assert any("Costs are estimated" in w for w in payload["warnings"])
    # the next session's P&L arrives -> the stored recommendation gets its actual result
    nxt = rec["target_day"]
    research.import_file(f"Date,P&L\n{nxt},1234\n".encode(), "next.csv", "strategy_pnl", "nifty_st_adx_i", "t")
    hist = research.history()
    row = next(r for r in hist["rows"] if r["target_day"] == nxt)
    assert row["actual"]["strategies"]["nifty_st_adx_i"]["net"] == pytest.approx(1234 - 121)
    bt = research.backtest()
    assert bt["days"] > 50 and set(bt["summary"]) >= {"B_regime_shrunk", "A_best_so_far", "equal_weight", "random"}
    rel = research.relationships()
    assert rel["count"] > 0 and all("q" in t for t in rel["tests"])


def test_research_api_requires_admin_and_handles_uploads(env, web):
    anon = web.client()
    assert anon.get("/api/admin/research/status").status_code == 401
    big = b"x" * 2_100_000
    r = anon.post("/api/admin/research/import", files={"file": ("a.csv", big)}, data={"kind": "market"},
                  headers={"X-Requested-With": "fetch"})
    assert r.status_code == 401                                        # large body refused before parsing

    c = web.admin()
    h = {"X-Requested-With": "fetch"}
    raw = tv_minutes(trading_days("2025-08-25", 2))
    r = c.post("/api/admin/research/import", files={"file": ("nifty.csv", raw)},
               data={"kind": "market", "target": "NIFTY50"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 750
    assert c.post("/api/admin/research/import", files={"file": ("again.csv", raw)},
                  data={"kind": "market", "target": "NIFTY50"}, headers=h).status_code == 409
    assert c.post("/api/admin/research/import", files={"file": ("x.pdf", b"%PDF")},
                  data={"kind": "market", "target": "NIFTY50"}, headers=h).status_code == 422
    s = c.get("/api/admin/research/status").json()
    assert s["usable_market_records"] == 750 and s["warnings"]
    for path in ("recommendation", "history", "backtest", "performance", "relationships", "market", "strategies",
                 "imports", "settings"):
        assert c.get(f"/api/admin/research/{path}").status_code == 200, path
    assert c.post("/api/admin/research/recommendation/run", headers=h).status_code == 200
    r = c.put("/api/admin/research/settings", json={"config": {"shrink_k": -5}}, headers=h)
    assert r.status_code == 422
    r = c.put("/api/admin/research/settings", json={"engine": "A_trailing60", "config": {"shrink_k": 30}}, headers=h)
    assert r.json()["engine"] == "A_trailing60" and r.json()["config"]["shrink_k"] == 30
    r = c.put("/api/admin/research/strategies/nifty_st_adx_i",
              json={"name": "Nifty St adx i", "underlying": "NIFTY50", "margin": 250000, "cost_per_active_day": 100}, headers=h)
    assert r.status_code == 200 and r.json()["strategy"]["margin"] == 250000
    backup = c.get("/api/admin/research/backup")
    assert backup.status_code == 200 and backup.headers["content-type"] == "application/zip"
    r = c.post("/api/admin/research/restore", files={"file": ("b.zip", backup.content)}, headers=h)
    assert r.status_code == 200, r.text
    assert c.get("/admin/research").status_code == 200


def test_excel_pnl_upload(research):
    buf = io.BytesIO()
    pd.DataFrame({"Date": ["2026-01-13", "2026-01-14"], "Gross P&L": [500.0, -200.0], "Charges": [40.0, 35.0]}) \
        .to_excel(buf, index=False)
    r = research.import_file(buf.getvalue(), "pnl.xlsx", "strategy_pnl", "nifty_st_adx_i", "t")
    assert r["added"] == 2 and "charges column" in r["basis"]
    assert research.db.one("SELECT charges FROM strategy_days WHERE day = '2026-01-14'")["charges"] == 35
