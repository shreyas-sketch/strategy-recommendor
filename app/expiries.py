"""F&O expiry calendar from Groww, and "does this strategy trade today?" checks.

Expiry dates are fetched once per day per underlying and cached on disk, so restarts and the
6 AM job don't re-query Groww. Weekly vs monthly is derived from the dates themselves (the last
expiry of a calendar month is the monthly one), so exchange schedule changes are picked up
automatically.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from pathlib import Path

from .data.base import ProviderError, ProviderNotConfigured
from .data.providers import GrowwProvider, now_ist

log = logging.getLogger(__name__)

ACTIVE_DAY_LABELS = {
    "expiry_day": "only on expiry day",
    "expiry_and_day_before": "only on expiry day and the day before",
}


class ExpiryCalendar:
    def __init__(self, settings, groww: GrowwProvider | None = None, clock=now_ist, cache_path: Path | None = None):
        self.settings = settings
        self.groww = groww or GrowwProvider()
        self.clock = clock
        self.path = Path(cache_path or Path(settings.data_dir) / "expiries.json")
        self._lock = threading.Lock()
        self._cache: dict = self._load()

    # ---- storage --------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache, indent=1))
        tmp.replace(self.path)

    # ---- fetching -------------------------------------------------------
    def _fno(self, key: str) -> dict | None:
        inst = self.settings.instrument(key)
        return inst.get("fno")

    def dates(self, key: str, force: bool = False) -> list[date] | None:
        """Sorted expiry dates for this year (+ next year from November), refreshed daily. None if unavailable."""
        fno = self._fno(key)
        if not fno:
            return None
        today = self.clock().date()
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry.get("fetched") == today.isoformat() and not force:
                return [date.fromisoformat(d) for d in entry["dates"]]
            years = [today.year] + ([today.year + 1] if today.month >= 11 else [])
            try:
                found: set[str] = set()
                for y in years:
                    found.update(self.groww.fetch_expiries(fno["exchange"], fno["underlying"], y))
            except ProviderNotConfigured:
                return [date.fromisoformat(d) for d in entry["dates"]] if entry else None
            except (ProviderError, OSError, ValueError) as exc:
                log.warning("expiry fetch failed for %s: %s", key, exc)
                # keep serving yesterday's list rather than nothing
                return [date.fromisoformat(d) for d in entry["dates"]] if entry else None
            if not found:
                return [date.fromisoformat(d) for d in entry["dates"]] if entry else None
            self._cache[key] = {"fetched": today.isoformat(), "dates": sorted(found), "source": "groww"}
            self._save()
            return [date.fromisoformat(d) for d in sorted(found)]

    # ---- facts for a day -------------------------------------------------
    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days after `start` up to and including `end` (weekdays minus admin holidays)."""
        holidays = set(self.settings.admin.market_holidays)
        n, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if d.weekday() < 5 and d.isoformat() not in holidays:
                n += 1
        return n

    def info(self, key: str, today: date | None = None) -> dict | None:
        all_dates = self.dates(key)
        if not all_dates:
            return None
        today = today or self.clock().date()
        upcoming = [d for d in all_dates if d >= today]
        if not upcoming:
            return None
        monthly = {}
        for d in all_dates:
            monthly[(d.year, d.month)] = max(d, monthly.get((d.year, d.month), d))
        monthly_dates = set(monthly.values())
        nxt = upcoming[0]
        next_monthly = next((d for d in upcoming if d in monthly_dates), None)
        return {
            "next_expiry": nxt.isoformat(),
            "next_expiry_weekday": nxt.strftime("%A"),
            "next_expiry_type": "monthly" if nxt in monthly_dates else "weekly",
            "calendar_days_to_expiry": (nxt - today).days,
            "trading_days_to_expiry": self.trading_days_between(today, nxt),
            "is_expiry_day": nxt == today,
            "is_day_before_expiry": (nxt - today).days == 1,
            "next_monthly_expiry": next_monthly.isoformat() if next_monthly else None,
            "has_weekly_expiries": len(monthly_dates) < len(all_dates),
        }

    def status(self) -> dict:
        keys = [i["key"] for i in self.settings.instruments if i.get("fno")]
        rows = []
        for k in keys:
            entry = self._cache.get(k) or {}
            info = self.info(k)
            rows.append({"key": k, "label": self.settings.instrument(k)["label"], "fetched": entry.get("fetched"),
                         "count": len(entry.get("dates", [])), "info": info})
        return {"groww_configured": _groww_configured(), "underlyings": rows}

    def refresh(self) -> dict:
        for i in self.settings.instruments:
            if i.get("fno"):
                self.dates(i["key"], force=True)
        return self.status()


def _groww_configured() -> bool:
    import os
    return bool(os.environ.get("GROWW_ACCESS_TOKEN") or (os.environ.get("GROWW_API_KEY") and os.environ.get("GROWW_API_SECRET")))


def schedule_check(strategy, calendar: ExpiryCalendar | None, today: date | None = None) -> str | None:
    """Return why the strategy will not trade today, or None if it may trade (or we can't tell)."""
    if strategy.active_days == "any" or calendar is None or not strategy.underlyings:
        return None
    infos = {u: calendar.info(u, today) for u in strategy.underlyings}
    if any(i is None for i in infos.values()):
        return None                      # calendar unavailable -> don't block, Claude is told instead
    for info in infos.values():
        if info["is_expiry_day"] or (strategy.active_days == "expiry_and_day_before" and info["is_day_before_expiry"]):
            return None
    nxt = ", ".join(f"{calendar.settings.instrument(u)['label']} {i['next_expiry']} ({i['next_expiry_weekday']})"
                    for u, i in infos.items())
    return f"doesn't trade today — runs {ACTIVE_DAY_LABELS[strategy.active_days]}; next expiry: {nxt}"


def expiry_context(calendar: ExpiryCalendar | None, keys: list[str], today: date | None = None) -> dict:
    if calendar is None:
        return {}
    out = {}
    for k in keys:
        info = calendar.info(k, today)
        out[k] = info if info else "unavailable"
    return out

