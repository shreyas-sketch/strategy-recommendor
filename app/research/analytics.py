"""Descriptive statistics: per-strategy performance and market-condition relationships.

These describe the past; they are not forecasts. The relationship tests report sample sizes and
p-values adjusted for the number of comparisons, so a pattern found by chance among many features
is not presented as evidence.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .features import FEATURE_LABELS


def _streaks(x: np.ndarray) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for v in x:
        if v > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif v < 0:
            cur_l, cur_w = cur_l + 1, 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return best_w, best_l


def _max_dd(pnl: np.ndarray) -> float:
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    return float((eq - peak).min()) if len(eq) else 0.0


def perf_stats(g: pd.DataFrame, margin: float) -> dict:
    net, gross = g["net"].to_numpy(), g["gross"].to_numpy()
    wins, losses = net[net > 0], net[net < 0]
    sd = net.std(ddof=1) if len(net) > 1 else float("nan")
    bw, bl = _streaks(net)
    dd = _max_dd(net)
    return {
        "days": int(len(net)), "active_days": int(g["active"].sum()),
        "win_days": int(len(wins)), "loss_days": int(len(losses)), "flat_days": int((net == 0).sum()),
        "win_rate": float(len(wins) / max(1, len(wins) + len(losses))),
        "gross_pnl": float(gross.sum()), "costs": float((gross - net).sum()), "net_pnl": float(net.sum()),
        "cost_estimated_days": int(g["cost_estimated"].sum()),
        "avg_day": float(net.mean()) if len(net) else 0.0,
        "avg_win_day": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_day": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
        "max_drawdown": dd, "max_drawdown_pct_margin": dd / margin * 100,
        "net_return_pct_margin": float(net.sum() / margin * 100),
        "sharpe": float(net.mean() / sd * math.sqrt(252)) if sd and sd > 0 else None,
        "best_day": float(net.max()) if len(net) else 0.0, "worst_day": float(net.min()) if len(net) else 0.0,
        "longest_win_streak": bw, "longest_loss_streak": bl,
        "avg_trades_per_active_day": float(g.loc[g["active"], "trades"].mean()) if g["trades"].notna().any() else None,
    }


def strategy_report(panel: pd.DataFrame, strategies: list[dict], start: str | None = None, end: str | None = None) -> dict:
    p = panel
    if start:
        p = p[p["day"] >= start]
    if end:
        p = p[p["day"] <= end]
    out = []
    for s in strategies:
        g = p[p["strategy"] == s["id"]]
        item = {"id": s["id"], "name": s["name"], "margin": s["margin"], "underlying": s["underlying"]}
        if g.empty:
            item["stats"] = None
            out.append(item)
            continue
        item["first_day"], item["last_day"] = g["day"].iloc[0], g["day"].iloc[-1]
        item["stats"] = perf_stats(g, s["margin"])
        item["equity"] = [[d, round(v, 2)] for d, v in zip(g["day"], np.cumsum(g["ret"].to_numpy()))]
        month = g["day"].str[:7]
        item["by_month"] = [{"month": m, "net": float(x["net"].sum()), "days": int(len(x)),
                             "pct_margin": float(x["net"].sum() / s["margin"] * 100)} for m, x in g.groupby(month)]
        wd = pd.to_datetime(g["day"]).dt.day_name()
        item["by_weekday"] = [{"weekday": w, "net": float(x["net"].sum()), "days": int(len(x)),
                               "win_rate": float((x["net"] > 0).mean())} for w, x in g.groupby(wd)]
        week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        item["by_weekday"].sort(key=lambda r: week.index(r["weekday"]))
        if "regime" in g:
            item["by_regime"] = _group_table(g, "regime")
            item["by_direction"] = _group_table(g, "direction")
        out.append(item)
    return {"strategies": out}


def _group_table(g: pd.DataFrame, col: str) -> list[dict]:
    rows = []
    for k, x in g.groupby(col):
        r = x["ret"].to_numpy()
        se = r.std(ddof=1) / math.sqrt(len(r)) if len(r) > 1 else float("nan")
        rows.append({col: k, "days": int(len(r)), "mean_per_lakh": float(r.mean()), "se": None if np.isnan(se) else float(se),
                     "win_rate": float((r > 0).mean()), "net": float(x["net"].sum())})
    return sorted(rows, key=lambda x: -x["days"])


def _welch_p(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sided Welch t-test p-value (normal approximation to the t distribution for df > 30)."""
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    if va + vb <= 0:
        return 1.0
    t = (a.mean() - b.mean()) / math.sqrt(va + vb)
    df = (va + vb) ** 2 / ((va ** 2) / (len(a) - 1) + (vb ** 2) / (len(b) - 1))
    if df > 30:
        return math.erfc(abs(t) / math.sqrt(2))
    # Student-t tail via the regularised incomplete beta (continued fraction), adequate for small df
    x = df / (df + t * t)
    return _betainc(df / 2, 0.5, x)


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b + lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(200):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1 + num * d
        d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + num / (c if abs(c) > 1e-30 else 1e-30)
        f *= c * d
        if abs(1 - c * d) < 1e-10:
            break
    return min(1.0, front * (f - 1))


def relationships(panel: pd.DataFrame, strategies: list[dict], features: dict[str, pd.DataFrame],
                  min_bucket: int = 15) -> dict:
    """For each strategy and each prior-session feature: next-day result in the top vs bottom third.

    Buckets use terciles of the whole sample, so this is a description of history, not a tradable rule.
    p-values are Benjamini-Hochberg adjusted across every test run here.
    """
    from .engine import prior_row
    tests = []
    for s in strategies:
        g = panel[panel["strategy"] == s["id"]]
        if g.empty:
            continue
        sym = g["regime_symbol"].iloc[0] if "regime_symbol" in g else None
        f = features.get(sym)
        if f is None or f.empty:
            continue
        rows = [prior_row(f, d) for d in g["day"]]
        for key in FEATURE_LABELS:
            if key not in f.columns:
                continue
            x = np.array([r[key] if r is not None else np.nan for r in rows], dtype=float)
            y = g["ret"].to_numpy()
            ok = ~np.isnan(x)
            if ok.sum() < 3 * min_bucket:
                continue
            xs, ys = x[ok], y[ok]
            lo_cut, hi_cut = np.quantile(xs, [1 / 3, 2 / 3])
            low, high = ys[xs <= lo_cut], ys[xs >= hi_cut]
            if len(low) < min_bucket or len(high) < min_bucket or lo_cut == hi_cut:
                continue
            tests.append({"strategy": s["id"], "strategy_name": s["name"], "feature": key,
                          "feature_label": FEATURE_LABELS[key][0], "symbol": sym, "n": int(ok.sum()),
                          "low_cut": float(lo_cut), "high_cut": float(hi_cut),
                          "low_mean": float(low.mean()), "high_mean": float(high.mean()),
                          "low_n": int(len(low)), "high_n": int(len(high)),
                          "diff": float(high.mean() - low.mean()), "p": _welch_p(high, low)})
    # Benjamini-Hochberg
    m = len(tests)
    order = sorted(range(m), key=lambda i: tests[i]["p"])
    q_prev = 1.0
    for rank, i in reversed(list(enumerate(order, 1))):
        q_prev = min(q_prev, tests[i]["p"] * m / rank)
        tests[i]["q"] = q_prev
    for t in tests:
        t["evidence"] = "supported" if t["q"] < 0.10 else "suggestive" if t["p"] < 0.05 else "none"
    tests.sort(key=lambda t: t["p"])
    return {"tests": tests, "count": m,
            "note": "Top vs bottom third of each feature (measured at the previous close) against the next day's net "
                    "result per Rs 1 lakh. 'supported' = significant after adjusting for all tests (BH q < 0.10); "
                    "'suggestive' = p < 0.05 before adjustment only."}

