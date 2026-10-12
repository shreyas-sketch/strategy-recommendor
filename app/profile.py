"""Participant trading profiles and strategy eligibility.

Eligibility is a hard, deterministic filter: a strategy that does not suit the participant's
risk tolerance, experience, capital or account type is never recommended to them, whatever
the market or the model says. Within the eligible set, Claude also weighs the profile.
"""

from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, Field

from .config import Strategy

RISK_LEVELS = ["low", "medium", "high"]
EXPERIENCE_LEVELS = ["beginner", "intermediate", "advanced"]
FNO_INSTRUMENTS = {"options", "futures"}
NO_TRADE = "NO_TRADE"


class TradingProfile(BaseModel):
    risk_tolerance: Literal["low", "medium", "high"]
    experience: Literal["beginner", "intermediate", "advanced"]
    capital_inr: int = Field(ge=0, le=10_000_000_000)
    fno_enabled: bool = False


def needs_fno(strategy: Strategy) -> bool:
    """True when the strategy can only be traded in the F&O segment."""
    return bool(strategy.instruments) and set(strategy.instruments) <= FNO_INSTRUMENTS


def inr(amount: int) -> str:
    if amount >= 10_000_000:
        return f"₹{amount / 10_000_000:g} crore"
    if amount >= 100_000:
        return f"₹{amount / 100_000:g} lakh"
    return f"₹{amount:,}"


def ineligibility(strategy: Strategy, profile: TradingProfile) -> list[str]:
    reasons = []
    if RISK_LEVELS.index(strategy.risk_level) > RISK_LEVELS.index(profile.risk_tolerance):
        reasons.append(f"{strategy.risk_level} risk — above your {profile.risk_tolerance} risk tolerance")
    if EXPERIENCE_LEVELS.index(strategy.min_experience) > EXPERIENCE_LEVELS.index(profile.experience):
        reasons.append(f"meant for {strategy.min_experience} traders — your profile says {profile.experience}")
    if strategy.min_capital_inr > profile.capital_inr:
        reasons.append(f"needs at least {inr(strategy.min_capital_inr)} capital — you have {inr(profile.capital_inr)}")
    if needs_fno(strategy) and not profile.fno_enabled:
        reasons.append("needs an F&O (futures & options) account")
    return reasons


def split(strategies: list[Strategy], profile: TradingProfile | None) -> tuple[list[Strategy], list[dict]]:
    """Return (eligible, excluded). With no profile, everything is eligible."""
    if profile is None:
        return list(strategies), []
    eligible, excluded = [], []
    for s in strategies:
        reasons = ineligibility(s, profile)
        if reasons:
            excluded.append({"strategy_id": s.id, "reasons": reasons, "kind": "profile"})
        else:
            eligible.append(s)
    return eligible, excluded


def capital_band(capital: int) -> str:
    """Coarse band sent to the model instead of the exact amount."""
    for limit, label in ((100_000, "under ₹1 lakh"), (500_000, "₹1–5 lakh"), (2_500_000, "₹5–25 lakh"),
                         (10_000_000, "₹25 lakh–1 crore")):
        if capital < limit:
            return label
    return "over ₹1 crore"


def profile_for_prompt(profile: TradingProfile | None) -> dict | None:
    if profile is None:
        return None
    return {"risk_tolerance": profile.risk_tolerance, "experience": profile.experience,
            "capital_band": capital_band(profile.capital_inr), "fno_account": profile.fno_enabled}


def apply_profile(rec: dict, eligible_ids: set[str], excluded: list[dict], names: dict[str, str]) -> dict:
    """Personalise a (possibly shared) recommendation: drop ineligible strategies, re-pick if needed."""
    rec = copy.deepcopy(rec)
    already = {e["strategy_id"] for e in rec.get("excluded", [])}
    rec["excluded"] = rec.get("excluded", []) + [e for e in excluded if e["strategy_id"] not in already]
    if not excluded:
        return rec
    market_top = rec.get("top_strategy_id")
    rec["rankings"] = [a for a in rec.get("rankings", []) if a["strategy_id"] in eligible_ids]
    for f in rec.get("deciding_factors", []):
        f["favors"] = [i for i in f.get("favors", []) if i in eligible_ids]
        f["hurts"] = [i for i in f.get("hurts", []) if i in eligible_ids]
    if market_top == NO_TRADE or market_top in eligible_ids:
        return rec

    reasons = next((e["reasons"] for e in excluded if e["strategy_id"] == market_top), [])
    candidates = [a for a in rec["rankings"] if a.get("verdict") != "avoid"]
    lead = (f"The best fit for this market is {names.get(market_top, market_top)}, but it does not suit your "
            f"profile ({'; '.join(reasons)}). ")
    if candidates:
        pick = candidates[0]
        rec["top_strategy_id"] = pick["strategy_id"]
        rec["why_top_over_others"] = (lead + f"{names.get(pick['strategy_id'], pick['strategy_id'])} is the best "
                                      f"of the strategies that suit you (fit {pick['fit_score']}). {pick['why']}")
    else:
        rec["top_strategy_id"] = NO_TRADE
        rec["why_top_over_others"] = lead + "None of the strategies that suit you fit today's market — sit out."
    rec["confidence"] = "low" if rec.get("confidence") == "low" else "medium"
    return rec
