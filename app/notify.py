"""AISensy WhatsApp notifications and the daily 6 AM job.

AISensy setup (dashboard): create a WhatsApp template, e.g.
    "Good morning {{1}}! Today's suggested strategy: {{2}} on {{3}}. {{4}} Open the app for the full analysis."
then create an *API campaign* that uses it, set it Live, and put its name in Admin -> Notifications.
Template params are sent in this order: [first name, strategy name, run on, one-line reason].
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import requests

from .auth import mask_phone
from .data.providers import now_ist
from .llm.recommender import NO_TRADE
from .daily import DailyAdvisor
from .service import today_ist

log = logging.getLogger(__name__)
AISENSY_URL = "https://backend.aisensy.com/campaign/t1/api/v2"
SEND_WINDOW = timedelta(hours=2)   # don't fire a 6 AM message at noon after a late deploy


def _param(text: str, limit: int = 120) -> str:
    # WhatsApp rejects template params with newlines, tabs or 4+ consecutive spaces.
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit] or "-"


class AISensyClient:
    def __init__(self, api_key: str | None = None, url: str = AISENSY_URL):
        self.api_key = api_key if api_key is not None else os.environ.get("AISENSY_API_KEY", "")
        self.url = url

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def send(self, *, campaign: str, phone: str, user_name: str, params: list[str],
             buttons: list[dict] | None = None) -> tuple[bool, str]:
        if not self.configured:
            return False, "AISENSY_API_KEY not set"
        if not campaign:
            return False, "AiSensy campaign name not set in admin settings"
        body = {
            "apiKey": self.api_key,
            "campaignName": campaign,
            "destination": phone,          # digits with country code, e.g. 919876543210
            "userName": _param(user_name, 60),
            "templateParams": [_param(p) for p in params],
            "source": "strategy-recommender",
        }
        if buttons:
            body["buttons"] = buttons
        try:
            resp = requests.post(self.url, json=body, timeout=20)
        except requests.RequestException as exc:
            return False, f"network error: {type(exc).__name__}"
        if resp.ok:
            return True, f"HTTP {resp.status_code}"
        # Never echo the request (it holds the API key); keep only the provider's message.
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def send_login_code(client: "AISensyClient", campaign: str, phone: str, name: str, code: str) -> tuple[bool, str]:
    """Send a WhatsApp login code. Authentication templates usually carry a "Copy code" URL button that
    needs the code too; if the template has no button, AiSensy rejects that, so retry without it."""
    button = [{"type": "button", "sub_type": "url", "index": 0, "parameters": [{"type": "text", "text": code}]}]
    ok, detail = client.send(campaign=campaign, phone=phone, user_name=name, params=[code], buttons=button)
    if ok or detail.startswith(("network error", "AISENSY_API_KEY", "AiSensy campaign")):
        return ok, detail
    ok2, detail2 = client.send(campaign=campaign, phone=phone, user_name=name, params=[code])
    return ok2, detail2 if ok2 else f"{detail} | without button: {detail2}"


def message_params(user: dict, payload: dict) -> list[str]:
    rec = payload["recommendation"]
    first = (user["name"] or "there").split()[0]
    if rec["top_strategy_id"] == NO_TRADE:
        return [first, "No strategy (sit out today)", "-", rec.get("whatsapp_line") or "Conditions are unclear."]
    name = payload["strategies"].get(rec["top_strategy_id"], {}).get("name", rec["top_strategy_id"])
    return [first, name, rec.get("run_on") or payload["meta"].get("benchmark", "-"), rec.get("whatsapp_line") or ""]


class DailyJobs:
    def __init__(self, advisor: DailyAdvisor, aisensy: AISensyClient | None = None):
        self.advisor = advisor
        self.store = advisor.store
        self.settings = advisor.settings
        self.aisensy = aisensy or AISensyClient()

    # ---- schedule ---------------------------------------------------------
    def is_notify_day(self, now: datetime) -> bool:
        admin = self.settings.admin
        if admin.notify_weekdays_only and now.weekday() >= 5:
            return False
        return now.strftime("%Y-%m-%d") not in set(admin.market_holidays)

    def due(self, now: datetime) -> list[str]:
        admin = self.settings.admin
        if not admin.notify_enabled or not self.is_notify_day(now):
            return []
        hh, mm = map(int, admin.notify_time.split(":"))
        send_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        prepare_at = send_at - timedelta(minutes=admin.prepare_lead_minutes)
        jobs = []
        if prepare_at <= now < send_at:
            jobs.append("prepare")
        if send_at <= now < send_at + SEND_WINDOW:
            jobs.append("send")
        return jobs

    def tick(self, now: datetime | None = None) -> list[str]:
        """Run whatever is due; the DB lock makes each job run once per day."""
        now = now or now_ist()
        ran = []
        for kind in self.due(now):
            if self.store.claim_job(kind, now.strftime("%Y-%m-%d")):
                ran.append(kind)
                self.run(kind)
        return ran

    # ---- jobs -------------------------------------------------------------
    def run(self, kind: str) -> dict:
        day = today_ist()
        try:
            summary = self.prepare() if kind == "prepare" else self.send_all()
        except Exception:
            log.exception("daily job %s failed", kind)
            self.store.release_job(kind, day)   # allow a retry on the next tick
            raise
        self.store.finish_job(kind, day, summary)
        log.info("daily job %s done: %s", kind, summary)
        return summary

    def prepare(self) -> dict:
        """Pre-generate the day's pick (it is the same for every participant)."""
        users = self.store.opted_in_users()
        try:
            self.advisor.today()
            return {"participants": len(users), "prepared": True}
        except Exception as exc:
            log.warning("prepare failed: %s", type(exc).__name__)
            return {"participants": len(users), "prepared": False, "error": type(exc).__name__}

    def send_all(self) -> dict:
        day = today_ist()
        users = self.store.opted_in_users()
        counts = {"participants": len(users), "sent": 0, "skipped": 0, "failed": 0}
        campaign = self.settings.admin.aisensy_campaign
        payload, error = None, None
        if users:
            try:
                payload = self.advisor.today()
            except Exception as exc:
                error = f"error: {type(exc).__name__}"
                log.warning("no pick to send: %s", type(exc).__name__)
        for user in users:
            if self.store.notified_today(user["id"], day):
                counts["skipped"] += 1
                continue
            try:
                if payload is None:
                    raise RuntimeError(error)
                ok, detail = self.aisensy.send(campaign=campaign, phone=user["phone"], user_name=user["name"],
                                               params=message_params(user, payload))
            except Exception as exc:
                ok, detail = False, error or f"error: {type(exc).__name__}"
            self.store.log_notification(user["id"], day, "sent" if ok else "failed", detail)
            counts["sent" if ok else "failed"] += 1
            if not ok:
                log.warning("notification failed for %s: %s", mask_phone(user["phone"]), detail)
        return counts

    def send_test(self, phone: str) -> tuple[bool, str]:
        params = ["Admin", "Test strategy", "NIFTY 50", "This is a test message from the strategy recommender."]
        ok, detail = self.aisensy.send(campaign=self.settings.admin.aisensy_campaign, phone=phone,
                                       user_name="Admin test", params=params)
        self.store.log_notification(None, today_ist(), "test-sent" if ok else "test-failed", detail)
        return ok, detail


async def scheduler_loop(jobs: DailyJobs, interval_s: float = 30.0) -> None:
    """Background loop started with the web app. Railway: keep 1 replica (the DB lock also guards)."""
    log.info("notification scheduler started")
    while True:
        try:
            await asyncio.to_thread(jobs.tick)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval_s)
