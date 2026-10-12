"""Claude-powered strategy ranking.

The deterministic snapshot + rule scores go in; a validated, structured
recommendation comes out. The model is whatever the admin selected.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import ModelInfo, Strategy

log = logging.getLogger(__name__)

NO_TRADE = "NO_TRADE"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class StrategyAssessment(BaseModel):
    strategy_id: str = Field(description="Exact id from the catalog")
    fit_score: int = Field(description="0-100: how well current conditions suit this strategy")
    verdict: Literal["recommended", "conditional", "avoid"]
    why: str = Field(description="1-2 sentences tying specific snapshot numbers to the strategy's rules")
    watch_out: str = Field(description="The main way this strategy could fail in this market")


class Recommendation(BaseModel):
    market_summary: str = Field(description="2-3 plain-English sentences a participant understands")
    regime: str = Field(description="Short regime label, e.g. 'strong uptrend, normal volatility'")
    confidence: Literal["low", "medium", "high"]
    top_strategy_id: str = Field(description=f"Best strategy id, or {NO_TRADE} if nothing fits")
    rankings: list[StrategyAssessment] = Field(description="Every catalog strategy, best first")
    reassess_if: list[str] = Field(description="Concrete market changes that would flip the recommendation")


class RecommendationRefused(RuntimeError):
    pass


SYSTEM_TEMPLATE = """You are the strategy-selection analyst for an algorithmic trading platform in the Indian market (NSE/BSE).
Participants pick ONE of the platform's pre-built strategies to run. Wrong picks for the market regime cause losses, so your job is to match the current regime to the strategy whose rules fit it best.

How to decide:
- Use ONLY the numbers in the market snapshot. Do not invent news, events or prices.
- Compare the regime (trend, trend strength, volatility, squeeze, gaps, choppiness) against each strategy's logic, ideal conditions and avoid conditions.
- The rule_score is a simple pre-computed match; use it as a hint, and override it when the detailed numbers disagree (e.g. ADX and efficiency ratio conflict, intraday and daily trends disagree, volatility is rising fast).
- Mixed or conflicting signals should lower confidence. If no strategy is a reasonable fit, set top_strategy_id to {no_trade} and say why — sitting out is a valid recommendation.
- Rank every strategy in the catalog exactly once, using its exact id.
- Write for traders, not quants: short, concrete, cite the key numbers (e.g. "ADX 31", "volatility in the 85th percentile").
- This is decision support, not financial advice. Never promise profits.

Analysis horizon: {horizon_text}

Strategy catalog:
{catalog}"""

HORIZON_TEXT = {
    "1d": "the participant will run the strategy for the next trading DAY (intraday).",
    "1w": "the participant will run the strategy for the next trading WEEK.",
    "1m": "the participant will run the strategy for the next trading MONTH.",
}


def build_system(horizon: str, strategies: list[Strategy]) -> str:
    catalog = json.dumps(
        [s.model_dump(exclude={"horizons"}) for s in strategies], indent=1, ensure_ascii=False
    )
    return SYSTEM_TEMPLATE.format(no_trade=NO_TRADE, horizon_text=HORIZON_TEXT[horizon], catalog=catalog)


def build_user(instrument_label: str, snapshot: dict, rule_scores: list[dict]) -> str:
    return (
        f"Instrument: {instrument_label}\n\n"
        f"Market snapshot (computed from candles):\n{json.dumps(snapshot, indent=1)}\n\n"
        f"Rule-engine pre-scores (hint only):\n{json.dumps(rule_scores)}\n\n"
        "Recommend the best strategy for this horizon."
    )


class ClaudeRecommender:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def recommend(self, *, model: ModelInfo, effort: str, horizon: str, instrument_label: str,
                  strategies: list[Strategy], snapshot: dict, rule_scores: list[dict]) -> tuple[Recommendation, dict]:
        kwargs: dict = dict(
            model=model.id,
            max_tokens=16000,
            # Stable per horizon -> prompt-cached across participants.
            system=[{"type": "text", "text": build_system(horizon, strategies),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": build_user(instrument_label, snapshot, rule_scores)}],
            output_format=Recommendation,
            output_config={"effort": effort},
        )
        if model.server_fallback:
            # If a safety classifier declines, the API re-runs on a recommended fallback model.
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")

        response = self.client.beta.messages.parse(**kwargs)

        if response.stop_reason == "refusal":
            raise RecommendationRefused(getattr(response.stop_details, "explanation", None) or "model declined")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError(f"incomplete model output (stop_reason={response.stop_reason})")

        u = response.usage
        usage = {
            "input_tokens": u.input_tokens or 0,
            "output_tokens": u.output_tokens or 0,
            "cache_write_tokens": u.cache_creation_input_tokens or 0,
            "cache_read_tokens": u.cache_read_input_tokens or 0,
            "served_model": response.model,
            "request_id": getattr(response, "_request_id", None),
        }
        return _sanitize(response.parsed_output, strategies, rule_scores), usage


def _sanitize(rec: Recommendation, strategies: list[Strategy], rule_scores: list[dict]) -> Recommendation:
    """Drop unknown/duplicate ids, append any strategy the model skipped, clamp scores."""
    valid = {s.id for s in strategies}
    seen, ranked = set(), []
    for a in rec.rankings:
        if a.strategy_id in valid and a.strategy_id not in seen:
            a.fit_score = max(0, min(100, a.fit_score))
            ranked.append(a)
            seen.add(a.strategy_id)
    for rs in rule_scores:
        if rs["strategy_id"] not in seen:
            ranked.append(StrategyAssessment(
                strategy_id=rs["strategy_id"], fit_score=rs["rule_score"], verdict="conditional",
                why="Not assessed by the model; score from the rule engine.", watch_out="—"))
    ranked.sort(key=lambda a: -a.fit_score)
    rec.rankings = ranked
    if rec.top_strategy_id != NO_TRADE and rec.top_strategy_id not in valid:
        rec.top_strategy_id = ranked[0].strategy_id if ranked else NO_TRADE
    return rec
