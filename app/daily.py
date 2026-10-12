"""The daily pick: which strategy to run in the next trading session, from market data only.

It is the same for every participant, so it is computed once per trading day and reused by the
web app and the 06:00 WhatsApp job. Each strategy is judged on the index it trades, and
strategies pinned to expiry days are left out on days they won't trade.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date

import anthropic

from .analysis.regime import rule_scores
from .data.base import ProviderError
from .expiries import expiry_context, schedule_check
from .llm.recommender import NO_TRADE, RecommendationRefused
from .service import RecommendationService, _RulesOnly, rules_recommendation, strategy_cards, trading_day

log = logging.getLogger(__name__)
DAILY_SCHEMA = "daily-v1"


def _clean_param(text: str, limit: int) -> str:
    """WhatsApp template params may not contain newlines/tabs or long runs of spaces."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


class DailyAdvisor:
    def __init__(self, service: RecommendationService):
        self.service = service
        self.store = service.store
        self.settings = service.settings
        self._lock = threading.Lock()

    def _key(self, day: str) -> str:
        engine = self.service.resolve_engine()
        return "|".join([DAILY_SCHEMA, day, engine["engine"], engine["model"] or "-", self.settings.admin.effort,
                         self.settings.catalog_hash()])

    def today(self, generate: bool = True) -> dict | None:
        """The pick for the next trading session (today before the 15:30 close, else the next session)."""
        day = trading_day(self.settings.admin.market_holidays).isoformat()
        key = self._key(day)
        hit = self.store.cache_get(key, 24 * 3600, time.time())
        if hit or not generate:
            return hit
        with self._lock:                 # one generation even if many participants open the page at once
            hit = self.store.cache_get(key, 24 * 3600, time.time())
            if hit:
                return hit
            payload = self._generate(day)
            self.store.cache_put(key, payload, time.time())
            return payload

    def _markets(self, keys: list[str]) -> tuple[dict, dict, str | None]:
        markets, views, provider = {}, {}, None
        for key in keys:
            inst = self.settings.instrument(key)
            try:
                snap, provider = self.service.market_snapshot(inst, "1d")
            except Exception as exc:
                log.warning("no market data for %s: %s", key, type(exc).__name__)
                continue
            markets[key] = {"label": inst["label"], "regime": snap["regime"]}
            views[key] = {"instrument": inst["label"], "regime": snap["regime"],
                          "primary": {k: snap["primary"].get(k) for k in (
                              "window_return_pct", "adx", "efficiency_ratio", "choppiness", "rsi14",
                              "realized_vol_annual_pct", "vol_percentile", "bb_width_percentile", "atr_pct")},
                          "daily_context": snap.get("daily_context")}
        return markets, views, provider

    def _generate(self, day: str) -> dict:
        target = date.fromisoformat(day)
        strategies = self.settings.strategies_for("1d")
        if not strategies:
            raise ValueError("No strategies are configured for the 1-day horizon")
        calendar = self.service.calendar
        tradeable, excluded = [], []
        for s in strategies:
            why = schedule_check(s, calendar, target)
            if why:
                excluded.append({"strategy_id": s.id, "reasons": [why], "kind": "schedule"})
            else:
                tradeable.append(s)
        cards = strategy_cards(strategies)
        meta = {"day": day, "engine": "claude", "benchmark": None, "data_provider": None, "note": None,
                "generated_at": time.time()}

        if not tradeable:
            rec = {"market_summary": "", "confidence": "high", "top_strategy_id": NO_TRADE, "run_on": "", "why": "",
                   "rankings": [], "deciding_factors": [], "excluded": excluded,
                   "why_top_over_others": "None of the strategies trade in this session.",
                   "whatsapp_line": "No strategy trades today."}
            meta.update(engine="rules")
            return {"recommendation": rec, "strategies": cards, "meta": meta}

        bench_key = self.settings.instrument(self.settings.admin.portfolio_benchmark)["key"]
        keys = list(dict.fromkeys(u for s in tradeable for u in s.underlyings))
        if any(not s.underlyings for s in tradeable) or not keys:
            keys = list(dict.fromkeys(keys + [bench_key]))      # generic strategies are judged on the benchmark
        markets, views, provider = self._markets(keys)
        if not markets:
            raise ProviderError("market data unavailable for " + ", ".join(keys))
        bench = markets.get(bench_key)
        meta.update(data_provider=provider, benchmark=self.settings.instrument(bench_key)["label"],
                    markets=[m["label"] for m in markets.values()])
        scores = rule_scores(bench["regime"] if bench else None, tradeable, markets)
        if not scores:
            raise ProviderError("market data unavailable for the indices these strategies trade")
        judged = [s for s in tradeable if s.id in {sc["strategy_id"] for sc in scores}]
        expiries = expiry_context(calendar, list(markets), target)
        context = {
            "trading_day": {"date": day, "weekday": target.strftime("%A")},
            "underlying_markets": views,
            "expiry_calendar": expiries,
            "strategy_ids": sorted(s.id for s in judged),
            "not_trading_this_session": excluded,
        }

        rec = None
        try:
            parsed, info = self.service.call_claude(
                self.service.recommender.recommend_daily, label="DAILY", horizon="1d",
                catalog=strategies, eligible=judged, context=context, rule_scores=scores)
            rec = parsed.model_dump()
            meta.update(model=info["model"], cost_usd=info["cost_usd"], note=info["note"])
        except _RulesOnly as exc:
            meta.update(engine="rules", note=str(exc) or None)
        except (anthropic.APIError, RecommendationRefused, RuntimeError) as exc:
            log.error("daily Claude call failed: %s", type(exc).__name__)
            meta.update(engine="rules", note="AI analysis unavailable — showing rule-engine result.")

        if rec is None:
            rec = rules_recommendation(None, scores, judged)
            top_sc = next((sc for sc in scores if sc["strategy_id"] == rec["top_strategy_id"]), None)
            rec.update(run_on=(top_sc or {}).get("on") or meta["benchmark"], why="",
                       whatsapp_line=_clean_param(rec["why_top_over_others"], 150))
        rec["excluded"] = excluded
        rec["whatsapp_line"] = _clean_param(rec.get("whatsapp_line", ""), 150)
        if rec["top_strategy_id"] != NO_TRADE:
            top = next((s for s in strategies if s.id == rec["top_strategy_id"]), None)
            run_on = rec.get("run_on") or ""
            if top and top.underlyings:
                # A strategy that trades a specific index always runs on that index, whatever the model wrote.
                labels = [self.settings.instrument(u)["label"] for u in top.underlyings]
                if not any(x.upper() in run_on.upper() for x in labels + list(top.underlyings)):
                    run_on = ", ".join(labels)
            rec["run_on"] = _clean_param(run_on or meta["benchmark"], 40)
        return {"recommendation": rec, "markets": views, "expiries": expiries, "strategies": cards, "meta": meta}
