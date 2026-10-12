"""Common contract for market-data providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]
IST = "Asia/Kolkata"


class ProviderError(RuntimeError):
    """Raised when a provider cannot return data (missing key, bad symbol, HTTP error)."""


class ProviderNotConfigured(ProviderError):
    """Provider has no credentials; the router skips it silently."""


@dataclass(frozen=True)
class FetchSpec:
    interval: str  # "15m" | "1h" | "1d"
    start: datetime
    end: datetime


class Provider:
    name: str = "base"

    def fetch(self, instrument: dict, spec: FetchSpec) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a provider frame to float OHLCV columns on a sorted, tz-aware IST index."""
    if df is None or df.empty:
        raise ProviderError("no candles returned")
    df = df.rename(columns=str.lower)[COLUMNS].apply(pd.to_numeric, errors="coerce")
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0)
    if df.empty:
        raise ProviderError("no valid candles after cleaning")
    return df
