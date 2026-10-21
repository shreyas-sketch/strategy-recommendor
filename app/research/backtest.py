"""Walk-forward validation: for every historical session, rank the strategies using only what was known at
the previous close, then compare the pick with what actually happened that day."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .engine import ENGINES, EngineConfig, prior_row, score_day


def regimes_for(strategies: list[dict], features: dict[str, pd.DataFrame], benchmark: str, target: str) -> dict:
    from .engine import market_symbol
    out = {}
    for s in strategies:
        sym = market_symbol(s["underlying"], features, benchmark)
        row = prior_row(features.get(sym), target)
        out[s["id"]] = {"symbol": sym if row is not None else None, "row": row,
                        "regime": row["regime"] if row is not None else "unknown",
                        "direction": row["direction"] if row is not None else "unknown",
                        "data_day": row.name if row is not None else None}
    return out


def walk_forward(panel: pd.DataFrame, strategies: list[dict], features: dict[str, pd.DataFrame], benchmark: str,
                 cfg: EngineConfig, engines=ENGINES, permutations: int = 2000) -> dict:
    calendar = sorted(panel["day"].unique())
    if not calendar:
        return {"days": 0, "message": "No strategy P&L stored yet."}
    counts = panel.groupby("strategy")["day"].apply(list).to_dict()
    start = None
    for d in calendar:
        if any(sum(x < d for x in days) >= cfg.min_history for days in counts.values()):
            start = d
            break
    if start is None:
        return {"days": 0, "message": f"No strategy has {cfg.min_history} recorded days yet, so nothing can be "
                                      "validated. Upload more history."}
    actual = panel.pivot_table(index="day", columns="strategy", values="ret", aggfunc="sum")
    log = []
    picks = {e: [] for e in engines}
    eligible_rets = []
    for t in [d for d in calendar if d >= start]:
        ctx = regimes_for(strategies, features, benchmark, t)
        row_actual = actual.loc[t] if t in actual.index else pd.Series(dtype=float)
        day = {"day": t, "regime": _bench_regime(ctx, benchmark), "picks": {}}
        elig = None
        for e in engines:
            res = score_day(panel, strategies, t, ctx, e, cfg, calendar)
            pick = res["top_strategy"]
            if e == "B_regime_shrunk":
                elig = [x["id"] for x in res["ranking"] if x["status"] == "ok"]
            realised = float(row_actual.get(pick, 0.0)) if pick and not pd.isna(row_actual.get(pick, np.nan)) else 0.0
            picks[e].append(realised)
            day["picks"][e] = {"pick": pick, "ret": realised}
        elig = elig or []
        er = [float(row_actual[s]) if s in row_actual.index and not pd.isna(row_actual[s]) else 0.0 for s in elig]
        eligible_rets.append(er)
        traded = {s: float(v) for s, v in row_actual.items() if not pd.isna(v) and s in elig}
        day["best"] = max(traded, key=traded.get) if traded else None
        day["top2"] = sorted(traded, key=traded.get, reverse=True)[:2]
        day["eligible"] = elig
        day["equal_weight"] = float(np.mean(er)) if er else 0.0
        log.append(day)

    n = len(log)
    summary = {e: _metrics([d["picks"][e]["ret"] for d in log], [d["picks"][e]["pick"] for d in log], log)
               for e in engines}
    summary["equal_weight"] = _metrics([d["equal_weight"] for d in log], [("all" if d["eligible"] else None) for d in log], log)
    rng = np.random.default_rng(cfg.seed)
    rand_totals = np.zeros(permutations)
    for er in eligible_rets:
        if er:
            rand_totals += np.asarray(er)[rng.integers(0, len(er), permutations)]
    summary["random"] = {"label": "Random eligible strategy (average of simulations)",
                         "total": float(rand_totals.mean()), "days": n,
                         "p05": float(np.quantile(rand_totals, 0.05)), "p95": float(np.quantile(rand_totals, 0.95))}
    for e in engines:
        summary[e]["p_vs_random"] = float((rand_totals >= summary[e]["total"]).mean())
    curves = {e: np.cumsum([d["picks"][e]["ret"] for d in log]).round(2).tolist() for e in engines}
    curves["equal_weight"] = np.cumsum([d["equal_weight"] for d in log]).round(2).tolist()
    return {"days": n, "first_day": log[0]["day"], "last_day": log[-1]["day"], "summary": summary,
            "curves": {"days": [d["day"] for d in log], "series": curves}, "log": log[-120:],
            "config": cfg.as_dict(),
            "note": "Each day is scored with data up to the previous session only. Returns are net P&L per Rs 1 lakh "
                    "of the picked strategy's margin; a day with no pick counts as 0."}


def _bench_regime(ctx: dict, benchmark: str) -> str:
    for c in ctx.values():
        if c["symbol"] == benchmark:
            return c["regime"]
    return next((c["regime"] for c in ctx.values()), "unknown")


def _metrics(rets: list[float], picks: list[str | None], log: list[dict]) -> dict:
    r = np.asarray(rets, dtype=float)
    picked = [i for i, p in enumerate(picks) if p]
    pr = r[picked] if picked else np.array([])
    hits1 = [log[i]["best"] == picks[i] for i in picked if log[i]["best"]]
    hits2 = [picks[i] in log[i]["top2"] for i in picked if log[i]["top2"]]
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    quarters = {}
    regimes = {}
    for i, d in enumerate(log):
        q = f"{d['day'][:4]}-Q{(int(d['day'][5:7]) - 1) // 3 + 1}"
        quarters.setdefault(q, []).append(r[i])
        regimes.setdefault(d["regime"], []).append(r[i])
    return {
        "days": int(len(r)), "picked_days": len(picked), "abstain_rate": 1 - len(picked) / max(1, len(r)),
        "total": float(r.sum()), "mean": float(r.mean()) if len(r) else 0.0,
        "profitable_pick_rate": float((pr > 0).mean()) if len(pr) else None,
        "top1_hit_rate": float(np.mean(hits1)) if hits1 else None,
        "top2_hit_rate": float(np.mean(hits2)) if hits2 else None,
        "max_drawdown": float((eq - peak).min()) if len(eq) else 0.0,
        "sharpe": float(r.mean() / sd * math.sqrt(252)) if sd > 0 else None,
        "by_quarter": {k: {"total": float(np.sum(v)), "days": len(v)} for k, v in sorted(quarters.items())},
        "by_regime": {k: {"total": float(np.sum(v)), "days": len(v)} for k, v in sorted(regimes.items())},
    }
