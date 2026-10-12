"""Portfolio upload parsing and the personal "strategy for today" recommendation."""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from pathlib import Path

import anthropic
import pandas as pd
import requests

from .analysis.regime import rule_scores
from .data.base import ProviderError
from .data.providers import now_ist
from .expiries import expiry_context, schedule_check
from .llm.recommender import NO_TRADE, RecommendationRefused
from .profile import TradingProfile, apply_profile, profile_for_prompt, split
from .service import RecommendationService, _RulesOnly, rules_recommendation, strategy_cards, today_ist, trading_day

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 1_000_000
MAX_ROWS = 300
MAX_ANALYSED_HOLDINGS = 15
SYMBOL_OK = re.compile(r"^[A-Z0-9&\-]{1,20}$")
ISIN_OK = re.compile(r"^IN[A-Z0-9]{10}$")

ALIASES = {
    "symbol": {"symbol", "tradingsymbol", "trading symbol", "ticker", "scrip", "scrip code", "nse symbol",
               "instrument", "stock", "stock symbol", "security", "stock name", "scrip name", "company",
               "company name", "name", "security name"},
    "isin": {"isin", "isin code"},
    "name": {"stock name", "company", "company name", "security name", "scrip name", "name"},
    "quantity": {"quantity", "qty", "qty.", "shares", "quantity available", "available qty", "net qty",
                 "holding quantity", "holdings", "units", "total quantity", "free quantity"},
    "avg_price": {"avg price", "avg. price", "average price", "avg cost", "avg. cost", "average cost",
                  "buy price", "avg buy price", "average buy price", "buy avg", "avg. buy price", "cost price"},
}
# Columns that hold a tradable symbol rather than a company name.
SYMBOL_FIRST = ["symbol", "tradingsymbol", "trading symbol", "ticker", "nse symbol", "stock symbol", "scrip code"]

TEMPLATE_CSV = "symbol,quantity,avg_price\nRELIANCE,10,2450.50\nHDFCBANK,25,1610\nINFY,15,1500\n"


class PortfolioError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _norm(cell) -> str:
    return re.sub(r"\s+", " ", str(cell or "").strip().lower().replace("_", " "))


def _text(cell) -> str:
    """Cell as stripped text; empty for blanks (pandas gives NaN for empty cells)."""
    if cell is None or (isinstance(cell, float) and cell != cell):
        return ""
    text = str(cell).strip()
    return "" if text.lower() in {"nan", "none", "null", "-"} else text


def _num(cell) -> float | None:
    text = re.sub(r"[₹,\s]", "", _text(cell))
    if not text:
        return None
    try:
        val = float(text)
    except ValueError:
        return None
    return val if val == val else None  # drop NaN


def clean_symbol(raw) -> str | None:
    text = str(raw or "").strip().upper()
    text = re.sub(r"^(NSE|BSE)[:_\-]", "", text)
    text = re.sub(r"(\.NS|\.BO|-EQ|-BE|-BZ)$", "", text)
    return text if SYMBOL_OK.match(text) else None


def _read_table(filename: str, data: bytes) -> pd.DataFrame:
    if len(data) > MAX_UPLOAD_BYTES:
        raise PortfolioError("File is larger than 1 MB")
    if data[:2] == b"PK":  # xlsx (zip)
        try:
            return pd.read_excel(io.BytesIO(data), header=None, dtype=str, engine="openpyxl", nrows=MAX_ROWS + 40)
        except Exception as exc:
            raise PortfolioError("Could not read the Excel file. Export holdings as .xlsx or .csv") from exc
    if not filename.lower().endswith((".csv", ".txt")) and b"\x00" in data[:1024]:
        raise PortfolioError("Unsupported file type. Upload a .csv or .xlsx holdings export")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        return pd.read_csv(io.StringIO(text), header=None, dtype=str, sep=None, engine="python",
                           nrows=MAX_ROWS + 40, on_bad_lines="skip")
    except Exception as exc:
        raise PortfolioError("Could not read the CSV file") from exc


def _find_header(df: pd.DataFrame) -> tuple[int, dict[str, int]]:
    for i in range(min(40, len(df))):
        cells = [_norm(c) for c in df.iloc[i].tolist()]
        cols: dict[str, int] = {}
        for field, names in ALIASES.items():
            # prefer real symbol columns over company-name columns
            order = SYMBOL_FIRST + sorted(names - set(SYMBOL_FIRST)) if field == "symbol" else sorted(names)
            for name in order:
                if name in cells:
                    cols[field] = cells.index(name)
                    break
        if cols.get("name") == cols.get("symbol"):
            cols.pop("name", None)
        if "quantity" in cols and ("symbol" in cols or "isin" in cols):
            return i, cols
    raise PortfolioError("Could not find 'symbol' and 'quantity' columns. Use the template CSV, "
                         "or a holdings export from your broker.")


def parse_portfolio(filename: str, data: bytes, isin_resolver=None) -> tuple[list[dict], list[str]]:
    """Return (holdings, unresolved_names). Raw file contents are never stored."""
    df = _read_table(filename, data)
    header, cols = _find_header(df)
    rows = df.iloc[header + 1:]
    merged: dict[str, dict] = {}
    unresolved: list[str] = []
    for _, row in rows.iterrows():
        qty = _num(row.iloc[cols["quantity"]])
        raw_name = _text(row.iloc[cols["symbol"]]) if "symbol" in cols else ""
        isin = _text(row.iloc[cols["isin"]]).upper() if "isin" in cols else ""
        if not qty or qty <= 0 or raw_name.lower() == "total" or not (raw_name or isin):
            continue
        symbol = clean_symbol(raw_name) if raw_name else None
        if symbol is None and isin_resolver is not None and ISIN_OK.match(isin):
            symbol = isin_resolver(isin)
        if symbol is None:
            label = raw_name or isin
            if "name" in cols and _text(row.iloc[cols["name"]]):
                label = _text(row.iloc[cols["name"]])
            unresolved.append(label[:60])
            continue
        avg = _num(row.iloc[cols["avg_price"]]) if "avg_price" in cols else None
        h = merged.setdefault(symbol, {"symbol": symbol, "quantity": 0.0, "cost": 0.0, "priced_qty": 0.0})
        h["quantity"] += qty
        if avg and avg > 0:
            h["cost"] += qty * avg
            h["priced_qty"] += qty
        if len(merged) > MAX_ROWS:
            raise PortfolioError(f"Too many holdings (max {MAX_ROWS})")
    holdings = [{"symbol": h["symbol"], "quantity": round(h["quantity"], 4),
                 "avg_price": round(h["cost"] / h["priced_qty"], 2) if h["priced_qty"] else None}
                for h in merged.values()]
    if not holdings:
        raise PortfolioError("No holdings found in the file" + (
            f" — could not match: {', '.join(unresolved[:5])}" if unresolved else ""))
    return sorted(holdings, key=lambda h: h["symbol"]), unresolved[:50]


class GrowwIsinResolver:
    """Best-effort ISIN -> NSE trading symbol using Groww's public instrument list (refreshed daily).

    Broker exports (e.g. Groww) often list company names + ISIN rather than trading symbols.
    If the list is unreachable or lacks an ISIN column, those rows are reported back as unresolved.
    """

    URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"

    def __init__(self, cache_dir: Path):
        self.path = Path(cache_dir) / "instruments.csv"
        self._map: dict[str, str] | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        if not self.path.exists() or time.time() - self.path.stat().st_mtime > 86400:
            try:
                resp = requests.get(self.URL, timeout=30)
                resp.raise_for_status()
                self.path.write_bytes(resp.content)
            except requests.RequestException as exc:
                log.warning("instrument list download failed: %s", type(exc).__name__)
        if not self.path.exists():
            return {}
        df = pd.read_csv(self.path, dtype=str)
        if not {"isin", "trading_symbol"} <= set(df.columns):
            return {}
        if "exchange" in df.columns:
            df = df[df["exchange"] == "NSE"]
        if "segment" in df.columns:
            df = df[df["segment"] == "CASH"]
        return dict(zip(df["isin"].str.upper(), df["trading_symbol"].str.upper()))

    def __call__(self, isin: str) -> str | None:
        with self._lock:
            if self._map is None or time.time() - self._loaded_at > 86400:
                self._map = self._load()
                self._loaded_at = time.time()
            return clean_symbol(self._map.get(isin)) if self._map.get(isin) else None


# ---------------------------------------------------------------------------
# Personal daily recommendation
# ---------------------------------------------------------------------------
def _holding_view(h: dict, snap: dict | None) -> dict:
    view = {"symbol": h["symbol"]}
    if not snap:
        view["data"] = "unavailable"
        return view
    p, g = snap["primary"], snap["regime"]
    last = p.get("last_close")
    view.update({
        "last_close": last,
        "return_22d_pct": p.get("window_return_pct"),
        "trend": g["trend"], "trend_strength": g["trend_strength"], "volatility": g["volatility"],
        "rsi14": p.get("rsi14"), "adx": p.get("adx"), "atr_pct": p.get("atr_pct"),
        "max_drawdown_22d_pct": p.get("max_drawdown_pct"),
    })
    if h.get("avg_price") and last:
        view["unrealised_pnl_pct"] = round((last / h["avg_price"] - 1) * 100, 2)
    return view


def portfolio_context(service: RecommendationService, holdings: list[dict], benchmark: dict) -> dict:
    # Analyse the largest positions (by cost, else quantity); data is shared/cached across participants.
    ranked = sorted(holdings, key=lambda h: -(h["quantity"] * (h.get("avg_price") or 1)))
    analysed = ranked[:MAX_ANALYSED_HOLDINGS]
    views = []
    for h in analysed:
        v = _holding_view(h, service.symbol_daily(h["symbol"]))
        price = v.get("last_close") or h.get("avg_price")
        v["_value"] = h["quantity"] * price if price else 0.0
        views.append(v)
    total = sum(v["_value"] for v in views) or 1.0
    by_trend = {"up": 0.0, "down": 0.0, "sideways": 0.0}
    for v in views:
        v["weight_pct"] = round(v.pop("_value") / total * 100, 1)
        if v.get("trend") in by_trend:
            by_trend[v["trend"]] += v["weight_pct"]
    weights = sorted((v["weight_pct"] for v in views), reverse=True)
    pnl = [(v["weight_pct"], v["unrealised_pnl_pct"]) for v in views if "unrealised_pnl_pct" in v]
    stats = {
        "holdings_total": len(holdings),
        "holdings_analysed": len(views),
        "largest_weight_pct": weights[0] if weights else None,
        "top3_weight_pct": round(sum(weights[:3]), 1) if weights else None,
        "weight_in_uptrend_pct": round(by_trend["up"], 1),
        "weight_in_downtrend_pct": round(by_trend["down"], 1),
        "weight_sideways_pct": round(by_trend["sideways"], 1),
        "weighted_unrealised_pnl_pct": round(sum(w * p for w, p in pnl) / sum(w for w, _ in pnl), 2) if pnl else None,
        "holdings_without_data": sum(1 for v in views if v.get("data") == "unavailable"),
    }
    return {"benchmark": benchmark, "portfolio_stats": stats,
            "holdings": sorted(views, key=lambda v: -v["weight_pct"])}


def _clean_param(text: str, limit: int) -> str:
    """WhatsApp template params may not contain newlines/tabs or long runs of spaces."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


class PortfolioAdvisor:
    def __init__(self, service: RecommendationService):
        self.service = service
        self.store = service.store
        self.settings = service.settings
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _user_lock(self, user_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(user_id, threading.Lock())

    def today(self, user_id: int, generate: bool = True) -> dict | None:
        """Today's personal recommendation for this participant (generated at most once per day)."""
        # Keyed by the session the pick is for: an evening visit prepares tomorrow's pick, which the
        # 06:00 job then reuses instead of paying for a second analysis.
        day = trading_day(self.settings.admin.market_holidays).isoformat()
        cached = self.store.get_daily_rec(user_id, day)
        if cached or not generate:
            return cached
        with self._user_lock(user_id):  # avoid double generation from parallel requests
            cached = self.store.get_daily_rec(user_id, day)
            if cached:
                return cached
            payload = self._generate(user_id, day)
            self.store.put_daily_rec(user_id, day, payload)
            return payload

    def profile(self, user_id: int) -> TradingProfile | None:
        raw = self.store.get_profile(user_id)
        return TradingProfile(**raw) if raw else None

    def _markets(self, keys: list[str]) -> tuple[dict, dict, str | None]:
        """Snapshots for each index the eligible strategies trade (shared, cached across participants)."""
        markets, views, provider = {}, {}, None
        for key in keys:
            inst = self.settings.instrument(key)
            try:
                snap, provider = self.service.market_snapshot(inst, "1d")
            except Exception as exc:
                log.warning("no market data for %s: %s", key, type(exc).__name__)
                continue
            markets[key] = {"label": inst["label"], "regime": snap["regime"]}
            views[key] = {"instrument": inst["label"], "regime": snap["regime"],
                          "primary": {k: snap["primary"].get(k) for k in (
                              "window_return_pct", "adx", "efficiency_ratio", "choppiness", "rsi14",
                              "realized_vol_annual_pct", "vol_percentile", "bb_width_percentile", "atr_pct")},
                          "daily_context": snap.get("daily_context")}
        return markets, views, provider

    def _generate(self, user_id: int, day: str) -> dict:
        from datetime import date
        target = date.fromisoformat(day)       # the session this pick is for
        admin = self.settings.admin
        strategies = self.settings.strategies_for("1d")
        if not strategies:
            raise ValueError("No strategies are configured for the 1-day horizon")
        profile = self.profile(user_id)
        eligible, excluded = split(strategies, profile)
        # Strategies pinned to expiry days are dropped on days they won't trade (Groww expiry calendar).
        calendar = self.service.calendar
        tradeable = []
        for s in eligible:
            why = schedule_check(s, calendar, target)
            if why:
                excluded.append({"strategy_id": s.id, "reasons": [why], "kind": "schedule"})
            else:
                tradeable.append(s)
        eligible = tradeable
        eligible_ids = {s.id for s in eligible}
        names = {s.id: s.name for s in strategies}
        cards = strategy_cards(strategies)
        portfolio = self.store.get_portfolio(user_id)
        meta = {"day": day, "engine": "claude", "benchmark": None, "data_provider": None,
                "has_portfolio": bool(portfolio), "has_profile": profile is not None, "note": None,
                "generated_at": time.time()}

        if not eligible:
            # Nothing suits this participant: no market data or AI call needed.
            rec = {"market_summary": "", "portfolio_summary": "", "confidence": "high", "top_strategy_id": NO_TRADE,
                   "run_on": "", "why": "", "rankings": [], "deciding_factors": [], "portfolio_risks": [],
                   "why_top_over_others": ("None of the strategies that suit you trade today."
                                           if any(e["kind"] == "schedule" for e in excluded) else
                                           "None of the platform's strategies suit your trading profile yet. "
                                           "Review your profile in Settings or ask the admin."),
                   "whatsapp_line": "No strategy for you today.", "excluded": excluded}
            meta.update(engine="rules")
            return {"recommendation": rec, "strategies": cards, "meta": meta}

        # Each strategy is judged on the index it trades; the benchmark covers generic strategies + portfolio.
        bench_key = self.settings.instrument(admin.portfolio_benchmark)["key"]
        keys = list(dict.fromkeys([u for s in eligible for u in s.underlyings]))
        if portfolio or any(not s.underlyings for s in eligible) or not keys:
            keys = list(dict.fromkeys(keys + [bench_key]))
        markets, views, provider = self._markets(keys)
        if not markets:
            raise ProviderError("market data unavailable for " + ", ".join(keys))
        bench = markets.get(bench_key)
        meta.update(data_provider=provider, benchmark=self.settings.instrument(bench_key)["label"],
                    markets=[m["label"] for m in markets.values()])
        scores = rule_scores(bench["regime"] if bench else None, eligible, markets)
        if not scores:
            raise ProviderError("market data unavailable for the indices these strategies trade")
        scored = {sc["strategy_id"] for sc in scores}
        judged = [s for s in eligible if s.id in scored]

        context = portfolio_context(self.service, portfolio["holdings"], views.get(bench_key)) if portfolio else None
        expiries = expiry_context(calendar, list(markets), target)
        prompt_context = {
            "trading_day": {"date": day, "weekday": target.strftime("%A")},
            "underlying_markets": views,
            "expiry_calendar": expiries,
            "portfolio": context,
            "participant_profile": profile_for_prompt(profile),
            "eligible_strategy_ids": sorted(s.id for s in judged),
            "excluded_strategies": excluded,
        }

        # Without a portfolio the answer depends only on markets + profile -> share it across participants.
        shared_key = None
        if not portfolio:
            engine = self.service.resolve_engine()
            shared_key = "|".join(["today-v2", day, engine["engine"], engine["model"] or "-", admin.effort,
                                   self.settings.catalog_hash(), ",".join(sorted(s.id for s in judged)),
                                   json.dumps(profile_for_prompt(profile), sort_keys=True)])
            hit = self.store.cache_get(shared_key, 20 * 3600, time.time())
            if hit:
                meta.update(hit["meta_extra"], shared=True)
                return {"recommendation": hit["rec"], "markets": views, "expiries": expiries, "strategies": cards,
                        "meta": meta}

        rec = None
        try:
            parsed, info = self.service.call_claude(
                self.service.recommender.recommend_portfolio, label="PORTFOLIO" if portfolio else "TODAY",
                horizon="1d", catalog=strategies, eligible=judged, context=prompt_context, rule_scores=scores)
            rec = parsed.model_dump()
            meta.update(model=info["model"], cost_usd=info["cost_usd"], note=info["note"])
        except _RulesOnly as exc:
            meta.update(engine="rules", note=str(exc) or None)
        except (anthropic.APIError, RecommendationRefused, RuntimeError) as exc:
            log.error("daily Claude call failed: %s", type(exc).__name__)
            meta.update(engine="rules", note="AI analysis unavailable — showing rule-engine result.")

        if rec is None:
            rec = rules_recommendation(None, scores, judged)
            if context:
                st = context["portfolio_stats"]
                summary = (f"{st['weight_in_uptrend_pct']}% of your analysed holdings are trending up, "
                           f"{st['weight_in_downtrend_pct']}% down, {st['weight_sideways_pct']}% sideways.")
                risks = ([f"Concentrated: top 3 holdings are {st['top3_weight_pct']}% of value"]
                         if (st["top3_weight_pct"] or 0) > 60 else [])
            else:
                summary, risks = "No portfolio uploaded — based on the markets and your profile.", []
            top_sc = next((sc for sc in scores if sc["strategy_id"] == rec["top_strategy_id"]), None)
            rec.update(run_on=(top_sc or {}).get("on") or meta["benchmark"], portfolio_summary=summary,
                       portfolio_risks=risks, whatsapp_line=_clean_param(rec["why_top_over_others"], 150))
        rec = apply_profile(rec, eligible_ids, excluded, names)   # safety net + "excluded" list
        rec["whatsapp_line"] = _clean_param(rec.get("whatsapp_line", ""), 150)
        if rec["top_strategy_id"] != NO_TRADE:
            top = next((s for s in strategies if s.id == rec["top_strategy_id"]), None)
            run_on = rec.get("run_on") or ""
            if top and top.underlyings:
                # A strategy that trades a specific index always runs on that index, whatever the model wrote.
                labels = [self.settings.instrument(u)["label"] for u in top.underlyings]
                if not any(x.upper() in run_on.upper() for x in labels + list(top.underlyings)):
                    run_on = ", ".join(labels)
            rec["run_on"] = _clean_param(run_on or meta["benchmark"], 40)
        if shared_key:
            self.store.cache_put(shared_key, {"rec": rec, "meta_extra": {k: meta.get(k) for k in (
                "engine", "model", "note", "data_provider", "benchmark", "markets")}}, time.time())
        out = {"recommendation": rec, "markets": views, "expiries": expiries, "strategies": cards, "meta": meta}
        if context:
            out["context"] = context
        return out
