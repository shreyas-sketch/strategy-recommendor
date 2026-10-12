"""Groww, Twelve Data and yfinance candle providers plus a fallback router."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

import pandas as pd
import requests

from .base import IST, FetchSpec, Provider, ProviderError, ProviderNotConfigured, normalize

log = logging.getLogger(__name__)
HTTP_TIMEOUT = 20


class GrowwProvider(Provider):
    """Groww Trade API historical candles (NSE/BSE). Mirrors growwapi 1.5 `get_historical_candles`."""

    name = "groww"
    BASE = "https://api.groww.in/v1"
    INTERVALS = {"15m": "15minute", "1h": "1hour", "1d": "1day"}

    def __init__(self) -> None:
        self._token: str | None = os.environ.get("GROWW_ACCESS_TOKEN") or None
        self._lock = threading.Lock()

    @staticmethod
    def _headers(token: str) -> dict:
        return {
            "x-request-id": str(uuid.uuid4()),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-api-version": "1.0",
        }

    def _mint_token(self) -> str:
        api_key = os.environ.get("GROWW_API_KEY")
        secret = os.environ.get("GROWW_API_SECRET")
        if not (api_key and secret):
            raise ProviderNotConfigured("set GROWW_ACCESS_TOKEN or GROWW_API_KEY + GROWW_API_SECRET")
        ts = int(time.time())
        checksum = hashlib.sha256(f"{secret}{ts}".encode()).hexdigest()
        resp = requests.post(
            f"{self.BASE}/token/api/access",
            headers=self._headers(api_key),
            json={"key_type": "approval", "checksum": checksum, "timestamp": ts},
            timeout=HTTP_TIMEOUT,
        )
        if not resp.ok:
            raise ProviderError(f"Groww token request failed: HTTP {resp.status_code}")
        return resp.json()["token"]

    def _get_token(self, refresh: bool = False) -> str:
        with self._lock:
            if refresh or not self._token:
                self._token = self._mint_token()
            return self._token

    def fetch(self, instrument: dict, spec: FetchSpec) -> pd.DataFrame:
        cfg = instrument.get("groww")
        if not cfg:
            raise ProviderError("instrument has no Groww mapping")
        params = {
            "exchange": cfg["exchange"],
            "segment": cfg["segment"],
            "groww_symbol": cfg["groww_symbol"],
            "start_time": spec.start.astimezone(_ist()).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": spec.end.astimezone(_ist()).strftime("%Y-%m-%d %H:%M:%S"),
            "candle_interval": self.INTERVALS[spec.interval],
        }
        token = self._get_token()
        resp = requests.get(f"{self.BASE}/historical/candles", headers=self._headers(token),
                            params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code == 401 and os.environ.get("GROWW_API_KEY"):
            token = self._get_token(refresh=True)
            resp = requests.get(f"{self.BASE}/historical/candles", headers=self._headers(token),
                                params=params, timeout=HTTP_TIMEOUT)
        body = _json(resp)
        if not resp.ok or body.get("status") == "FAILURE":
            msg = (body.get("error") or {}).get("message", f"HTTP {resp.status_code}")
            raise ProviderError(f"Groww: {msg}")
        candles = (body.get("payload") or body).get("candles") or []
        return normalize(parse_candle_rows(candles))


    def fetch_expiries(self, exchange: str, underlying: str, year: int, month: int | None = None) -> list[str]:
        """F&O expiry dates (YYYY-MM-DD) for an underlying. Mirrors growwapi 1.5 `get_expiries`."""
        params = {"exchange": exchange, "underlying_symbol": underlying, "year": str(year)}
        if month:
            params["month"] = str(month)
        token = self._get_token()
        resp = requests.get(f"{self.BASE}/historical/expiries", headers=self._headers(token),
                            params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code == 401 and os.environ.get("GROWW_API_KEY"):
            resp = requests.get(f"{self.BASE}/historical/expiries", headers=self._headers(self._get_token(refresh=True)),
                                params=params, timeout=HTTP_TIMEOUT)
        body = _json(resp)
        if not resp.ok or body.get("status") == "FAILURE":
            msg = (body.get("error") or {}).get("message", f"HTTP {resp.status_code}")
            raise ProviderError(f"Groww expiries: {msg}")
        # The SDK documents only "the list of expiry dates"; collect every ISO date in the payload.
        return sorted(set(re.findall(r"\b(\d{4}-\d{2}-\d{2})", json.dumps(body.get("payload", body)))))


class TwelveDataProvider(Provider):
    name = "twelvedata"
    URL = "https://api.twelvedata.com/time_series"
    INTERVALS = {"15m": "15min", "1h": "1h", "1d": "1day"}

    def fetch(self, instrument: dict, spec: FetchSpec) -> pd.DataFrame:
        key = os.environ.get("TWELVEDATA_API_KEY")
        if not key:
            raise ProviderNotConfigured("TWELVEDATA_API_KEY not set")
        cfg = instrument.get("twelvedata")
        if not cfg:
            raise ProviderError("instrument has no Twelve Data mapping")
        params = {
            "symbol": cfg["symbol"],
            "interval": self.INTERVALS[spec.interval],
            "start_date": spec.start.astimezone(_ist()).strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": spec.end.astimezone(_ist()).strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": IST,
            "outputsize": 5000,
            "apikey": key,
        }
        if cfg.get("exchange"):
            params["exchange"] = cfg["exchange"]
        resp = requests.get(self.URL, params=params, timeout=HTTP_TIMEOUT)
        body = _json(resp)
        if not resp.ok or body.get("status") == "error":
            raise ProviderError(f"Twelve Data: {body.get('message', f'HTTP {resp.status_code}')}")
        values = body.get("values") or []
        df = pd.DataFrame(values)
        if df.empty:
            raise ProviderError("Twelve Data: no values")
        if "volume" not in df:
            df["volume"] = 0
        df.index = pd.to_datetime(df.pop("datetime"))
        return normalize(df)


class YFinanceProvider(Provider):
    """Free, no key. Intraday history is limited (15m ≈ 60 days) and data can lag ~15 min."""

    name = "yfinance"

    def fetch(self, instrument: dict, spec: FetchSpec) -> pd.DataFrame:
        import yfinance as yf

        ticker = instrument.get("yfinance")
        if not ticker:
            raise ProviderError("instrument has no yfinance mapping")
        try:
            df = yf.Ticker(ticker).history(start=spec.start, end=spec.end + timedelta(days=1),
                                           interval=spec.interval, auto_adjust=False)
        except Exception as exc:  # yfinance raises a zoo of exception types
            raise ProviderError(f"yfinance: {exc}") from exc
        return normalize(df)


class DataRouter:
    """Tries providers in the admin-configured order; first success wins."""

    def __init__(self, providers: dict[str, Provider] | None = None):
        self.providers = providers or {
            p.name: p for p in (GrowwProvider(), TwelveDataProvider(), YFinanceProvider())
        }

    def fetch(self, instrument: dict, spec: FetchSpec, order: list[str]) -> tuple[pd.DataFrame, str]:
        errors = []
        for name in order:
            provider = self.providers.get(name)
            if provider is None:
                continue
            try:
                return provider.fetch(instrument, spec), name
            except ProviderNotConfigured:
                continue
            except (ProviderError, requests.RequestException, KeyError, ValueError) as exc:
                log.warning("provider %s failed for %s: %s", name, instrument.get("key"), exc)
                errors.append(f"{name}: {exc}")
        raise ProviderError("all data providers failed — " + ("; ".join(errors) or "none configured"))


def parse_candle_rows(rows: list) -> pd.DataFrame:
    """Groww returns [timestamp, open, high, low, close, volume, ...]; timestamp may be epoch s/ms or ISO."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([r[:6] for r in rows], columns=["ts", "open", "high", "low", "close", "volume"])
    ts = df.pop("ts")
    if pd.api.types.is_numeric_dtype(ts):
        unit = "ms" if ts.iloc[0] > 10**11 else "s"
        df.index = pd.to_datetime(ts, unit=unit, utc=True)
    else:
        df.index = pd.to_datetime(ts)
    return df


def _json(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _ist():
    from zoneinfo import ZoneInfo
    return ZoneInfo(IST)


def now_ist() -> datetime:
    return datetime.now(_ist())
