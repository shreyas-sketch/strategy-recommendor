"""Turn candles into a compact market snapshot + rule-based regime and strategy scores.

All number crunching happens here, deterministically. Claude only receives the
summary (a few hundred tokens), which keeps cost low and the math reliable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from ..config import Horizon, Strategy
from ..data.base import FetchSpec
from . import indicators as ind


@dataclass(frozen=True)
class HorizonSpec:
    interval: str          # primary candle size
    fetch_days: int        # calendar days of primary candles (incl. indicator warm-up)
    window_bars: int       # bars summarised as "the recent period"
    bars_per_year: int     # for annualising volatility
    window_label: str


# NSE session 09:15–15:30 = 25 x 15m bars, ~7 x 1h bars.
HORIZON_SPECS: dict[str, HorizonSpec] = {
    "1d": HorizonSpec("15m", 12, 50, 25 * 252, "last 2 sessions (15-min candles)"),
    "1w": HorizonSpec("1h", 50, 35, 7 * 252, "last 5 sessions (1-hour candles)"),
    "1m": HorizonSpec("1d", 420, 22, 252, "last 22 sessions (daily candles)"),
}
DAILY_CONTEXT_DAYS = 420

VOL_LEVELS = ["low", "normal", "high", "extreme"]
STRENGTH_LEVELS = ["weak", "moderate", "strong"]


def fetch_specs(horizon: Horizon, end) -> tuple[FetchSpec, FetchSpec | None]:
    hs = HORIZON_SPECS[horizon]
    primary = FetchSpec(hs.interval, end - timedelta(days=hs.fetch_days), end)
    daily = None if hs.interval == "1d" else FetchSpec("1d", end - timedelta(days=DAILY_CONTEXT_DAYS), end)
    return primary, daily


def _r(x, nd=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def _frame_features(df: pd.DataFrame, window: int, bars_per_year: int) -> dict:
    close = df["close"]
    window = max(5, min(window, len(df) - 1))
    w = df.iloc[-window:]
    logret = np.log(close).diff()
    atr14 = ind.atr(df)
    atr_pct = atr14 / close * 100
    adx_df = ind.adx(df)
    bbw = ind.bollinger_bandwidth(close)
    e20, e50 = ind.ema(close, 20), ind.ema(close, 50)
    roll_vol = logret.rolling(20).std() * math.sqrt(bars_per_year) * 100
    rng_hi, rng_lo = w["high"].max(), w["low"].min()
    last = float(close.iloc[-1])
    vol_avg = df["volume"].iloc[-window * 4:-window].mean() if len(df) > window * 2 else np.nan

    return {
        "last_close": _r(last),
        "window_return_pct": _r((last / float(w["close"].iloc[0]) - 1) * 100),
        "window_high": _r(rng_hi),
        "window_low": _r(rng_lo),
        "window_range_pct": _r((rng_hi - rng_lo) / last * 100),
        "close_position_in_range": _r((last - rng_lo) / (rng_hi - rng_lo), 2) if rng_hi > rng_lo else None,
        "max_drawdown_pct": _r(ind.max_drawdown(w["close"]) * 100),
        "realized_vol_annual_pct": _r(logret.iloc[-window:].std() * math.sqrt(bars_per_year) * 100),
        "vol_percentile": _r(ind.percentile_rank(roll_vol, roll_vol.iloc[-1]), 0),
        "atr_pct": _r(atr_pct.iloc[-1]),
        "atr_percentile": _r(ind.percentile_rank(atr_pct, atr_pct.iloc[-1]), 0),
        "adx": _r(adx_df["adx"].iloc[-1], 1),
        "plus_di": _r(adx_df["plus_di"].iloc[-1], 1),
        "minus_di": _r(adx_df["minus_di"].iloc[-1], 1),
        "efficiency_ratio": _r(ind.efficiency_ratio(close, window)),
        "choppiness": _r(ind.choppiness(df).iloc[-1], 1),
        "rsi14": _r(ind.rsi(close).iloc[-1], 1),
        "ema20_vs_ema50_pct": _r((e20.iloc[-1] / e50.iloc[-1] - 1) * 100) if len(df) >= 50 else None,
        "price_vs_ema20_pct": _r((last / e20.iloc[-1] - 1) * 100) if len(df) >= 20 else None,
        "bb_width_pct": _r(bbw.iloc[-1] * 100),
        "bb_width_percentile": _r(ind.percentile_rank(bbw.iloc[-120:], bbw.iloc[-1]), 0),
        "return_autocorr_lag1": _r(logret.iloc[-window:].autocorr(1)),
        "volume_vs_prior": _r(w["volume"].mean() / vol_avg) if vol_avg and vol_avg > 0 else None,
        "bars": int(len(df)),
    }


def _daily_context(daily: pd.DataFrame) -> dict:
    close = daily["close"]
    gaps = (daily["open"] / close.shift() - 1).abs() * 100
    e50, e200 = ind.ema(close, 50), ind.ema(close, 200)
    logret = np.log(close).diff()
    roll_vol = logret.rolling(20).std() * math.sqrt(252) * 100
    return {
        "return_20d_pct": _r((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) > 21 else None,
        "return_60d_pct": _r((close.iloc[-1] / close.iloc[-61] - 1) * 100) if len(close) > 61 else None,
        "price_vs_ema50_pct": _r((close.iloc[-1] / e50.iloc[-1] - 1) * 100) if len(close) >= 50 else None,
        "price_vs_ema200_pct": _r((close.iloc[-1] / e200.iloc[-1] - 1) * 100) if len(close) >= 200 else None,
        "realized_vol_20d_pct": _r(roll_vol.iloc[-1]),
        "vol_20d_percentile_1y": _r(ind.percentile_rank(roll_vol.iloc[-252:], roll_vol.iloc[-1]), 0),
        "avg_abs_gap_pct_20d": _r(gaps.iloc[-20:].mean()),
        "max_abs_gap_pct_20d": _r(gaps.iloc[-20:].max()),
        "adx_daily": _r(ind.adx(daily)["adx"].iloc[-1], 1),
    }


def classify(primary: dict, daily: dict | None) -> dict:
    adx = primary.get("adx") or 0
    er = primary.get("efficiency_ratio") or 0
    chop = primary.get("choppiness") or 50
    if adx >= 25 and er >= 0.3:
        strength = "strong"
    elif adx >= 20 or er >= 0.2:
        strength = "moderate"
    else:
        strength = "weak"
    if chop > 61.8 and strength == "strong":
        strength = "moderate"

    ret = primary.get("window_return_pct") or 0
    ema_gap = primary.get("ema20_vs_ema50_pct")
    if strength == "weak":
        trend = "sideways"
    elif ret > 0 and (ema_gap is None or ema_gap > 0):
        trend = "up"
    elif ret < 0 and (ema_gap is None or ema_gap < 0):
        trend = "down"
    else:
        trend = "sideways"
        strength = "weak"

    pcts = [p for p in (primary.get("vol_percentile"), primary.get("atr_percentile"),
                        (daily or {}).get("vol_20d_percentile_1y")) if p is not None]
    vol_pct = float(np.mean(pcts)) if pcts else 50.0
    volatility = "low" if vol_pct < 25 else "normal" if vol_pct < 75 else "high" if vol_pct < 93 else "extreme"

    bbp = primary.get("bb_width_percentile")
    squeeze = bbp is not None and bbp <= 20

    return {
        "trend": trend,
        "trend_strength": strength,
        "volatility": volatility,
        "volatility_score": round(vol_pct),
        "squeeze": squeeze,
        "label": f"{strength} {trend}" if trend != "sideways" else f"sideways / range-bound",
    }


def build_snapshot(primary_df: pd.DataFrame, daily_df: pd.DataFrame | None, horizon: Horizon) -> dict:
    hs = HORIZON_SPECS[horizon]
    if len(primary_df) < 30:
        raise ValueError(f"only {len(primary_df)} candles available — need at least 30")
    primary = _frame_features(primary_df, hs.window_bars, hs.bars_per_year)
    daily_src = daily_df if daily_df is not None else (primary_df if hs.interval == "1d" else None)
    daily = _daily_context(daily_src) if daily_src is not None and len(daily_src) > 25 else None
    return {
        "horizon": horizon,
        "window": hs.window_label,
        "as_of": primary_df.index[-1].isoformat(),
        "primary": primary,
        "daily_context": daily,
        "regime": classify(primary, daily),
    }


# ---------------------------------------------------------------------------
# Rule engine: used as a hint for Claude and as the fallback when Claude is
# switched off (budget) or unavailable.
# ---------------------------------------------------------------------------
def _level_match(actual: str, wanted: list[str], scale: list[str]) -> float:
    if not wanted:
        return 1.0
    if actual in wanted:
        return 1.0
    if actual in scale and any(w in scale and abs(scale.index(w) - scale.index(actual)) == 1 for w in wanted):
        return 0.5
    return 0.0


def rule_scores(regime: dict, strategies: list[Strategy]) -> list[dict]:
    out = []
    for s in strategies:
        c = s.ideal_conditions
        parts = [
            1.0 if not c.trend or regime["trend"] in c.trend else 0.0,
            _level_match(regime["trend_strength"], c.trend_strength, STRENGTH_LEVELS),
            _level_match(regime["volatility"], c.volatility, VOL_LEVELS),
        ]
        if c.squeeze != "any":
            parts.append(1.0 if bool(c.squeeze) == regime["squeeze"] else 0.3)
        out.append({"strategy_id": s.id, "rule_score": round(100 * sum(parts) / len(parts))})
    return sorted(out, key=lambda x: -x["rule_score"])
