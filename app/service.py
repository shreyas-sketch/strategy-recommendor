"""Orchestrates: budget check -> cache -> market data -> snapshot -> Claude (or rules) -> usage log."""

from __future__ import annotations

import logging
import threading
import time

import anthropic

from .analysis.regime import build_snapshot, fetch_specs, rule_scores
from .config import CHEAPEST_MODEL, Horizon, Settings
from .data.providers import DataRouter, now_ist
from .expiries import schedule_check
from .llm.recommender import NO_TRADE, ClaudeRecommender, RecommendationRefused
from .store import Store

log = logging.getLogger(__name__)


# Bump when the recommendation payload shape changes so stale cached results are not served.
RESULT_SCHEMA = "v2"


class BudgetExceeded(RuntimeError):
    pass


class QuotaExceeded(RuntimeError):
    pass


def today_ist() -> str:
    return now_ist().strftime("%Y-%m-%d")


MARKET_CLOSE = (15, 30)


def trading_day(holidays=(), now=None):
    """The session a 1-day pick is for: today until the 15:30 IST close on a trading day, otherwise
    the next weekday that isn't a listed holiday (so an evening run is for tomorrow's session)."""
    from datetime import timedelta
    now = now or now_ist()
    day = now.date()
    holidays = set(holidays)

    def open_on(d):
        return d.weekday() < 5 and d.isoformat() not in holidays

    if open_on(day) and (now.hour, now.minute) < MARKET_CLOSE:
        return day
    day += timedelta(days=1)
    while not open_on(day):
        day += timedelta(days=1)
    return day


class TTLCache:
    """Small in-process cache for market snapshots shared by all participants (no personal data)."""

    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key, max_age: float, now: float):
        with self._lock:
            hit = self._data.get(key)
            return hit[1] if hit and now - hit[0] <= max_age else None

    def put(self, key, value, now: float):
        with self._lock:
            if len(self._data) > 2000:
                self._data.clear()
            self._data[key] = (now, value)


class RecommendationService:
    def __init__(self, settings: Settings, store: Store, router: DataRouter | None = None,
                 recommender: ClaudeRecommender | None = None, clock=time.time, calendar=None):
        self.settings = settings
        self.store = store
        self.router = router or DataRouter()
        self.recommender = recommender or ClaudeRecommender()
        self.clock = clock
        self.calendar = calendar          # ExpiryCalendar (Groww); None disables expiry awareness
        self._snapshots = TTLCache()

    # ---- which engine/model runs this request ----------------------------
    def resolve_engine(self) -> dict:
        admin = self.settings.admin
        spend = self.store.month_spend()
        over = admin.monthly_budget_usd > 0 and spend >= admin.monthly_budget_usd
        state = {"engine": "claude", "model": admin.active_model, "budget_exceeded": over,
                 "month_spend_usd": round(spend, 4), "note": None}
        if over:
            if admin.on_budget_exceeded == "block":
                raise BudgetExceeded("Monthly AI budget reached. Ask the admin to raise it.")
            if admin.on_budget_exceeded == "rules_only":
                state.update(engine="rules", model=None, note="Monthly AI budget reached — using the rule engine.")
            elif admin.active_model != CHEAPEST_MODEL:
                state.update(model=CHEAPEST_MODEL, note="Monthly AI budget reached — downgraded to Haiku 5.5.")
        return state

    # ---- shared market data (cached, not personal) --------------------------
    def market_snapshot(self, instrument: dict, horizon: Horizon) -> tuple[dict, str]:
        """Candles -> snapshot for one instrument/horizon, cached in memory for 10 minutes."""
        key = ("snap", instrument["key"], horizon)
        now = self.clock()
        hit = self._snapshots.get(key, 600, now)
        if hit:
            return hit
        order = self.settings.admin.provider_order
        primary_spec, daily_spec = fetch_specs(horizon, now_ist())
        primary_df, provider = self.router.fetch(instrument, primary_spec, order)
        daily_df = None
        if daily_spec is not None:
            try:
                daily_df, _ = self.router.fetch(instrument, daily_spec, order)
            except Exception as exc:  # daily context is a nice-to-have
                log.warning("daily context unavailable: %s", exc)
        result = (build_snapshot(primary_df, daily_df, horizon), provider)
        self._snapshots.put(key, result, now)
        return result

    def call_claude(self, fn, *, label: str, horizon: str, **kwargs) -> tuple[object, dict]:
        """Run a recommender call on the currently resolved model and record its cost."""
        engine = self.resolve_engine()
        if engine["engine"] != "claude":
            raise _RulesOnly(engine["note"])
        model = self.settings.models[engine["model"]]
        rec, usage = fn(model=model, effort=self.settings.admin.effort, **kwargs)
        cost = model.cost_usd(usage["input_tokens"], usage["output_tokens"],
                              usage["cache_write_tokens"], usage["cache_read_tokens"])
        self.store.record_usage(model=model.id, instrument=label, horizon=horizon, cost_usd=cost, **usage)
        return rec, {"model": model.id, "served_model": usage["served_model"], "cost_usd": round(cost, 5),
                     "note": engine["note"]}

    # ---- market recommendation --------------------------------------------
    def recommend(self, instrument_key: str, horizon: Horizon, force_refresh: bool = False,
                  quota_user: int | None = None) -> dict:
        instrument = self.settings.instrument(instrument_key)
        all_for_horizon = self.settings.strategies_for(horizon)
        # A strategy that trades a specific index is only judged on that index's market.
        strategies = [s for s in all_for_horizon if not s.underlyings or instrument["key"] in s.underlyings]
        if not strategies:
            traded = sorted({u for s in all_for_horizon for u in s.underlyings})
            raise ValueError(f"No {horizon} strategy trades {instrument['key']}. "
                             f"Analyse one of: {', '.join(traded) or 'none configured'}")
        # Intraday horizon: strategies pinned to expiry days only count on the days they actually trade.
        schedule_excluded = []
        target = trading_day(self.settings.admin.market_holidays)
        if horizon == "1d":
            keep = []
            for s in strategies:
                why = schedule_check(s, self.calendar, target)
                if why:
                    schedule_excluded.append({"strategy_id": s.id, "reasons": [why], "kind": "schedule"})
                else:
                    keep.append(s)
            strategies = keep
        engine = self.resolve_engine()
        admin = self.settings.admin

        cache_key = "|".join([RESULT_SCHEMA, instrument["key"], horizon, engine["engine"], engine["model"] or "-",
                              admin.effort, self.settings.catalog_hash(), target.isoformat(),
                              ",".join(sorted(e["strategy_id"] for e in schedule_excluded))])
        max_age = 60 * admin.cache_minutes.get(horizon, 0)
        now = self.clock()
        if not force_refresh and max_age > 0:
            hit = self.store.cache_get(cache_key, max_age, now)
            if hit:
                hit["meta"]["cached"] = True
                hit["meta"]["cache_age_s"] = int(now - hit["meta"]["generated_at"])
                return hit

        # A cache miss on the AI engine costs money: count it against the participant's daily cap.
        if (quota_user is not None and engine["engine"] == "claude"
                and not self.store.take_quota(quota_user, today_ist(), "analysis", admin.per_user_daily_analyses)):
            raise QuotaExceeded(f"Daily limit of {admin.per_user_daily_analyses} fresh analyses reached. "
                                "Try an instrument others have checked recently, or come back tomorrow.")

        snapshot, provider = self.market_snapshot(instrument, horizon)
        if self.calendar is not None and instrument.get("fno") and horizon in ("1d", "1w"):
            expiry = self.calendar.info(instrument["key"], target)
            snapshot = {**snapshot, "expiry": expiry or "unavailable"}
        if horizon == "1d":
            snapshot = {**snapshot, "for_trading_day": {"date": target.isoformat(), "weekday": target.strftime("%A")}}
        cards = strategy_cards(all_for_horizon)
        if not strategies:
            rec = {"market_summary": "", "regime": snapshot["regime"]["label"], "confidence": "high",
                   "top_strategy_id": NO_TRADE, "rankings": [], "deciding_factors": [], "reassess_if": [],
                   "why_top_over_others": "None of the strategies for this instrument trade today.",
                   "excluded": schedule_excluded}
            return {"recommendation": rec, "snapshot": snapshot, "rule_scores": [], "strategies": cards,
                    "meta": {"instrument": instrument["key"], "instrument_label": instrument["label"],
                             "trading_day": target.isoformat(), "horizon": horizon, "data_provider": provider, "engine": "rules", "model": None,
                             "effort": admin.effort, "note": None, "cost_usd": 0.0, "cached": False,
                             "generated_at": now}}
        scores = rule_scores(snapshot["regime"], strategies)

        meta = {"instrument": instrument["key"], "instrument_label": instrument["label"],
                "trading_day": target.isoformat() if horizon == "1d" else None,
                "horizon": horizon, "data_provider": provider, "engine": engine["engine"],
                "model": engine["model"], "effort": admin.effort, "note": engine["note"],
                "cost_usd": 0.0, "cached": False, "generated_at": now}

        recommendation = None
        if engine["engine"] == "claude":
            try:
                rec, info = self.call_claude(
                    self.recommender.recommend, label=instrument["key"], horizon=horizon,
                    horizon_key=horizon, instrument_label=instrument["label"],
                    strategies=strategies, snapshot=snapshot, rule_scores=scores)
                meta.update(cost_usd=info["cost_usd"], served_model=info["served_model"])
                recommendation = rec.model_dump()
            except _RulesOnly:
                pass
            except (anthropic.APIError, RecommendationRefused, RuntimeError) as exc:
                log.error("Claude call failed, falling back to rules: %s", type(exc).__name__)
                meta.update(engine="rules", note=f"AI analysis unavailable ({type(exc).__name__}) — showing rule-engine result.")

        if recommendation is None:
            recommendation = rules_recommendation(snapshot, scores, strategies)
        recommendation["excluded"] = schedule_excluded

        payload = {"recommendation": recommendation, "snapshot": snapshot, "rule_scores": scores,
                   "strategies": cards, "meta": meta}
        if max_age > 0:
            self.store.cache_put(cache_key, payload, now)
        return payload


class _RulesOnly(Exception):
    pass


def strategy_cards(strategies) -> dict:
    return {s.id: {"name": s.name, "category": s.category, "description": s.description} for s in strategies}


DIM_LABELS = {"trend": "trend", "trend_strength": "trend strength", "volatility": "volatility", "squeeze": "squeeze"}


def _wanted(strategy, dim: str) -> str:
    c = strategy.ideal_conditions
    if dim == "squeeze":
        return "a squeeze" if c.squeeze is True else "no squeeze"
    return "/".join(getattr(c, dim)) or "any"


def _regime_value(regime: dict, dim: str) -> str:
    if dim == "squeeze":
        return "a squeeze" if regime["squeeze"] else "no squeeze"
    return regime[dim]


def rules_recommendation(snapshot: dict | None, scores: list[dict], strategies) -> dict:
    """Deterministic fallback when Claude is off or failing, including the comparison with the top pick.

    Each score carries the regime it was judged on (its own underlying), so mixed-index catalogs work.
    """
    by_id = {s.id: s for s in strategies}
    default_regime = snapshot["regime"] if snapshot else scores[0]["regime"]
    top_sc = scores[0]
    top = by_id[top_sc["strategy_id"]]

    def reg(sc: dict) -> dict:
        return sc.get("regime") or default_regime

    def where(sc: dict) -> str:
        return f" on {sc['on']}" if sc.get("on") else ""

    def shortfalls(sc: dict) -> list[str]:
        s = by_id[sc["strategy_id"]]
        return [f"wants {'' if d == 'squeeze' else DIM_LABELS[d] + ' '}{_wanted(s, d)} "
                f"but {sc.get('on') or 'the market'} shows {_regime_value(reg(sc), d)}"
                for d, v in sc["breakdown"].items() if v < top_sc["breakdown"].get(d, 1.0) or v < 0.5]

    rankings = []
    for i, sc in enumerate(scores):
        s = by_id[sc["strategy_id"]]
        verdict = "recommended" if sc["rule_score"] >= 80 else "conditional" if sc["rule_score"] >= 50 else "avoid"
        if i == 0:
            runner = scores[1] if len(scores) > 1 else None
            vs_top = (f"Top rule score ({sc['rule_score']}); next best is {by_id[runner['strategy_id']].name} "
                      f"({runner['rule_score']})." if runner else "Only strategy for this horizon.")
        elif sc["rule_score"] == top_sc["rule_score"]:
            vs_top = (f"Ties with {top.name} at {sc['rule_score']} — the rule engine cannot separate them; "
                      "either fits the current conditions.")
        else:
            gaps = shortfalls(sc)
            vs_top = (f"Scores {sc['rule_score']} vs {top_sc['rule_score']} for {top.name}: "
                      + ("; ".join(gaps) if gaps else "matches its ideal conditions less closely") + ".")
        rankings.append({"strategy_id": s.id, "fit_score": sc["rule_score"], "verdict": verdict,
                         "why": f"Rule match against ideal conditions for a {reg(sc)['label']} market{where(sc)}.",
                         "watch_out": s.avoid_conditions[0] if s.avoid_conditions else s.risk_notes,
                         "vs_top": vs_top})

    factors = []
    groups: dict = {}
    for sc in scores:
        groups.setdefault(sc.get("on"), []).append(sc)
    for on, group in groups.items():
        r = reg(group[0])
        for dim in ("trend", "trend_strength", "volatility", "squeeze"):
            favors = [sc["strategy_id"] for sc in group if sc["breakdown"].get(dim) == 1.0
                      and (dim != "squeeze" or by_id[sc["strategy_id"]].ideal_conditions.squeeze != "any")]
            hurts = [sc["strategy_id"] for sc in group if sc["breakdown"].get(dim, 1.0) <= 0.3]
            if favors or hurts:
                factors.append({"factor": f"{on + ' ' if on else ''}{DIM_LABELS[dim]}: {_regime_value(r, dim)}",
                                "favors": favors, "hurts": hurts})

    pick = top.id if top_sc["rule_score"] >= 50 else NO_TRADE
    if pick == NO_TRADE:
        why = f"Even the best match, {top.name}, scores only {top_sc['rule_score']}/100 — no strategy fits well."
    elif len(scores) > 1 and scores[1]["rule_score"] == top_sc["rule_score"]:
        runner = by_id[scores[1]["strategy_id"]]
        why = (f"{top.name} and {runner.name} tie at {top_sc['rule_score']}/100 on the rule engine — "
               "both suit this market. Switch the AI analysis on to separate them.")
    elif len(scores) > 1:
        runner = by_id[scores[1]["strategy_id"]]
        gaps = shortfalls(scores[1])
        why = (f"{top.name} matches the most ideal conditions ({top_sc['rule_score']}/100{where(top_sc)}). "
               f"Runner-up {runner.name} ({scores[1]['rule_score']}/100) "
               + ("; ".join(gaps) if gaps else "ties on conditions and ranks second on order") + ".")
    else:
        why = f"{top.name} is the only strategy configured for this horizon."

    summaries = []
    for on, group in groups.items():
        r = reg(group[0])
        summaries.append(f"{on or 'Market'} looks {r['label']} with {r['volatility']} volatility"
                         f"{' and a Bollinger squeeze' if r['squeeze'] else ''}")
    return {
        "market_summary": "; ".join(summaries) + ".",
        "regime": "; ".join(f"{on or 'market'}: {reg(g[0])['label']}" for on, g in groups.items()),
        "confidence": "low",
        "top_strategy_id": pick,
        "why_top_over_others": why,
        "deciding_factors": factors,
        "rankings": rankings,
        "reassess_if": ["Regime label changes on the next analysis"],
    }
