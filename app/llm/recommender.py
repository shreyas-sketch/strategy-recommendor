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
    vs_top: str = Field(description=(
        "Comparison with the top pick. For the top pick: its edge over the runner-up. For every other "
        "strategy: the specific condition it fails (or fits worse) that the top pick meets, citing numbers"))


class DecidingFactor(BaseModel):
    factor: str = Field(description="A market fact with its number, e.g. 'ADX 31 — strong trend'")
    favors: list[str] = Field(description="Strategy ids this fact helps")
    hurts: list[str] = Field(description="Strategy ids this fact works against")


class Recommendation(BaseModel):
    market_summary: str = Field(description="2-3 plain-English sentences a participant understands")
    regime: str = Field(description="Short regime label, e.g. 'strong uptrend, normal volatility'")
    confidence: Literal["low", "medium", "high"]
    top_strategy_id: str = Field(description=f"Best strategy id, or {NO_TRADE} if nothing fits")
    why_top_over_others: str = Field(description=(
        "2-4 sentences validating the pick: name the runner-up and the numbers that separate them. "
        f"If {NO_TRADE}, explain why even the best-scoring strategy is not good enough"))
    deciding_factors: list[DecidingFactor] = Field(description="3-6 market facts that decided the ranking")
    rankings: list[StrategyAssessment] = Field(description="Every catalog strategy, best first")
    reassess_if: list[str] = Field(description="Concrete market changes that would flip the recommendation")


class DailyRecommendation(BaseModel):
    market_summary: str = Field(description="2-3 sentences on the regime of the indices the strategies trade")
    confidence: Literal["low", "medium", "high"]
    top_strategy_id: str = Field(description=f"Strategy to run in this session, or {NO_TRADE}")
    run_on: str = Field(description="The index the strategy trades (its underlying)")
    why: str = Field(description="Why this strategy fits this session, citing the market numbers of its index")
    why_top_over_others: str = Field(description=(
        "2-4 sentences: name the runner-up and the market numbers that separate them. "
        f"If {NO_TRADE}, explain why even the best-scoring strategy is not good enough"))
    deciding_factors: list[DecidingFactor] = Field(description="3-6 market facts that decided the ranking")
    rankings: list[StrategyAssessment] = Field(description="Every listed strategy, best first")
    whatsapp_line: str = Field(description="One plain-text sentence under 150 characters, no line breaks or emojis, "
                                           "summarising the reason for the pick")


class RecommendationRefused(RuntimeError):
    pass


SYSTEM_TEMPLATE = """You are the strategy-selection analyst for an algorithmic trading platform in the Indian market (NSE/BSE).
Participants pick ONE of the platform's pre-built strategies to run. Wrong picks for the market regime cause losses, so your job is to match the current regime to the strategy whose rules fit it best.

How to decide:
- Use ONLY the numbers in the market snapshot. Do not invent news, events or prices.
- Compare the regime (trend, trend strength, volatility, squeeze, gaps, choppiness) against each strategy's logic, ideal conditions and avoid conditions.
- The rule_score is a simple pre-computed match; use it as a hint, and override it when the detailed numbers disagree (e.g. ADX and efficiency ratio conflict, intraday and daily trends disagree, volatility is rising fast).
- If the snapshot has an "expiry" block (from the exchange calendar), weigh it for option strategies: expiry day brings fast premium decay and sharp late moves; few days to expiry means high gamma; a monthly expiry far away means slower decay.
- Mixed or conflicting signals should lower confidence. If no strategy is a reasonable fit, set top_strategy_id to {no_trade} and say why — sitting out is a valid recommendation.
- Rank every strategy in the catalog exactly once, using its exact id. Fit scores must be consistent with the order.
- Justify the pick against the alternatives, not just on its own merits. Participants must be able to verify why the top pick beats each other strategy:
  - why_top_over_others: name the runner-up and the specific numbers that separate them.
  - vs_top on every strategy: the concrete condition it fails (or fits worse) that the top pick meets.
  - deciding_factors: the 3-6 market facts that drove the ranking, each listing the strategy ids it favors and hurts.
- Write for traders, not quants: short, concrete, cite the key numbers (e.g. "ADX 31", "volatility in the 85th percentile").
- This is decision support, not financial advice. Never promise profits.

Analysis horizon: {horizon_text}

Strategy catalog:
{catalog}"""

HORIZON_TEXT = {
    "1d": "the participant will run the strategy for ONE trading session (intraday) — the date is in the snapshot's for_trading_day; the candles end at the latest available bar, which may be the previous session's close.",
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
        f"Market snapshot (computed from candles):\n{json.dumps(snapshot, indent=1, ensure_ascii=False)}\n\n"
        f"Rule-engine pre-scores (hint only):\n{json.dumps(rule_scores)}\n\n"
        "Recommend the best strategy for this horizon."
    )


DAILY_SYSTEM_TEMPLATE = """You are the strategy-selection analyst for an algorithmic trading platform in the Indian market (NSE/BSE).
Each day the platform tells its participants which ONE of its strategies best fits the next trading session. Strategies run automatically for that session. Decide from market data only.

How to decide:
- Use ONLY the numbers provided. Do not invent news, events, expiry dates you were not given, or prices.
- Each strategy trades the index listed in its "underlyings". Judge it on THAT index's regime in underlying_markets — never on a different index. A strategy with no underlyings is generic: judge it on the benchmark.
- Read each strategy's logic carefully: option buying needs movement and suffers from time decay in ranges; option selling and straddles profit from calm or decaying premium and suffer on trending or volatile days; in-the-money versus out-of-the-money strikes change the risk.
- trading_day is the session the pick is for; the candles end at the latest available bar, which may be the previous session's close. expiry_calendar gives each index's next expiry as seen from trading_day. Weigh expiry effects for option strategies: expiry day = fast premium decay and sharp late moves; near expiry = high gamma.
- Only the strategies in strategy_ids may be ranked; the ones in not_trading_this_session do not trade that day.
- The rule_score is a simple pre-computed match on each strategy's own index; use it as a hint and override it when the detailed numbers or the strategy logic disagree.
- If no strategy fits, set top_strategy_id to {no_trade} and explain — sitting out is valid.
- Rank every listed strategy exactly once with its exact id; fit scores must match the order.
- Justify the pick against the alternatives: why_top_over_others names the runner-up and the separating numbers; vs_top on every strategy gives the concrete reason it ranks below the pick; deciding_factors lists 3-6 facts with the strategy ids each favors and hurts.
- Short, concrete, cite numbers (e.g. "BANKNIFTY ADX 31", "NIFTY volatility in the 20th percentile"). Decision support only — never promise profits.

Strategy catalog:
{catalog}"""


def build_daily_system(strategies: list[Strategy]) -> str:
    catalog = json.dumps([s.model_dump(exclude={"horizons", "source"}) for s in strategies], indent=1, ensure_ascii=False)
    return DAILY_SYSTEM_TEMPLATE.format(no_trade=NO_TRADE, catalog=catalog)


def build_daily_user(context: dict, rule_scores: list[dict]) -> str:
    return (
        f"Market data for the session:\n{json.dumps(context, indent=1, ensure_ascii=False)}\n\n"
        f"Rule-engine pre-scores (hint only):\n{json.dumps(rule_scores, ensure_ascii=False)}\n\n"
        "Which strategy best fits this session?"
    )


class ClaudeRecommender:
    def __init__(self, client: anthropic.Anthropic | None = None):
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def recommend(self, *, model: ModelInfo, effort: str, horizon_key: str, instrument_label: str,
                  strategies: list[Strategy], snapshot: dict, rule_scores: list[dict]) -> tuple[Recommendation, dict]:
        rec, usage = self._parse(model, effort, build_system(horizon_key, strategies),
                                 build_user(instrument_label, snapshot, rule_scores), Recommendation)
        return _sanitize(rec, strategies, rule_scores), usage

    def recommend_daily(self, *, model: ModelInfo, effort: str, catalog: list[Strategy],
                        eligible: list[Strategy], context: dict, rule_scores: list[dict]):
        rec, usage = self._parse(model, effort, build_daily_system(catalog),
                                 build_daily_user(context, rule_scores), DailyRecommendation)
        return _sanitize(rec, eligible, rule_scores), usage

    def _parse(self, model: ModelInfo, effort: str, system: str, user: str, output_format):
        kwargs: dict = dict(
            model=model.id,
            max_tokens=16000,
            # The system prompt is identical across participants -> prompt-cached.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
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
        return response.parsed_output, usage


def _sanitize(rec, strategies: list[Strategy], rule_scores: list[dict]) -> Recommendation:
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
                why="Not assessed by the model; score from the rule engine.", watch_out="—",
                vs_top="Not compared by the model."))
    for f in rec.deciding_factors:
        f.favors = [i for i in f.favors if i in valid]
        f.hurts = [i for i in f.hurts if i in valid]
    ranked.sort(key=lambda a: -a.fit_score)
    rec.rankings = ranked
    if rec.top_strategy_id != NO_TRADE and rec.top_strategy_id not in valid:
        rec.top_strategy_id = ranked[0].strategy_id if ranked else NO_TRADE
    return rec
