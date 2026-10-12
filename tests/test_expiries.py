from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.data.base import ProviderError
from app.expiries import ExpiryCalendar, schedule_check

IST = ZoneInfo("Asia/Kolkata")
FIX = Path(__file__).parent / "fixtures" / "tradetron"
PRO = {"risk_tolerance": "high", "experience": "advanced", "capital_inr": 1_000_000, "fno_enabled": True}
CALENDAR = {   # NIFTY weekly on Tuesdays, SENSEX weekly on Thursdays, BANKNIFTY monthly only
    "NIFTY": ["2026-10-06", "2026-10-13", "2026-10-20", "2026-10-27", "2026-11-03", "2026-11-24"],
    "SENSEX": ["2026-10-08", "2026-10-15", "2026-10-22", "2026-10-29", "2026-11-26"],
    "BANKNIFTY": ["2026-09-29", "2026-10-27", "2026-11-24"],
}


class FakeGroww:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def fetch_expiries(self, exchange, underlying, year, month=None):
        self.calls.append((exchange, underlying, year))
        if self.fail:
            raise ProviderError("down")
        return CALENDAR.get(underlying, [])


def clock(d):
    return lambda: datetime.fromisoformat(d).replace(hour=6, tzinfo=IST)


@pytest.fixture
def cal(env):
    groww = FakeGroww()
    c = ExpiryCalendar(env.settings, groww=groww, clock=clock("2026-10-16"))
    env.service.calendar = c
    return c


def test_info_weekly_monthly_and_trading_days(env, cal):
    n = cal.info("NIFTY50")
    assert n["next_expiry"] == "2026-10-20" and n["next_expiry_type"] == "weekly"
    assert n["calendar_days_to_expiry"] == 4 and n["trading_days_to_expiry"] == 2   # Fri -> Mon, Tue
    assert n["next_monthly_expiry"] == "2026-10-27" and n["has_weekly_expiries"]
    b = cal.info("BANKNIFTY")
    assert b["next_expiry_type"] == "monthly" and not b["has_weekly_expiries"]
    env.settings.update_admin({"market_holidays": ["2026-10-19"]})
    assert cal.info("NIFTY50")["trading_days_to_expiry"] == 1
    assert cal.info("RELIANCE") is None                       # no F&O mapping


def test_fetched_once_per_day_and_cached_on_disk(env):
    groww = FakeGroww()
    c1 = ExpiryCalendar(env.settings, groww=groww, clock=clock("2026-10-16"))
    c1.info("NIFTY50"); c1.info("NIFTY50")
    assert len(groww.calls) == 1
    other = FakeGroww()
    c2 = ExpiryCalendar(env.settings, groww=other, clock=clock("2026-10-16"))   # e.g. after a restart
    assert c2.info("NIFTY50")["next_expiry"] == "2026-10-20" and other.calls == []
    # Groww down the next day -> yesterday's list is still used
    c3 = ExpiryCalendar(env.settings, groww=FakeGroww(fail=True), clock=clock("2026-10-17"))
    assert c3.info("NIFTY50")["next_expiry"] == "2026-10-20"


def _install(web):
    a = web.admin()
    for name in ("bnf_supertrend", "atm_roll", "kc_trend_ride"):
        d = a.post("/api/admin/strategies/import",
                   files={"file": ("f.json", (FIX / f"{name}.json").read_bytes(), "application/json")}).json()["draft"]
        assert a.post("/api/admin/strategies", json=d).status_code == 201


def test_schedule_check(env, web):
    _install(web)
    atm = env.settings.strategies["atm_roll_aggressive_diversified_kapoor"]
    bnf = env.settings.strategies["bnf_15_min_super_trend_intraday_instant"]
    assert atm.active_days == "expiry_and_day_before" and bnf.active_days == "any"
    fri = ExpiryCalendar(env.settings, groww=FakeGroww(), clock=clock("2026-10-16"))
    why = schedule_check(atm, fri)
    assert "doesn't trade today" in why and "NIFTY 50 2026-10-20 (Tuesday)" in why
    assert schedule_check(atm, ExpiryCalendar(env.settings, groww=FakeGroww(), clock=clock("2026-10-19"))) is None  # day before NIFTY
    assert schedule_check(atm, ExpiryCalendar(env.settings, groww=FakeGroww(), clock=clock("2026-10-22"))) is None  # SENSEX expiry
    assert schedule_check(atm, None) is None and schedule_check(bnf, fri) is None


def test_daily_pick_drops_strategy_on_non_trading_day(env, web, cal):
    _install(web)
    c = web.participant()
    c.put("/api/me/profile", json=PRO)
    rec = c.get("/api/me/today").json()
    excluded = {e["strategy_id"]: e for e in rec["recommendation"]["excluded"]}
    assert excluded["atm_roll_aggressive_diversified_kapoor"]["kind"] == "schedule"
    assert "atm_roll_aggressive_diversified_kapoor" not in {r["strategy_id"] for r in rec["recommendation"]["rankings"]}
    assert rec["expiries"]["BANKNIFTY"]["next_expiry"] == "2026-10-27"
    msg = env.client.calls[-1]["messages"][0]["content"]
    assert '"expiry_calendar"' in msg and '"is_day_before_expiry"' in msg


def test_daily_pick_keeps_strategy_on_expiry_day(env, web):
    _install(web)
    env.service.calendar = ExpiryCalendar(env.settings, groww=FakeGroww(), clock=clock("2026-10-19"))
    c = web.participant()
    c.put("/api/me/profile", json=PRO)
    rec = c.get("/api/me/today").json()["recommendation"]
    assert "atm_roll_aggressive_diversified_kapoor" not in {e["strategy_id"] for e in rec["excluded"]}


def test_market_tool_expiry_block_and_schedule(env, web, cal):
    _install(web)
    out = web.participant().post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1d"}).json()
    assert out["snapshot"]["expiry"]["next_expiry"] == "2026-10-20"
    assert [e["strategy_id"] for e in out["recommendation"]["excluded"]] == ["atm_roll_aggressive_diversified_kapoor"]
    assert '"expiry"' in env.client.calls[-1]["messages"][0]["content"]
    # weekly horizon: schedule is not applied to today
    week = web.participant("B", "9000000002").post("/api/recommend", json={"instrument": "NIFTY50", "horizon": "1w"}).json()
    assert "atm_roll_aggressive_diversified_kapoor" in {r["strategy_id"] for r in week["recommendation"]["rankings"]}


def test_admin_expiry_endpoints(env, web, cal):
    a = web.admin()
    status = a.get("/api/admin/expiries").json()
    nifty = next(r for r in status["underlyings"] if r["key"] == "NIFTY50")
    assert nifty["info"]["next_expiry"] == "2026-10-20"
    assert a.post("/api/admin/expiries/refresh").status_code == 200


def test_groww_fetch_expiries_parses_dates(monkeypatch):
    from app.data import providers

    class R:
        ok, status_code = True, 200

        def json(self):
            return {"status": "SUCCESS", "payload": {"expiries": ["2026-10-20", "2026-10-13", "2026-10-20"]}}

    captured = {}

    def fake_get(url, headers, params, timeout):
        captured.update(url=url, params=params)
        return R()

    monkeypatch.setenv("GROWW_ACCESS_TOKEN", "t")
    monkeypatch.setattr(providers.requests, "get", fake_get)
    out = providers.GrowwProvider().fetch_expiries("NSE", "NIFTY", 2026)
    assert out == ["2026-10-13", "2026-10-20"]
    assert captured["url"].endswith("/historical/expiries")
    assert captured["params"] == {"exchange": "NSE", "underlying_symbol": "NIFTY", "year": "2026"}


def test_run_on_forced_to_strategys_index(env, web, cal):
    _install(web)
    for sid in [k for k, st in env.settings.strategies.items() if not st.underlyings]:
        env.settings.delete_strategy(sid)
    c = web.participant()
    c.put("/api/me/profile", json=PRO)
    c.post("/api/portfolio", files={"file": ("p.csv", b"symbol,quantity\nRELIANCE,10\n", "text/csv")})
    rec = c.get("/api/me/today").json()["recommendation"]      # fake model says run_on="RELIANCE"
    assert rec["top_strategy_id"].startswith(("bnf_", "kc_")) and rec["run_on"] == "NIFTY BANK"
