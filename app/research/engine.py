"""Strategy ranking engines. Every engine sees only data up to a cutoff day and scores the strategies
for the next session.

Units: a strategy's daily result is its net P&L per Rs 1,00,000 of margin ("per lakh"), so strategies
deployed on very different capital are comparable.

Engines
  A_best_so_far   highest average daily return over all history so far           (baseline)
  A_trailing60    highest average over the last 60 recorded days                  (baseline)
  B_regime_raw    highest raw average in today's regime (>= 5 days), no shrinkage (baseline)
  B_regime_shrunk regime average shrunk toward the strategy's overall average, ranked on the
                  conservative end of a bootstrap range minus a tail-risk penalty   (main engine)
  C_ridge         per-strategy ridge regression on the previous session's features  (challenger)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

LAKH = 100_000.0
MODEL_VERSION = "research-v1"
ENGINES = ("B_regime_shrunk", "A_best_so_far", "A_trailing60", "B_regime_raw", "C_ridge")
ENGINE_LABELS = {
    "B_regime_shrunk": "Regime-aware, shrunk (main)",
    "A_best_so_far": "Best average so far",
    "A_trailing60": "Best last-60-days average",
    "B_regime_raw": "Best raw average in this regime",
    "C_ridge": "Ridge regression on features",
}
RIDGE_FEATURES = ("ret_1d", "ret_5d", "range_pct", "atr14_pct", "eff10", "dist_ema20_pct", "rv10")


@dataclass
class EngineConfig:
    shrink_k: float = 20.0          # pseudo-days pulling a regime average toward the overall average
    min_history: int = 40           # recorded days before a strategy can be ranked
    min_regime_days: int = 15       # below this the regime evidence is flagged as thin
    lower_quantile: float = 0.10    # conservative end of the bootstrap range (80% interval)
    tail_penalty: float = 0.02      # score -= tail_penalty * |CVaR 5%|
    bootstrap: int = 400
    weekday_window: int = 8         # recent same-weekday sessions used to judge "does it trade on this weekday"
    weekday_min_active: float = 0.15
    ridge_alpha: float = 10.0
    seed: int = 7

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Panel: one row per strategy-day, with the regime known before that day
# ---------------------------------------------------------------------------
def build_panel(strategies: list[dict], days: list[dict]) -> pd.DataFrame:
    """days: rows of strategy_days. Adds net P&L and return per lakh."""
    if not days:
        return pd.DataFrame(columns=["strategy", "day", "gross", "net", "ret", "cost_estimated", "active"])
    reg = {s["id"]: s for s in strategies}
    df = pd.DataFrame(days).rename(columns={"strategy_id": "strategy"})
    df = df[df["strategy"].isin(reg)].copy()
    for c in ("gross_pnl", "charges", "trades"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    margin = df["strategy"].map(lambda s: reg[s]["margin"])
    est_cost = df["strategy"].map(lambda s: reg[s]["cost_per_active_day"] or 0.0)
    active = df["gross_pnl"].abs() > 0.005
    charges = df["charges"].where(df["charges"].notna(), np.where(active, est_cost, 0.0))
    df["gross"] = df["gross_pnl"]
    df["net"] = df["gross_pnl"] - charges
    df["cost_estimated"] = df["charges"].isna() & active
    df["active"] = active
    df["ret"] = df["net"] / margin * LAKH
    df["gross_ret"] = df["gross"] / margin * LAKH
    return df[["strategy", "day", "gross", "net", "ret", "gross_ret", "cost_estimated", "active", "trades"]] \
        .sort_values(["day", "strategy"]).reset_index(drop=True)


def prior_row(features: pd.DataFrame | None, day: str) -> pd.Series | None:
    """The last feature row strictly before `day` (what was known at the previous close)."""
    if features is None or features.empty:
        return None
    idx = features.index.searchsorted(day, side="left") - 1
    return features.iloc[idx] if idx >= 0 else None


def attach_regimes(panel: pd.DataFrame, strategies: list[dict], features: dict[str, pd.DataFrame],
                   benchmark: str) -> pd.DataFrame:
    reg = {s["id"]: s for s in strategies}
    regimes, directions, symbols, fx = [], [], [], []
    for sid, day in zip(panel["strategy"], panel["day"]):
        sym = market_symbol(reg[sid]["underlying"], features, benchmark)
        row = prior_row(features.get(sym), day)
        regimes.append(row["regime"] if row is not None else "unknown")
        directions.append(row["direction"] if row is not None else "unknown")
        symbols.append(sym)
        fx.append([float(row[c]) if row is not None and c in row.index else np.nan for c in RIDGE_FEATURES])
    panel = panel.copy()
    panel["regime"], panel["direction"], panel["regime_symbol"] = regimes, directions, symbols
    fx = np.array(fx, dtype=float).reshape(len(panel), len(RIDGE_FEATURES))
    for i, c in enumerate(RIDGE_FEATURES):
        panel[f"f_{c}"] = fx[:, i]
    return panel


def market_symbol(underlying: str, features: dict[str, pd.DataFrame], benchmark: str) -> str:
    f = features.get(underlying)
    return underlying if f is not None and not f.empty else benchmark


# ---------------------------------------------------------------------------
# Scoring one day
# ---------------------------------------------------------------------------
def _weekday_activity(hist: pd.DataFrame, calendar: list[str], target: str, window: int) -> tuple[int, int]:
    """(sessions it traded, sessions observed) over the last `window` same-weekday sessions in its active span."""
    if hist.empty:
        return 0, 0
    wd = pd.Timestamp(target).weekday()
    first = hist["day"].iloc[0]
    same = [d for d in calendar if first <= d < target and pd.Timestamp(d).weekday() == wd][-window:]
    active_days = set(hist.loc[hist["active"], "day"])
    return sum(d in active_days for d in same), len(same)


def _drawdowns(rets: np.ndarray) -> tuple[float, float]:
    eq = np.cumsum(rets)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    dd = eq - peak
    return float(dd[-1]) if len(dd) else 0.0, float(dd.min()) if len(dd) else 0.0


def _tail(rets: np.ndarray) -> tuple[float, float]:
    if len(rets) < 5:
        return float("nan"), float("nan")
    var = float(np.quantile(rets, 0.05))
    return var, float(rets[rets <= var].mean())


def score_day(panel: pd.DataFrame, strategies: list[dict], target: str, regimes: dict[str, dict],
              engine: str, cfg: EngineConfig, calendar: list[str]) -> dict:
    """Rank strategies for `target` using only panel rows dated before it.

    regimes: strategy id -> {"regime", "direction", "symbol", "row"} known at the previous close.
    """
    rng = np.random.default_rng(cfg.seed)
    hist_all = panel[panel["day"] < target]
    out = []
    for s in strategies:
        sid = s["id"]
        hist = hist_all[hist_all["strategy"] == sid]
        rets = hist["ret"].to_numpy()
        n = len(rets)
        ctx = regimes.get(sid) or {"regime": "unknown", "direction": "unknown", "symbol": None}
        item = {"id": sid, "name": s["name"], "n_days": n, "regime": ctx["regime"], "regime_symbol": ctx["symbol"],
                "status": "ok", "notes": []}
        if n:
            item["last_data_day"] = hist["day"].iloc[-1]
        traded, observed = _weekday_activity(hist, calendar, target, cfg.weekday_window)
        item["weekday_activity"] = [traded, observed]
        if n < cfg.min_history:
            item["status"] = "insufficient_history"
            item["notes"].append(f"only {n} recorded days before {target} (needs {cfg.min_history})" if n else
                                 "no P&L recorded yet")
        elif observed >= 4 and traded / observed < cfg.weekday_min_active:
            item["status"] = "not_trading"
            item["notes"].append(f"traded on {traded} of the last {observed} {pd.Timestamp(target):%A}s; "
                                 "it is not expected to trade this session")
        if n:
            mu_all = float(rets.mean())
            item["overall_mean"] = mu_all
            item["overall_win_rate"] = float((rets > 0).mean())
            cur_dd, max_dd = _drawdowns(rets)
            item["current_drawdown"], item["max_drawdown"] = cur_dd, max_dd
        if item["status"] != "ok":
            out.append(item)
            continue

        in_regime = rets[(hist["regime"] == ctx["regime"]).to_numpy()] if ctx["regime"] != "unknown" else rets[:0]
        n_r = len(in_regime)
        item["regime_days"] = n_r
        item["regime_mean"] = float(in_regime.mean()) if n_r else None
        item["regime_win_rate"] = float((in_regime > 0).mean()) if n_r else None
        tail_src = in_regime if n_r >= 20 else rets
        item["var5"], item["cvar5"] = _tail(tail_src)
        if ctx["regime"] == "unknown":
            item["notes"].append("market regime unknown for this session; overall history used")
        elif n_r < cfg.min_regime_days:
            item["notes"].append(f"only {n_r} past days in the '{ctx['regime']}' regime: thin evidence")
        if cur_dd < 0 and max_dd < 0 and cur_dd <= max_dd * 0.999:
            item["notes"].append("currently at its deepest drawdown so far")

        if engine == "B_regime_shrunk":
            k = cfg.shrink_k
            est = (in_regime.sum() + k * mu_all) / (n_r + k)
            boot_all = rng.choice(rets, (cfg.bootstrap, n)).mean(axis=1)
            boot_r = rng.choice(in_regime, (cfg.bootstrap, n_r)).sum(axis=1) if n_r else np.zeros(cfg.bootstrap)
            boot = (boot_r + k * boot_all) / (n_r + k)
            lo, hi = np.quantile(boot, [cfg.lower_quantile, 1 - cfg.lower_quantile])
            wins_r = float((in_regime > 0).sum())
            p_win = (wins_r + k * item["overall_win_rate"]) / (n_r + k)
            penalty = cfg.tail_penalty * abs(item["cvar5"]) if not np.isnan(item["cvar5"]) else 0.0
            item.update(expected=float(est), low=float(lo), high=float(hi), p_win=float(p_win),
                        penalty=float(penalty), score=float(lo - penalty))
        elif engine == "A_best_so_far":
            item.update(expected=mu_all, score=mu_all, p_win=item["overall_win_rate"])
        elif engine == "A_trailing60":
            recent = rets[-60:]
            item.update(expected=float(recent.mean()), score=float(recent.mean()), p_win=float((recent > 0).mean()))
        elif engine == "B_regime_raw":
            if n_r >= 5:
                item.update(expected=item["regime_mean"], score=item["regime_mean"], p_win=item["regime_win_rate"])
            else:
                item["status"] = "insufficient_regime"
                item["notes"].append(f"fewer than 5 past days in '{ctx['regime']}'")
        elif engine == "C_ridge":
            pred = _ridge_predict(hist, ctx, cfg)
            if pred is None:
                item["status"] = "insufficient_features"
                item["notes"].append("not enough days with market features to fit the regression")
            else:
                item.update(expected=pred, score=pred)
        else:
            raise ValueError(f"unknown engine {engine}")
        out.append(item)

    ranked = sorted([x for x in out if x["status"] == "ok"], key=lambda x: -x["score"])
    for i, x in enumerate(ranked, 1):
        x["rank"] = i
    rest = [x for x in out if x["status"] != "ok"]
    qualifying = [x for x in ranked if x["score"] > 0]
    top = qualifying[0] if qualifying else None
    return {"target_day": target, "engine": engine, "ranking": ranked + rest,
            "top_strategy": top["id"] if top else None,
            "also_qualify": [x["id"] for x in qualifying[1:]]}


def _ridge_predict(hist: pd.DataFrame, ctx: dict, cfg: EngineConfig) -> float | None:
    """Fit on this strategy's history (features of the session before each day) and predict the next day."""
    if ctx.get("row") is None:
        return None
    cols = [c for c in RIDGE_FEATURES if c in ctx["row"].index]
    if not cols:
        return None
    X = hist[[f"f_{c}" for c in cols]].to_numpy(dtype=float)
    keep = ~np.isnan(X).any(axis=1)
    X, y = X[keep], hist["ret"].to_numpy(dtype=float)[keep]
    if len(y) < max(60, 6 * len(cols)):
        return None
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1
    Z = (X - mu) / sd
    ym = y.mean()
    beta = np.linalg.solve(Z.T @ Z + cfg.ridge_alpha * np.eye(len(cols)), Z.T @ (y - ym))
    x_now = ctx["row"][cols].to_numpy(dtype=float)
    if np.isnan(x_now).any():
        return None
    return float(ym + ((x_now - mu) / sd) @ beta)
