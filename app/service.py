"""Orchestrates: budget check -> cache -> market data -> snapshot -> Claude (or rules) -> usage log."""

from __future__ import annotations

import logging
import time

import anthropic

from .analysis.regime import build_snapshot, fetch_specs, rule_scores
from .config import CHEAPEST_MODEL, Horizon, Settings
from .data.providers import DataRouter, now_ist
from .llm.recommender import NO_TRADE, ClaudeRecommender, RecommendationRefused
from .store import Store

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class RecommendationService:
    def __init__(self, settings: Settings, store: Store, router: DataRouter | None = None,
                 recommender: ClaudeRecommender | None = None, clock=time.time):
        self.settings = settings
        self.store = store
        self.router = router or DataRouter()
        self.recommender = recommender or ClaudeRecommender()
        self.clock = clock

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

    # ---- main entry point ------------------------------------------------
    def recommend(self, instrument_key: str, horizon: Horizon, force_refresh: bool = False) -> dict:
        instrument = self.settings.instrument(instrument_key)
        strategies = self.settings.strategies_for(horizon)
        if not strategies:
            raise ValueError(f"No strategies are configured for horizon {horizon}")
        engine = self.resolve_engine()
        admin = self.settings.admin

        cache_key = "|".join([instrument["key"], horizon, engine["engine"], engine["model"] or "-",
                              admin.effort, self.settings.catalog_hash()])
        max_age = 60 * admin.cache_minutes.get(horizon, 0)
        now = self.clock()
        if not force_refresh and max_age > 0:
            hit = self.store.cache_get(cache_key, max_age, now)
            if hit:
                hit["meta"]["cached"] = True
                hit["meta"]["cache_age_s"] = int(now - hit["meta"]["generated_at"])
                return hit

        primary_spec, daily_spec = fetch_specs(horizon, now_ist())
        primary_df, provider = self.router.fetch(instrument, primary_spec, admin.provider_order)
        daily_df = None
        if daily_spec is not None:
            try:
                daily_df, _ = self.router.fetch(instrument, daily_spec, admin.provider_order)
            except Exception as exc:  # daily context is a nice-to-have
                log.warning("daily context unavailable: %s", exc)
        snapshot = build_snapshot(primary_df, daily_df, horizon)
        scores = rule_scores(snapshot["regime"], strategies)

        meta = {"instrument": instrument["key"], "instrument_label": instrument["label"],
                "horizon": horizon, "data_provider": provider, "engine": engine["engine"],
                "model": engine["model"], "effort": admin.effort, "note": engine["note"],
                "cost_usd": 0.0, "cached": False, "generated_at": now}

        recommendation = None
        if engine["engine"] == "claude":
            model = self.settings.models[engine["model"]]
            try:
                rec, usage = self.recommender.recommend(
                    model=model, effort=admin.effort, horizon=horizon, instrument_label=instrument["label"],
                    strategies=strategies, snapshot=snapshot, rule_scores=scores)
                cost = model.cost_usd(usage["input_tokens"], usage["output_tokens"],
                                      usage["cache_write_tokens"], usage["cache_read_tokens"])
                self.store.record_usage(model=model.id, instrument=instrument["key"], horizon=horizon,
                                        cost_usd=cost, **usage)
                meta.update(cost_usd=round(cost, 5), served_model=usage["served_model"],
                            tokens={k: usage[k] for k in ("input_tokens", "output_tokens",
                                                          "cache_read_tokens", "cache_write_tokens")})
                recommendation = rec.model_dump()
            except (anthropic.APIError, RecommendationRefused, RuntimeError) as exc:
                log.error("Claude call failed, falling back to rules: %s", exc)
                meta.update(engine="rules", note=f"AI analysis unavailable ({type(exc).__name__}) — showing rule-engine result.")

        if recommendation is None:
            recommendation = rules_recommendation(snapshot, scores, strategies)

        payload = {"recommendation": recommendation, "snapshot": snapshot, "rule_scores": scores,
                   "strategies": {s.id: {"name": s.name, "category": s.category, "description": s.description}
                                  for s in strategies},
                   "meta": meta}
        if max_age > 0:
            self.store.cache_put(cache_key, payload, now)
        return payload


def rules_recommendation(snapshot: dict, scores: list[dict], strategies) -> dict:
    """Deterministic fallback when Claude is off or failing."""
    by_id = {s.id: s for s in strategies}
    regime = snapshot["regime"]
    rankings = []
    for sc in scores:
        s = by_id[sc["strategy_id"]]
        verdict = "recommended" if sc["rule_score"] >= 80 else "conditional" if sc["rule_score"] >= 50 else "avoid"
        rankings.append({"strategy_id": s.id, "fit_score": sc["rule_score"], "verdict": verdict,
                         "why": f"Rule match against ideal conditions for a {regime['label']} market.",
                         "watch_out": s.avoid_conditions[0] if s.avoid_conditions else s.risk_notes})
    top = rankings[0]["strategy_id"] if rankings and rankings[0]["fit_score"] >= 50 else NO_TRADE
    return {
        "market_summary": (f"Market looks {regime['label']} with {regime['volatility']} volatility"
                           f"{' and a Bollinger squeeze' if regime['squeeze'] else ''}."),
        "regime": f"{regime['label']}, {regime['volatility']} volatility",
        "confidence": "low",
        "top_strategy_id": top,
        "rankings": rankings,
        "reassess_if": ["Regime label changes on the next analysis"],
    }
