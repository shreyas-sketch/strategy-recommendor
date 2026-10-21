"""Orchestrates storage, features, the engine and validation for the admin research pages."""

from __future__ import annotations

import hashlib
import io
import logging
import math
import threading
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ..data.base import FetchSpec, ProviderError
from . import analytics
from .backtest import regimes_for, walk_forward
from .db import ResearchDB, now_iso
from .engine import ENGINE_LABELS, ENGINES, LAKH, MODEL_VERSION, EngineConfig, attach_regimes, build_panel, score_day
from .features import build_features, describe, next_session
from .ingest import IngestError, match_strategy, parse_market, parse_strategy_pnl

log = logging.getLogger(__name__)
MARKET_SYMBOLS = ("NIFTY50", "SENSEX", "BANKNIFTY")
BENCHMARK = "NIFTY50"
ALLOWED_EXT = (".csv", ".txt", ".xlsx", ".xls", ".xlsm")


class DuplicateImport(ValueError):
    pass


def clean(obj):
    """JSON-safe: NaN/inf -> None, numpy scalars -> Python."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def rs(x: float | None, d: int = 0) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    s = f"{abs(x):,.{d}f}"
    return f"-₹{s}" if x < 0 else f"₹{s}"


class ResearchService:
    def __init__(self, settings, router=None, root: Path | None = None):
        self.settings = settings
        self.router = router
        self.db = ResearchDB(root or Path(settings.data_dir) / "research")
        self._lock = threading.RLock()
        self._cache: dict = {}
        self._seed_strategies()

    # ---- configuration ----------------------------------------------------
    def _seed_strategies(self) -> None:
        path = Path(self.settings.config_dir) / "research_strategies.yaml"
        if not path.exists():
            return
        seeded = set(self.db.get_kv("seeded_strategies", []))
        for s in yaml.safe_load(path.read_text()) or []:
            if s["id"] not in seeded:
                if not self.db.get_strategy(s["id"]):
                    self.db.upsert_strategy(s)
                seeded.add(s["id"])
        self.db.set_kv("seeded_strategies", sorted(seeded))

    @property
    def engine_name(self) -> str:
        e = self.db.get_kv("active_engine", "B_regime_shrunk")
        return e if e in ENGINES else "B_regime_shrunk"

    @property
    def config(self) -> EngineConfig:
        base = EngineConfig()
        for k, v in (self.db.get_kv("engine_config", {}) or {}).items():
            if hasattr(base, k):
                setattr(base, k, type(getattr(base, k))(v))
        return base

    def update_settings(self, engine: str | None = None, config: dict | None = None) -> dict:
        if engine is not None:
            if engine not in ENGINES:
                raise ValueError(f"Unknown engine {engine!r}")
            self.db.set_kv("active_engine", engine)
        if config:
            base = EngineConfig()
            bounds = {"shrink_k": (0, 500), "min_history": (10, 1000), "min_regime_days": (1, 500),
                      "lower_quantile": (0.01, 0.5), "tail_penalty": (0, 1), "bootstrap": (100, 5000),
                      "weekday_window": (2, 52), "weekday_min_active": (0, 1), "ridge_alpha": (0, 1e6)}
            current = self.db.get_kv("engine_config", {}) or {}
            for k, v in config.items():
                if k not in bounds:
                    raise ValueError(f"Unknown setting {k!r}")
                v = type(getattr(base, k))(v)
                lo, hi = bounds[k]
                if not lo <= v <= hi:
                    raise ValueError(f"{k} must be between {lo} and {hi}")
                current[k] = v
            self.db.set_kv("engine_config", current)
        self._invalidate()
        return self.settings_view()

    def settings_view(self) -> dict:
        return {"engine": self.engine_name, "engines": [{"id": e, "label": ENGINE_LABELS[e]} for e in ENGINES],
                "config": self.config.as_dict(), "model_version": MODEL_VERSION}

    def _holidays(self) -> set[str]:
        return set(getattr(self.settings.admin, "market_holidays", []) or [])

    # ---- cached analysis inputs -----------------------------------------
    def _invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _data(self) -> dict:
        with self._lock:
            if "data" in self._cache:
                return self._cache["data"]
            strategies = self.db.list_strategies(active_only=True)
            symbols = [r["symbol"] for r in self.db.all("SELECT DISTINCT symbol FROM market_bars")]
            features = {s: build_features(self.db, s) for s in symbols}
            features = {k: v for k, v in features.items() if not v.empty}
            days = self.db.all("SELECT strategy_id, day, gross_pnl, charges, trades FROM strategy_days ORDER BY day")
            panel = build_panel(strategies, days)
            if not panel.empty:
                panel = attach_regimes(panel, strategies, features, BENCHMARK)
            data = {"strategies": strategies, "features": features, "panel": panel}
            self._cache["data"] = data
            return data

    # ---- imports ------------------------------------------------------------
    def import_file(self, raw: bytes, filename: str, kind: str, target: str | None, uploaded_by: str,
                    replace: bool = False) -> dict:
        filename = Path(filename or "upload.csv").name
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise IngestError(f"Unsupported file type {ext or '(none)'}; upload CSV or Excel")
        if not raw:
            raise IngestError("The file is empty")
        sha = hashlib.sha256(raw).hexdigest()
        prior = self.db.import_by_sha(sha)
        if prior:
            raise DuplicateImport(f"This exact file was already imported on {prior['uploaded_at'][:10]} as "
                                  f"'{prior['filename']}' ({prior['kind']}, {prior['target']}). Nothing was changed.")
        if kind == "market":
            report = self._import_market(raw, filename, ext, sha, target, uploaded_by, replace)
        elif kind == "strategy_pnl":
            report = self._import_pnl(raw, filename, ext, sha, target, uploaded_by, replace)
        else:
            raise IngestError("kind must be 'market' or 'strategy_pnl'")
        self._invalidate()
        report["refresh"] = self.refresh_after_data_change()
        return clean(report)

    def _import_market(self, raw, filename, ext, sha, symbol, uploaded_by, replace) -> dict:
        symbol = (symbol or "").upper()
        if symbol not in MARKET_SYMBOLS:
            raise IngestError(f"Choose which index this file is for: {', '.join(MARKET_SYMBOLS)}")
        parsed = parse_market(raw, filename)
        stored = self.db.save_raw(sha, ext, raw)
        iid = self.db.record_import(sha256=sha, kind="market", target=symbol, filename=filename, stored_as=stored,
                                    size_bytes=len(raw), uploaded_by=uploaded_by, rows_in_file=parsed["rows_in_file"],
                                    first_date=parsed["first_day"], last_date=parsed["last_day"],
                                    details={"interval": parsed["interval"], "mapping": parsed["mapping"],
                                             "warnings": parsed["warnings"], "sessions": parsed["sessions"],
                                             "flagged_sessions": parsed["flagged_sessions"]})
        counts = self.db.upsert_bars(symbol, parsed["interval"], parsed["rows"], iid, replace=replace)
        self.db.write("UPDATE imports SET rows_added = ?, rows_skipped = ?, rows_conflicting = ? WHERE id = ?",
                      (counts["added"], counts["skipped"], counts["conflicting"], iid))
        warnings = list(parsed["warnings"])
        if counts["conflicting"]:
            warnings.append(f"{counts['conflicting']} bar(s) already stored with different prices were "
                            f"{'replaced' if replace else 'kept as they were (tick “replace” to overwrite)'}.")
        return {"import_id": iid, "kind": "market", "target": symbol, "interval": parsed["interval"],
                "rows_in_file": parsed["rows_in_file"], **counts, "first_day": parsed["first_day"],
                "last_day": parsed["last_day"], "sessions": parsed["sessions"], "mapping": parsed["mapping"],
                "warnings": warnings}

    def _import_pnl(self, raw, filename, ext, sha, target, uploaded_by, replace) -> dict:
        parsed = parse_strategy_pnl(raw, filename)
        strategies = self.db.list_strategies()
        assign: dict[str, list] = {}
        unknown = []
        for name, rows in parsed["groups"].items():
            sid = None
            if name is None or (len(parsed["groups"]) == 1 and target):
                sid = target
            else:
                sid = match_strategy(name, strategies)
            if not sid or not self.db.get_strategy(sid):
                unknown.append(name or "(no strategy column)")
                continue
            assign.setdefault(sid, []).extend(rows)
        if unknown:
            raise IngestError("Could not tell which strategy these rows belong to: " + ", ".join(unknown) +
                              ". Pick the strategy when uploading, or add it under Strategies first.")
        stored = self.db.save_raw(sha, ext, raw)
        iid = self.db.record_import(sha256=sha, kind="strategy_pnl", target=",".join(sorted(assign)), filename=filename,
                                    stored_as=stored, size_bytes=len(raw), uploaded_by=uploaded_by,
                                    rows_in_file=parsed["rows_in_file"], first_date=parsed["first_day"],
                                    last_date=parsed["last_day"],
                                    details={"mapping": parsed["mapping"], "basis": parsed["basis"],
                                             "warnings": parsed["warnings"], "trade_level": parsed["trade_level"]})
        totals = {"added": 0, "skipped": 0, "conflicting": 0}
        per = {}
        for sid, rows in assign.items():
            c = self.db.upsert_strategy_days(sid, rows, iid, replace=replace)
            per[sid] = {**c, "days": len(rows), "first_day": rows[0][0], "last_day": rows[-1][0]}
            for k in totals:
                totals[k] += c[k]
        self.db.write("UPDATE imports SET rows_added = ?, rows_skipped = ?, rows_conflicting = ? WHERE id = ?",
                      (totals["added"], totals["skipped"], totals["conflicting"], iid))
        warnings = list(parsed["warnings"])
        if totals["conflicting"]:
            warnings.append(f"{totals['conflicting']} day(s) already stored with a different P&L were "
                            f"{'replaced' if replace else 'kept as they were (tick “replace” to overwrite)'}.")
        return {"import_id": iid, "kind": "strategy_pnl", "strategies": per, **totals, "basis": parsed["basis"],
                "mapping": parsed["mapping"], "rows_in_file": parsed["rows_in_file"], "warnings": warnings}

    def fetch_daily(self, symbols: list[str], days: int = 600, uploaded_by: str = "") -> dict:
        """Daily candles from the app's market-data providers, stored like an uploaded file."""
        if self.router is None:
            raise ProviderError("No market-data providers configured")
        out = {}
        end = datetime.now()
        for sym in symbols:
            if sym not in MARKET_SYMBOLS:
                out[sym] = {"error": "unsupported index"}
                continue
            try:
                inst = self.settings.instrument(sym)
                df, provider = self.router.fetch(inst, FetchSpec("1d", end - timedelta(days=days), end),
                                                 self.settings.admin.provider_order)
            except (ProviderError, KeyError, ValueError) as exc:
                out[sym] = {"error": str(exc)}
                continue
            df = df[df.index.normalize() < pd.Timestamp(end.date(), tz=df.index.tz)]   # only completed sessions
            buf = io.StringIO()
            df.rename_axis("time").reset_index().assign(
                time=lambda x: x["time"].dt.strftime("%Y-%m-%dT00:00:00+05:30")).to_csv(buf, index=False)
            raw = buf.getvalue().encode()
            try:
                rep = self.import_file(raw, f"{sym}_daily_{provider}_{end:%Y%m%d}.csv", "market", sym, uploaded_by)
                out[sym] = {"provider": provider, "added": rep["added"], "skipped": rep["skipped"],
                            "last_day": rep["last_day"]}
            except DuplicateImport:
                out[sym] = {"provider": provider, "added": 0, "note": "already up to date"}
            except IngestError as exc:
                out[sym] = {"error": str(exc)}
        return out

    # ---- strategies -----------------------------------------------------------
    def save_strategy(self, s: dict, create: bool = False) -> dict:
        sid = str(s.get("id", "")).strip()
        if not sid or not all(c.isalnum() or c == "_" for c in sid) or sid != sid.lower() or len(sid) > 60:
            raise ValueError("id must be lowercase letters, digits or _")
        exists = self.db.get_strategy(sid)
        if create and exists:
            raise ValueError(f"Strategy {sid!r} already exists")
        if not create and not exists:
            raise KeyError(sid)
        name = str(s.get("name", "")).strip()[:80]
        if not name:
            raise ValueError("name is required")
        underlying = str(s.get("underlying", "")).upper()
        if underlying not in MARKET_SYMBOLS:
            raise ValueError(f"underlying must be one of {', '.join(MARKET_SYMBOLS)}")
        margin = float(s.get("margin") or 0)
        if margin <= 0:
            raise ValueError("margin must be positive")
        cost = float(s.get("cost_per_active_day") or 0)
        if cost < 0:
            raise ValueError("cost_per_active_day cannot be negative")
        self.db.upsert_strategy({"id": sid, "name": name, "underlying": underlying, "margin": margin,
                                 "cost_per_active_day": cost, "tradetron_id": str(s.get("tradetron_id") or "")[:20],
                                 "config_strategy_id": str(s.get("config_strategy_id") or "")[:60],
                                 "notes": str(s.get("notes") or "")[:500], "active": 1 if s.get("active", True) else 0})
        self._invalidate()
        return self.db.get_strategy(sid)

    def delete_strategy(self, sid: str) -> None:
        if not self.db.get_strategy(sid):
            raise KeyError(sid)
        n = self.db.one("SELECT COUNT(*) AS n FROM strategy_days WHERE strategy_id = ?", (sid,))["n"]
        if n:
            raise ValueError(f"{n} days of P&L are stored for this strategy. Mark it inactive instead of deleting.")
        self.db.write("DELETE FROM strategies WHERE id = ?", (sid,))
        self._invalidate()

    def strategies_view(self) -> list[dict]:
        summary = {r["id"]: r for r in self.db.strategy_summary()}
        return [{**s, "days": summary.get(s["id"], {}).get("days", 0), "first_day": summary.get(s["id"], {}).get("first_day"),
                 "last_day": summary.get(s["id"], {}).get("last_day")} for s in self.db.list_strategies()]

    # ---- dashboard ------------------------------------------------------------
    def status(self) -> dict:
        imports = self.db.list_imports()
        market = self.db.market_summary()
        strategies = self.strategies_view()
        recs = self.db.list_recommendations(limit=1)
        data = self._data()
        warnings = []
        with_days = [s for s in strategies if s["days"] and s["active"]]
        if not with_days:
            warnings.append("No strategy P&L uploaded yet. The engine and validation need each strategy's daily P&L "
                            "(Data → Upload strategy P&L).")
        elif len(with_days) < len([s for s in strategies if s["active"]]):
            missing = [s["name"] for s in strategies if s["active"] and not s["days"]]
            warnings.append(f"No P&L yet for: {', '.join(missing)}.")
        if not market:
            warnings.append("No market data uploaded yet.")
        for sym in MARKET_SYMBOLS:
            used = any(s["underlying"] == sym for s in strategies if s["active"])
            if used and sym not in data["features"]:
                warnings.append(f"No {sym} market data: strategies on {sym} are judged on the {BENCHMARK} regime instead.")
        cutoff = self.cutoff_day()
        stale = [s["name"] for s in with_days if s["last_day"] and cutoff and s["last_day"] < cutoff]
        if stale:
            warnings.append(f"Strategy P&L ends before the latest complete market session ({cutoff}) for: "
                            + ", ".join(stale) + ".")
        n_bars = sum(m["bars"] for m in market)
        return clean({
            "last_update": imports[0]["uploaded_at"] if imports else None,
            "market": market, "usable_market_records": n_bars,
            "strategy_records": sum(s["days"] for s in strategies), "strategies": strategies,
            "imports": len(imports), "latest_recommendation": recs[0] if recs else None,
            "engine": self.engine_name, "engine_label": ENGINE_LABELS[self.engine_name],
            "analysis_status": "ready" if with_days and data["features"] else "waiting for data",
            "cutoff_day": cutoff, "next_session": next_session(cutoff, self._holidays()) if cutoff else None,
            "warnings": warnings,
        })

    # ---- recommendation ---------------------------------------------------------
    def cutoff_day(self) -> str | None:
        data = self._data()
        f = data["features"].get(BENCHMARK)
        if f is None:
            f = next(iter(data["features"].values()), None)
        days = []
        if f is not None and not f.empty:
            days.append(f.index[-1])
        if not data["panel"].empty:
            days.append(data["panel"]["day"].iloc[-1])
        return max(days) if days else None

    def recommend(self, save: bool = True) -> dict:
        data = self._data()
        cutoff = self.cutoff_day()
        if cutoff is None:
            return {"available": False, "message": "No data stored yet."}
        target = next_session(cutoff, self._holidays())
        engine, cfg = self.engine_name, self.config
        strategies, panel, features = data["strategies"], data["panel"], data["features"]
        ctx = regimes_for(strategies, features, BENCHMARK, target)
        calendar = sorted(panel["day"].unique()) if not panel.empty else []
        if panel.empty:
            res = {"ranking": [{"id": s["id"], "name": s["name"], "status": "insufficient_history", "n_days": 0,
                                "notes": ["no P&L uploaded"]} for s in strategies], "top_strategy": None, "also_qualify": []}
        else:
            res = score_day(panel, strategies, target, ctx, engine, cfg, calendar)
        others = {}
        if not panel.empty:
            for e in ENGINES:
                if e != engine:
                    r = score_day(panel, strategies, target, ctx, e, cfg, calendar)
                    others[e] = {"label": ENGINE_LABELS[e], "top_strategy": r["top_strategy"]}
        rec = self._explain(res, ctx, strategies, cutoff, target, engine, data)
        rec["other_engines"] = others
        rec = clean(rec)
        if save:
            self.db.save_recommendation(rec)
        return rec

    def _explain(self, res: dict, ctx: dict, strategies: list[dict], cutoff: str, target: str, engine: str,
                 data: dict) -> dict:
        reg = {s["id"]: s for s in strategies}
        ranking = res["ranking"]
        top = next((x for x in ranking if x["id"] == res["top_strategy"]), None)
        for x in ranking:
            m = reg[x["id"]]["margin"]
            for k in ("expected", "low", "high", "var5", "cvar5"):
                if x.get(k) is not None:
                    x[f"{k}_rs"] = x[k] * m / LAKH
            x["margin"] = m
            x["underlying"] = reg[x["id"]]["underlying"]
            x["why"] = self._why(x, top, engine)
        markets = {}
        for c in ctx.values():
            sym = c["symbol"]
            if sym and sym not in markets and c["row"] is not None:
                markets[sym] = {"symbol": sym, "data_day": c["data_day"], "regime": c["regime"],
                                "direction": c["direction"], "trend": c["row"].get("trend"),
                                "volatility": c["row"].get("volatility"), "readings": describe(c["row"])}
        warnings = []
        if top is None:
            headline = "No strategy meets the current selection criteria"
            if any(x["status"] == "ok" for x in ranking):
                warnings.append("Every eligible strategy's conservative estimate is at or below zero for this regime.")
            else:
                warnings.append("No strategy has enough evidence to be ranked.")
        else:
            headline = f"{top['name']}"
        if any(c["regime"] == "unknown" for c in ctx.values()):
            warnings.append("The market regime could not be determined for at least one index (not enough market history).")
        thin = [x["name"] for x in ranking if x["status"] == "ok" and x.get("regime_days", 0) < self.config.min_regime_days]
        if thin:
            warnings.append(f"Thin regime evidence (< {self.config.min_regime_days} days) for: {', '.join(thin)}.")
        if not data["panel"].empty and data["panel"]["cost_estimated"].any():
            warnings.append("Costs are estimated from each strategy's Tradetron cost ratio where the P&L file had no "
                            "charges column.")
        lag = [x["name"] for x in ranking if x.get("last_data_day") and x["last_data_day"] < cutoff]
        if lag:
            warnings.append(f"P&L data ends before {cutoff} for: {', '.join(lag)}.")
        return {"target_day": target, "cutoff_day": cutoff, "engine": engine, "engine_label": ENGINE_LABELS[engine],
                "model_version": f"{MODEL_VERSION}/{engine}", "config": self.config.as_dict(),
                "top_strategy": res["top_strategy"], "headline": headline, "also_qualify": res["also_qualify"],
                "ranking": ranking, "markets": list(markets.values()), "warnings": warnings,
                "disclaimer": "Estimates come from past results in similar conditions. They are not guarantees; "
                              "a profitable-day probability is a model estimate."}

    def _why(self, x: dict, top: dict | None, engine: str) -> list[str]:
        if x["status"] != "ok":
            return [n[0].upper() + n[1:] for n in x["notes"]]
        out = []
        if x.get("regime_days") and x.get("regime_mean") is not None:
            out.append(f"In {x['regime_days']} past '{x['regime']}' sessions it averaged {rs(x['regime_mean'])} per ₹1L "
                       f"(profitable {x['regime_win_rate'] * 100:.0f}% of days); overall {rs(x.get('overall_mean'))} per ₹1L "
                       f"over {x['n_days']} days.")
        else:
            out.append(f"Overall it averaged {rs(x.get('overall_mean'))} per ₹1L over {x['n_days']} days.")
        if engine == "B_regime_shrunk":
            out.append(f"Estimate {rs(x['expected'])} per ₹1L (80% range {rs(x['low'])} to {rs(x['high'])}); "
                       f"tail-risk penalty {rs(x['penalty'])}; score {rs(x['score'])}.")
        if top and x["id"] != top["id"]:
            gap = top["score"] - x["score"]
            reason = "lower conservative estimate"
            if engine == "B_regime_shrunk":
                if x.get("penalty", 0) > top.get("penalty", 0) and x["low"] >= top["low"] - 1e-9:
                    reason = "larger losses on its worst days"
                elif x.get("regime_days", 0) < self.config.min_regime_days:
                    reason = "too few days in this regime to trust its average"
            out.append(f"Ranks below {top['name']} by {rs(gap)} per ₹1L of score: {reason}.")
        elif x.get("score", 0) <= 0:
            out.append("Its score is not above zero, so it does not qualify.")
        out += [n[0].upper() + n[1:] for n in x["notes"]]
        return out

    def latest_recommendation(self) -> dict | None:
        rows = self.db.list_recommendations(limit=1)
        return rows[0] if rows else None

    def evaluate_actuals(self) -> int:
        """Fill in what actually happened for stored recommendations whose day now has P&L."""
        data = self._data()
        panel = data["panel"]
        if panel.empty:
            return 0
        n = 0
        for r in self.db.list_recommendations(limit=1000):
            day = r["target_day"]
            got = panel[panel["day"] == day]
            if got.empty:
                continue
            rets = dict(zip(got["strategy"], got["ret"]))
            nets = dict(zip(got["strategy"], got["net"]))
            pick = r["top_strategy"]
            order = sorted(rets, key=rets.get, reverse=True)
            actual = {"strategies": {k: {"ret": rets[k], "net": nets[k]} for k in rets},
                      "best": order[0] if order else None, "pick": pick,
                      "pick_ret": rets.get(pick) if pick else 0.0, "pick_net": nets.get(pick) if pick else 0.0,
                      "pick_rank": order.index(pick) + 1 if pick in order else None,
                      "strategies_reported": len(rets), "evaluated_at": now_iso()}
            previous = {k: v for k, v in (r["actual"] or {}).items() if k != "evaluated_at"}
            if r["actual"] is None or previous != {k: v for k, v in clean(actual).items() if k != "evaluated_at"}:
                self.db.set_actual(day, r["engine"], clean(actual))
                n += 1
        return n

    def history(self) -> dict:
        rows = self.db.list_recommendations(limit=500)
        evaluated = [r for r in rows if r["actual"]]
        picked = [r for r in evaluated if r["top_strategy"]]
        summary = {
            "recommendations": len(rows), "evaluated": len(evaluated),
            "picked_and_evaluated": len(picked),
            "profitable_picks": sum(1 for r in picked if (r["actual"]["pick_ret"] or 0) > 0),
            "best_pick_hits": sum(1 for r in picked if r["actual"]["best"] == r["top_strategy"]),
            "total_per_lakh": sum((r["actual"]["pick_ret"] or 0) for r in picked),
        }
        return clean({"summary": summary, "rows": [{
            "target_day": r["target_day"], "cutoff_day": r["cutoff_day"], "engine": r["engine"],
            "model_version": r["model_version"], "created_at": r["created_at"], "top_strategy": r["top_strategy"],
            "headline": r["payload"].get("headline"),
            "ranking": [{"id": x["id"], "name": x["name"], "rank": x.get("rank"), "status": x["status"],
                         "score": x.get("score"), "p_win": x.get("p_win")} for x in r["payload"].get("ranking", [])],
            "regimes": {m["symbol"]: m["regime"] for m in r["payload"].get("markets", [])},
            "actual": r["actual"]} for r in rows]})

    def refresh_after_data_change(self) -> dict:
        try:
            rec = self.recommend(save=True)
            n = self.evaluate_actuals()
            return {"recommendation_for": rec.get("target_day"), "top_strategy": rec.get("top_strategy"),
                    "actuals_filled": n}
        except Exception as exc:  # an import must not fail because the follow-up analysis did
            log.exception("research refresh failed")
            return {"error": str(exc)}

    # ---- analysis views -------------------------------------------------------------
    def performance(self, start: str | None = None, end: str | None = None) -> dict:
        data = self._data()
        if data["panel"].empty:
            return {"strategies": [], "message": "No strategy P&L uploaded yet."}
        return clean(analytics.strategy_report(data["panel"], data["strategies"], start, end))

    def relationships(self) -> dict:
        data = self._data()
        if data["panel"].empty:
            return {"tests": [], "count": 0, "message": "No strategy P&L uploaded yet."}
        with self._lock:
            if "rel" not in self._cache:
                self._cache["rel"] = clean(analytics.relationships(data["panel"], data["strategies"], data["features"]))
            return self._cache["rel"]

    def backtest(self) -> dict:
        data = self._data()
        if data["panel"].empty:
            return {"days": 0, "message": "No strategy P&L uploaded yet."}
        with self._lock:
            if "bt" not in self._cache:
                self._cache["bt"] = clean(walk_forward(data["panel"], data["strategies"], data["features"], BENCHMARK,
                                                       self.config))
            return self._cache["bt"]

    def market_view(self) -> dict:
        data = self._data()
        out = []
        for sym, f in data["features"].items():
            tail = f.tail(30)
            out.append({"symbol": sym, "days": int(len(f)), "first_day": f.index[0], "last_day": f.index[-1],
                        "regime_counts": f["regime"].value_counts().to_dict(),
                        "recent": [{"day": d, "close": r["close"], "regime": r["regime"], "direction": r["direction"],
                                    "ret_1d": r["ret_1d"], "atr14_pct": r["atr14_pct"], "eff10": r["eff10"]}
                                   for d, r in tail.iterrows()]})
        return clean({"markets": out})

    # ---- backup ----------------------------------------------------------------
    def backup(self) -> bytes:
        return self.db.backup_zip()

    def restore(self, raw: bytes) -> dict:
        out = self.db.restore_zip(raw)
        self._invalidate()
        return out
