from datetime import datetime
from zoneinfo import ZoneInfo

from app.notify import AISensyClient

IST = ZoneInfo("Asia/Kolkata")


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def enable(env, **kw):
    env.settings.update_admin({"notify_enabled": True, "aisensy_campaign": "daily_strategy", **kw})


def test_due_windows(env, web):
    enable(env)
    j = web.jobs
    mon = (2026, 10, 12)
    assert j.due(at(*mon, 5, 30)) == []
    assert j.due(at(*mon, 5, 40)) == ["prepare"]
    assert j.due(at(*mon, 6, 0)) == ["send"]
    assert j.due(at(*mon, 9, 0)) == []              # no catch-up after a late deploy
    assert j.due(at(2026, 10, 17, 6, 0)) == []      # Saturday
    env.settings.update_admin({"market_holidays": ["2026-10-12"]})
    assert j.due(at(*mon, 6, 0)) == []
    env.settings.update_admin({"notify_enabled": False, "market_holidays": []})
    assert j.due(at(*mon, 6, 0)) == []


def test_send_once_per_day_to_opted_in_only(env, web, monkeypatch):
    enable(env)
    a = web.participant("Asha Rao", "9000000001")
    web.participant("Bilal", "9000000002")          # not opted in
    a.put("/api/me/notifications", json={"opt_in": True})

    from app import notify, service
    monkeypatch.setattr(notify, "today_ist", lambda: "2026-10-12")
    monkeypatch.setattr(service, "today_ist", lambda: "2026-10-12")
    import app.daily as daily
    from datetime import date
    monkeypatch.setattr(daily, "trading_day", lambda holidays=(), now=None: date(2026, 10, 12))

    assert web.jobs.tick(at(2026, 10, 12, 6, 1)) == ["send"]
    assert web.jobs.tick(at(2026, 10, 12, 6, 2)) == []          # DB lock: once per day
    assert len(web.aisensy.sent) == 1
    msg = web.aisensy.sent[0]
    assert msg["phone"] == "919000000001" and msg["campaign"] == "daily_strategy"
    first, strategy, run_on, line = msg["params"]
    assert first == "Asha" and run_on == "NIFTY 50"
    assert "\n" not in line                                      # WhatsApp rejects newlines in params
    # manual re-run skips people already notified
    assert web.jobs.send_all()["skipped"] == 1


def test_admin_notification_endpoints(env, web):
    enable(env)
    a = web.admin()
    info = a.get("/api/admin/notifications").json()
    assert info["enabled"] and info["campaign"] == "daily_strategy"
    assert a.post("/api/admin/notifications/test", json={"phone": "9000000001"}).json()["ok"] is True
    assert a.post("/api/admin/jobs/send/run").status_code == 202
    assert a.post("/api/admin/jobs/nope/run").status_code == 404


def test_aisensy_payload(monkeypatch):
    captured = {}

    class R:
        ok, status_code, text = True, 200, "ok"

    def fake_post(url, json, timeout):
        captured.update(url=url, body=json)
        return R()

    import app.notify as notify
    monkeypatch.setattr(notify.requests, "post", fake_post)
    ok, _ = AISensyClient(api_key="k").send(campaign="c", phone="919000000001", user_name="A",
                                             params=["a\tb", "x    y", "", "z"])
    body = captured["body"]
    assert ok and captured["url"].endswith("/campaign/t1/api/v2")
    assert body["destination"] == "919000000001" and body["campaignName"] == "c"
    assert body["templateParams"] == ["a b", "x y", "-", "z"]
    assert AISensyClient(api_key="").send(campaign="c", phone="1", user_name="A", params=[])[0] is False
