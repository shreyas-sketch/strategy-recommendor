"""Plain pandas/numpy technical indicators (no TA-Lib dependency)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _wilder(true_range(df), n)
    plus_di = 100 * _wilder(plus_dm, n) / tr
    minus_di = 100 * _wilder(minus_dm, n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame({"adx": _wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di})


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), n)
    loss = _wilder(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(loss.notna())


def bollinger_bandwidth(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return (2 * k * sd) / mid


def efficiency_ratio(close: pd.Series, n: int) -> float:
    """Kaufman ER: 1 = straight-line trend, 0 = pure noise."""
    seg = close.iloc[-(n + 1):]
    path = seg.diff().abs().sum()
    return float(abs(seg.iloc[-1] - seg.iloc[0]) / path) if path else 0.0


def choppiness(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Choppiness Index: > 61.8 choppy / ranging, < 38.2 trending."""
    tr_sum = true_range(df).rolling(n).sum()
    span = df["high"].rolling(n).max() - df["low"].rolling(n).min()
    return 100 * np.log10(tr_sum / span.replace(0, np.nan)) / np.log10(n)


def percentile_rank(s: pd.Series, value: float) -> float:
    s = s.dropna()
    if s.empty or value is None or np.isnan(value):
        return float("nan")
    return float((s <= value).mean() * 100)


def max_drawdown(close: pd.Series) -> float:
    peak = close.cummax()
    return float(((close / peak) - 1).min())
