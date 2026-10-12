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


class PortfolioRecommendation(BaseModel):
    portfolio_summary: str = Field(description="2-3 sentences on what this portfolio is exposed to right now")
    market_summary: str = Field(description="1-2 sentences on the benchmark index regime")
    confidence: Literal["low", "medium", "high"]
    top_strategy_id: str = Field(description=f"Strategy to run today, or {NO_TRADE}")
    run_on: str = Field(description="What to run it on: the benchmark index, or a holding's symbol from the list")
    why: str = Field(description="Why this strategy suits THIS portfolio today, citing holding and market numbers")
    why_top_over_others: str = Field(description=(
        "2-4 sentences: name the runner-up and the portfolio/market numbers that separate them. "
        f"If {NO_TRADE}, explain why even the best-scoring strategy is not good enough"))
    deciding_factors: list[DecidingFactor] = Field(description="3-6 portfolio or market facts that decided the ranking")
    rankings: list[StrategyAssessment] = Field(description="Every catalog strategy, best first")
    portfolio_risks: list[str] = Field(description="1-3 concrete risks in this portfolio today")
    whatsapp_line: str = Field(description="One plain-text sentence under 150 characters, no line breaks or emojis, "
                                           "summarising the reason for today's pick")


class RecommendationRefused(RuntimeError):
    pass


SYSTEM_TEMPLATE = """You are the strategy-selection analyst for an algorithmic trading platform in the Indian market (NSE/BSE).
Participants pick ONE of the platform's pre-built strategies to run. Wrong picks for the market regime cause losses, so your job is to match the current regime to the strategy whose rules fit it best.

How to decide:
- Use ONLY the numbers in the market snapshot. Do not invent news, events or prices.
- Compare the regime (trend, trend strength, volatility, squeeze, gaps, choppiness) against each strategy's logic, ideal conditions and avoid conditions.
- The rule_score is a simple pre-computed match; use it as a hint, and override it when the detailed numbers disagree (e.g. ADX and efficiency ratio conflict, intraday and daily trends disagree, volatility is rising fast).
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


PORTFOLIO_SYSTEM_TEMPLATE = """You are the strategy-selection analyst for an algorithmic trading platform in the Indian market (NSE/BSE).
Each morning a participant asks: "Given MY portfolio, which ONE of the platform's strategies should I run TODAY?"

How to decide:
- Use ONLY the numbers provided: the benchmark index snapshot, portfolio statistics and per-holding data. Do not invent news, events, sectors you are unsure of, or prices.
- Weigh both the market regime (benchmark) and the portfolio: concentration, how much of it is trending up/down or sideways, volatility of the largest holdings, unrealised profit or loss.
  Examples of portfolio-aware reasoning: a portfolio heavy in holdings that are falling may call for a strategy that profits from or hedges downside; large unrealised gains in volatile holdings argue for protection; a portfolio of strongly trending stocks suits trend-following on those stocks.
- run_on: say what to apply the strategy to — the benchmark index or one specific holding symbol from the list. Respect each strategy's instruments list.
- The rule_score is a market-only hint; override it when the portfolio changes the picture.
- If no strategy fits, set top_strategy_id to {no_trade} and explain — sitting out is valid.
- Rank every strategy in the catalog exactly once with its exact id; fit scores must match the order.
- Justify the pick against the alternatives: why_top_over_others names the runner-up and the separating numbers; vs_top on every strategy gives the concrete reason it ranks below the pick; deciding_factors lists 3-6 facts with the strategy ids each favors and hurts.
- Short, concrete, cite numbers (e.g. "38% of the portfolio is in downtrending stocks", "HDFCBANK RSI 74"). Decision support only — never promise profits.

The participant runs the strategy for the next trading DAY.

Strategy catalog:
{catalog}"""


def build_portfolio_system(strategies: list[Strategy]) -> str:
    catalog = json.dumps([s.model_dump(exclude={"horizons"}) for s in strategies], indent=1, ensure_ascii=False)
    return PORTFOLIO_SYSTEM_TEMPLATE.format(no_trade=NO_TRADE, catalog=catalog)


def build_portfolio_user(context: dict, rule_scores: list[dict]) -> str:
    # The context carries holdings and statistics only — never the participant's name or phone.
    return (
        f"Benchmark and portfolio data:\n{json.dumps(context, indent=1)}\n\n"
        f"Rule-engine market pre-scores (hint only):\n{json.dumps(rule_scores)}\n\n"
        "Which strategy should this participant run today?"
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

    def recommend_portfolio(self, *, model: ModelInfo, effort: str, strategies: list[Strategy],
                            context: dict, rule_scores: list[dict]):
        rec, usage = self._parse(model, effort, build_portfolio_system(strategies),
                                 build_portfolio_user(context, rule_scores), PortfolioRecommendation)
        return _sanitize(rec, strategies, rule_scores), usage

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
