"""Daily market features and regime labels, computed only from data available at each day's close.

Row D of the feature table describes session D and everything before it. A recommendation for
session D+1 uses row D, so nothing from D+1 (or later) can leak in. Regime thresholds are trailing
medians of earlier days only.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .ingest import IST, SESSION_LAST_BAR, SESSION_OPEN

TREND_LOOKBACK = 10        # days for the efficiency ratio
THRESHOLD_WINDOW = 120     # days of history for the trailing-median regime thresholds
THRESHOLD_MIN_OBS = 10     # fewer earlier days than this -> regime "unknown"

FEATURE_LABELS = {
    "ret_1d": ("Day return", "%"),
    "ret_5d": ("5-day return", "%"),
    "ret_20d": ("20-day return", "%"),
    "range_pct": ("Day range", "%"),
    "gap_pct": ("Opening gap", "%"),
    "atr14_pct": ("ATR(14) as % of price", "%"),
    "rv10": ("10-day realised volatility (annualised)", "%"),
    "eff10": ("10-day trend efficiency (0 = choppy, 1 = straight line)", ""),
    "dist_ema20_pct": ("Close vs 20-day EMA", "%"),
    "id_eff": ("Intraday trend efficiency", ""),
    "st_dir_close": ("Supertrend direction at close (+1 up / -1 down)", ""),
    "st_flips": ("Supertrend flips during the session", ""),
    "close_vs_vwap_pct": ("Close vs VWAP", "%"),
    "above_vwap_share": ("Share of minutes above VWAP", "%"),
    "close_vs_ema_pct": ("Close vs EMA (chart EMA)", "%"),
    "rsi_close": ("RSI at close", ""),
    "rsi_chg_60": ("RSI change over the last hour", ""),
    "relvol": ("Volume vs its 20-session average", "x"),
    "last_hour_ret": ("Last-hour return", "%"),
    "first_hour_range_share": ("Share of the day's range made in the first hour", "%"),
}
PCT_FEATURES = {k for k, (_, u) in FEATURE_LABELS.items() if u == "%"}


def load_bars(db, symbol: str, interval: str) -> pd.DataFrame:
    rows = db.all("SELECT ts, day, open, high, low, close, volume, vwap, st_line, st_dir, ema, rsi, rsi_ma "
                  "FROM market_bars WHERE symbol = ? AND interval = ? ORDER BY ts", (symbol, interval))
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(IST)
    return df


def classify_sessions(m1: pd.DataFrame) -> pd.Series:
    """day -> normal | partial | special for 1-minute data."""
    g = m1.groupby("day")["ts"]
    first, last = g.min(), g.max()
    out = {}
    for day in first.index:
        f, l = first[day], last[day]
        if f.weekday() >= 5 or f.time().hour >= 12:
            out[day] = "special"
        elif f.time() > SESSION_OPEN or l.time() < SESSION_LAST_BAR:
            out[day] = "partial"
        else:
            out[day] = "normal"
    return pd.Series(out, dtype=object)


def intraday_features(m1: pd.DataFrame, flags: pd.Series) -> pd.DataFrame:
    rows = {}
    vols = []
    for day, g in m1.groupby("day", sort=True):
        if flags.get(day) != "normal":
            continue
        c = g["close"].to_numpy()
        o0 = g["open"].iloc[0]
        path = np.abs(np.diff(c)).sum()
        hi, lo = g["high"].max(), g["low"].min()
        f = {"id_eff": abs(c[-1] - o0) / path if path > 0 else np.nan}
        if g["st_dir"].notna().any():
            d = g["st_dir"].dropna().to_numpy()
            f["st_dir_close"] = float(d[-1])
            f["st_flips"] = float((np.diff(d) != 0).sum())
        if g["vwap"].notna().iloc[-1]:
            f["close_vs_vwap_pct"] = c[-1] / g["vwap"].iloc[-1] - 1
            f["above_vwap_share"] = float((g["close"] > g["vwap"]).mean())
        if g["ema"].notna().iloc[-1]:
            f["close_vs_ema_pct"] = c[-1] / g["ema"].iloc[-1] - 1
        r = g["rsi"].to_numpy()
        if not np.isnan(r[-1]):
            f["rsi_close"] = float(r[-1])
            if len(r) > 60 and not np.isnan(r[-61]):
                f["rsi_chg_60"] = float(r[-1] - r[-61])
        if len(c) > 60:
            f["last_hour_ret"] = c[-1] / c[-61] - 1
            first_hour = g.iloc[:60]
            f["first_hour_range_share"] = (first_hour["high"].max() - first_hour["low"].min()) / (hi - lo) if hi > lo else np.nan
        vol = g["volume"].sum() if g["volume"].notna().any() else np.nan
        # relative to the previous 20 normal sessions only (never including today)
        prior = [v for v in vols[-20:] if not np.isnan(v)]
        if not np.isnan(vol) and len(prior) >= 5:
            f["relvol"] = vol / np.mean(prior)
        vols.append(vol)
        rows[day] = f
    return pd.DataFrame.from_dict(rows, orient="index")


def daily_candles(db, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """Daily OHLC for a symbol: uploaded/fetched daily bars, filled in from normal 1-minute sessions."""
    m1 = load_bars(db, symbol, "1m")
    d1 = load_bars(db, symbol, "1d")
    flags = classify_sessions(m1) if not m1.empty else pd.Series(dtype=object)
    frames = []
    if not d1.empty:
        frames.append(d1.set_index("day")[["open", "high", "low", "close"]].assign(source="daily"))
    if not m1.empty:
        normal = m1[m1["day"].map(flags) == "normal"]
        agg = normal.groupby("day").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                        close=("close", "last"))
        frames.append(agg.assign(source="1m"))
    if not frames:
        return pd.DataFrame(), m1, flags
    daily = pd.concat(frames)
    daily = daily[~daily.index.duplicated(keep="first")].sort_index()     # daily bars win over 1m aggregates
    special = set(flags[flags == "special"].index)
    daily = daily[~daily.index.isin(special) & (pd.to_datetime(daily.index).weekday < 5)]
    return daily, m1, flags


def build_features(db, symbol: str) -> pd.DataFrame:
    daily, m1, flags = daily_candles(db, symbol)
    if daily.empty:
        return pd.DataFrame()
    c, h, l, o = daily["close"], daily["high"], daily["low"], daily["open"]
    prev = c.shift(1)
    f = pd.DataFrame(index=daily.index)
    f["close"] = c
    f["ret_1d"] = c / prev - 1
    f["ret_5d"] = c / c.shift(5) - 1
    f["ret_20d"] = c / c.shift(20) - 1
    f["range_pct"] = (h - l) / prev
    f["gap_pct"] = o / prev - 1
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    f["atr14_pct"] = atr / c
    f["rv10"] = np.log(c / prev).rolling(10).std() * np.sqrt(252)
    move = (c - c.shift(TREND_LOOKBACK)).abs()
    path = c.diff().abs().rolling(TREND_LOOKBACK).sum()
    f["eff10"] = move / path
    ema20 = c.ewm(span=20, adjust=False, min_periods=10).mean()
    f["dist_ema20_pct"] = c / ema20 - 1
    if not m1.empty:
        f = f.join(intraday_features(m1, flags), how="left")
    f["source"] = daily["source"]
    return add_regimes(f)


def _trailing_median(s: pd.Series) -> pd.Series:
    """Median of the previous THRESHOLD_WINDOW values, excluding the current one."""
    return s.shift(1).rolling(THRESHOLD_WINDOW, min_periods=THRESHOLD_MIN_OBS).median()


def add_regimes(f: pd.DataFrame) -> pd.DataFrame:
    eff_t, vol_t = _trailing_median(f["eff10"]), _trailing_median(f["atr14_pct"])
    trend = np.where(f["eff10"].isna() | eff_t.isna(), "unknown",
                     np.where(f["eff10"] >= eff_t, "trending", "range-bound"))
    vol = np.where(f["atr14_pct"].isna() | vol_t.isna(), "unknown",
                   np.where(f["atr14_pct"] >= vol_t, "high-vol", "low-vol"))
    f["trend_threshold"] = eff_t
    f["vol_threshold"] = vol_t
    f["trend"] = trend
    f["volatility"] = vol
    f["direction"] = np.where(f["dist_ema20_pct"].isna(), "unknown",
                              np.where(f["dist_ema20_pct"] >= 0, "bullish", "bearish"))
    f["regime"] = [f"{t} / {v}" if "unknown" not in (t, v) else "unknown" for t, v in zip(trend, vol)]
    return f


def next_session(day: str, holidays: set[str]) -> str:
    d = date.fromisoformat(day)
    while True:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            return d.isoformat()


def describe(row: pd.Series) -> list[dict]:
    """Human-readable key readings for a feature row."""
    out = []
    for k, (label, unit) in FEATURE_LABELS.items():
        v = row.get(k)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        shown = f"{v * 100:+.2f}%" if unit == "%" and k not in ("above_vwap_share", "first_hour_range_share") else \
            f"{v * 100:.0f}%" if unit == "%" else f"{v:.2f}x" if unit == "x" else f"{v:.2f}"
        out.append({"key": k, "label": label, "value": float(v), "shown": shown})
    return out
