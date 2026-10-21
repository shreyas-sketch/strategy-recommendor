"""Parse uploaded files into rows for storage, reporting every assumption and data-quality issue.

Two kinds of file:
* market data: a TradingView chart export (1-minute or daily) with optional indicator columns;
* strategy P&L: a daily P&L table or a trade/order log for one or more strategies (CSV or Excel).
Column names are matched loosely because exports differ; the detected mapping is returned so the
admin can check it.
"""

from __future__ import annotations

import io
import re
from datetime import time

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
SESSION_OPEN = time(9, 15)
SESSION_LAST_BAR = time(15, 29)
FULL_SESSION_BARS = 375


class IngestError(ValueError):
    pass


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9&]+", " ", str(name).lower()).strip()


def read_table(raw: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    try:
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            return pd.read_excel(io.BytesIO(raw))
        text = raw.decode("utf-8-sig", errors="replace")
        return pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except Exception as exc:  # pandas raises many types for malformed input
        raise IngestError(f"Could not read {filename}: {exc}") from exc


def _find(cols: dict[str, str], *candidates: str, contains: tuple[str, ...] = (), exclude: tuple[str, ...] = ()):
    for c in candidates:
        if c in cols:
            return cols[c]
    for n, orig in cols.items():
        if any(k in n for k in contains) and not any(x in n for x in exclude):
            return orig
    return None


def parse_timestamps(s: pd.Series) -> pd.Series:
    """TradingView exports ISO-8601 with offset, or UNIX seconds. Naive times are taken as IST."""
    if pd.api.types.is_numeric_dtype(s):
        unit = "ms" if s.dropna().iloc[0] > 10**11 else "s"
        return pd.to_datetime(s, unit=unit, utc=True).dt.tz_convert(IST)
    ts = pd.to_datetime(s.astype(str).str.strip(), utc=False, errors="coerce", format="mixed")
    if ts.dt.tz is None:
        return ts.dt.tz_localize(IST)
    return ts.dt.tz_convert(IST)


def parse_dates(s: pd.Series) -> tuple[pd.Series, str | None]:
    """Dates as YYYY-MM-DD strings. Resolves DD/MM vs MM/DD from the data itself; ambiguous -> day first (India)."""
    if pd.api.types.is_datetime64_any_dtype(s):
        d = s.dt.tz_convert(IST) if s.dt.tz is not None else s
        return d.dt.strftime("%Y-%m-%d"), None
    text = s.astype(str).str.strip()
    if text.str.match(r"^\d{4}-\d{2}-\d{2}").all():
        ts = pd.to_datetime(text, errors="coerce", format="mixed", utc=False)
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_convert(IST)
        return ts.dt.strftime("%Y-%m-%d"), None
    parts = text.str.extract(r"^(\d{1,2})[/\-. ](\d{1,2})[/\-. ](\d{2,4})")
    note = None
    if parts.notna().all().all():
        a, b = parts[0].astype(int), parts[1].astype(int)
        if (a > 12).any() and not (b > 12).any():
            dayfirst = True
        elif (b > 12).any() and not (a > 12).any():
            dayfirst = False
        else:
            dayfirst = True
            note = "Dates were ambiguous (day and month both ≤ 12 everywhere); read as day/month/year."
    else:
        dayfirst = True
    ts = pd.to_datetime(text, errors="coerce", dayfirst=dayfirst, format="mixed")
    return ts.dt.strftime("%Y-%m-%d"), note


def to_number(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    t = s.astype(str).str.strip().str.replace(r"[₹,\s]|Rs\.?|INR", "", regex=True)
    neg = t.str.match(r"^\(.*\)$")
    t = t.str.replace(r"[()]", "", regex=True)
    out = pd.to_numeric(t, errors="coerce")
    return out.where(~neg, -out)


# ---------------------------------------------------------------------------
# Market data (TradingView export)
# ---------------------------------------------------------------------------
def parse_market(raw: bytes, filename: str) -> dict:
    df = read_table(raw, filename)
    if df.empty:
        raise IngestError("The file has no rows")
    cols = {norm(c): c for c in df.columns}
    tcol = _find(cols, "time", "datetime", "date", "timestamp", contains=("time", "date"))
    ohlc = {k: _find(cols, k) for k in ("open", "high", "low", "close")}
    missing = [k for k, v in ohlc.items() if v is None]
    if tcol is None or missing:
        raise IngestError(f"Missing required column(s): {', '.join((['time'] if tcol is None else []) + missing)}. "
                          f"Found: {', '.join(map(str, df.columns))}")
    mapping = {"time": tcol, **ohlc}
    optional = {
        "volume": _find(cols, "volume", "vol"),
        "vwap": _find(cols, "vwap", contains=("vwap",)),
        "ema": _find(cols, "ema", contains=("ema",), exclude=("rsi",)),
        "rsi": _find(cols, "rsi"),
        "rsi_ma": _find(cols, "rsi based ma", contains=("rsi based", "rsi ma")),
        "st_up": _find(cols, "up trend", "uptrend", contains=("up trend",)),
        "st_down": _find(cols, "down trend", "downtrend", contains=("down trend",)),
        "st_line": _find(cols, "supertrend", contains=("supertrend",)),
    }
    mapping.update({k: v for k, v in optional.items() if v is not None})
    recognised = set(mapping.values())
    ignored = [c for c in df.columns if c not in recognised]

    warnings: list[str] = []
    ts = parse_timestamps(df[tcol])
    bad_ts = int(ts.isna().sum())
    out = pd.DataFrame({"ts": ts})
    for k in ("open", "high", "low", "close"):
        out[k] = to_number(df[ohlc[k]])
    for k in ("volume", "vwap", "ema", "rsi", "rsi_ma"):
        out[k] = to_number(df[mapping[k]]) if k in mapping else np.nan
    if "st_up" in mapping or "st_down" in mapping:
        up = to_number(df[mapping["st_up"]]) if "st_up" in mapping else pd.Series(np.nan, index=df.index)
        down = to_number(df[mapping["st_down"]]) if "st_down" in mapping else pd.Series(np.nan, index=df.index)
        out["st_line"] = up.fillna(down)
        out["st_dir"] = np.where(up.notna(), 1, np.where(down.notna(), -1, np.nan))
    elif "st_line" in mapping:
        out["st_line"] = to_number(df[mapping["st_line"]])
        out["st_dir"] = np.where(out["close"] > out["st_line"], 1, np.where(out["st_line"].notna(), -1, np.nan))
        warnings.append("Supertrend direction inferred from close vs the Supertrend line.")
    else:
        out["st_line"] = np.nan
        out["st_dir"] = np.nan

    rows_in_file = len(out)
    out = out.dropna(subset=["ts", "open", "high", "low", "close"])
    dropped_nan = rows_in_file - len(out)
    if bad_ts or dropped_nan:
        warnings.append(f"{dropped_nan} row(s) dropped: unreadable time or missing price.")
    dup = int(out["ts"].duplicated(keep="last").sum())
    if dup:
        warnings.append(f"{dup} duplicate timestamp(s) inside the file; the last occurrence was kept.")
        out = out.drop_duplicates("ts", keep="last")
    out = out.sort_values("ts").reset_index(drop=True)
    bad_ohlc = (out["high"] < out[["open", "close"]].max(axis=1) - 1e-9) | (out["low"] > out[["open", "close"]].min(axis=1) + 1e-9)
    if bad_ohlc.any():
        warnings.append(f"{int(bad_ohlc.sum())} bar(s) with high/low inconsistent with open/close were dropped.")
        out = out[~bad_ohlc].reset_index(drop=True)
    if out.empty:
        raise IngestError("No valid bars after cleaning")

    step = out["ts"].diff().dt.total_seconds().dropna()
    median = float(step.median()) if len(step) else 86400
    if median <= 61:
        interval = "1m"
    elif median >= 3600 * 20:
        interval = "1d"
    else:
        raise IngestError(f"Bars are {median / 60:.0f} minutes apart. Upload 1-minute or daily data.")
    if interval == "1d":
        out["ts"] = out["ts"].dt.normalize()
    out["day"] = out["ts"].dt.strftime("%Y-%m-%d")

    sessions = session_report(out) if interval == "1m" else []
    if interval == "1m":
        gaps = int(sum(s["gaps"] for s in sessions))
        if gaps:
            warnings.append(f"{gaps} missing minute(s) inside sessions.")
        for s in sessions:
            if s["flag"] != "normal":
                warnings.append(f"{s['day']}: {s['flag']} session ({s['bars']} bars, {s['first']}–{s['last']}).")
    if out["volume"].notna().any() and (out["volume"] == 0).any():
        warnings.append(f"{int((out['volume'] == 0).sum())} bar(s) have zero volume.")
    if interval == "1m" and "volume" in mapping:
        warnings.append("Index volume is a TradingView-derived figure; it is used only relative to its own normal level.")
    if "st_up" in mapping or "st_line" in mapping:
        missing_st = int(out["st_line"].isna().sum())
        if missing_st:
            warnings.append(f"{missing_st} bar(s) without a Supertrend value (indicator warm-up).")
    if ignored:
        empty = [c for c in ignored if df[c].isna().all()]
        if empty:
            warnings.append(f"Empty column(s) ignored: {', '.join(map(str, empty))}.")
        rest = [c for c in ignored if c not in empty]
        if rest:
            warnings.append(f"Column(s) not used: {', '.join(map(str, rest))}.")

    epoch = (out["ts"] - pd.Timestamp(0, tz="UTC")).dt.total_seconds().round().astype("int64")
    rows = [
        (int(e), d, *(None if pd.isna(v) else float(v) for v in r[:7]),
         None if pd.isna(r[7]) else int(r[7]), *(None if pd.isna(v) else float(v) for v in r[8:]))
        for e, d, r in zip(epoch, out["day"], out[["open", "high", "low", "close", "volume", "vwap", "st_line",
                                                   "st_dir", "ema", "rsi", "rsi_ma"]].itertuples(index=False, name=None))
    ]
    return {"interval": interval, "rows": rows, "rows_in_file": rows_in_file, "mapping": mapping,
            "warnings": warnings, "first_day": out["day"].iloc[0], "last_day": out["day"].iloc[-1],
            "sessions": len(set(out["day"])), "flagged_sessions": [s for s in sessions if s["flag"] != "normal"]}


def session_report(bars: pd.DataFrame) -> list[dict]:
    """One entry per 1-minute session: bar count, gaps and a flag (normal / partial / special)."""
    out = []
    for day, g in bars.groupby("day"):
        t = g["ts"]
        first, last = t.iloc[0].time(), t.iloc[-1].time()
        span = int((t.iloc[-1] - t.iloc[0]).total_seconds() // 60) + 1
        weekend = t.iloc[0].weekday() >= 5
        flag = "normal"
        if weekend or first >= time(12, 0):
            flag = "special"             # Muhurat, Budget-day Sunday etc.
        elif first > SESSION_OPEN or last < SESSION_LAST_BAR:
            flag = "partial"
        out.append({"day": day, "bars": len(g), "gaps": max(0, span - len(g)), "first": first.strftime("%H:%M"),
                    "last": last.strftime("%H:%M"), "flag": flag})
    return out


# ---------------------------------------------------------------------------
# Strategy P&L (daily table or trade log)
# ---------------------------------------------------------------------------
PNL_EXCLUDE = ("cum", "running", "equity", "balance", "%", "percent", "pct", "drawdown", "max", "capital", "margin")


def parse_strategy_pnl(raw: bytes, filename: str) -> dict:
    df = read_table(raw, filename)
    if df.empty:
        raise IngestError("The file has no rows")
    cols = {norm(c): c for c in df.columns}
    dcol = _find(cols, "date", "day", "trade date", "exit date", "exit time", "exit datetime", "datetime", "time",
                 contains=("exit date", "exit time", "date", "day"), exclude=("days", "weekday"))
    if dcol is None:
        raise IngestError(f"No date column found. Found: {', '.join(map(str, df.columns))}")
    net_col = _find(cols, "net p&l", "net pnl", "net profit", contains=("net p&l", "net pnl", "net profit"),
                    exclude=PNL_EXCLUDE)
    gross_col = _find(cols, "gross p&l", "gross pnl", "p&l", "pnl", "profit", "realised p&l", "realized p&l",
                      "realised pnl", "realized pnl", "mtm", "profit loss", "profit & loss",
                      contains=("gross", "p&l", "pnl", "profit", "realised", "realized", "mtm"),
                      exclude=PNL_EXCLUDE + ("net",))
    charges_col = _find(cols, "charges", "brokerage", "costs", "fees", "taxes",
                        contains=("charge", "brokerage", "cost", "fee", "tax"), exclude=("price",))
    trades_col = _find(cols, "trades", "orders", "no of trades", "number of trades", contains=("trades", "orders"))
    strat_col = _find(cols, "strategy", "strategy name", contains=("strategy",), exclude=("id",))
    if gross_col is None and net_col is None:
        raise IngestError(f"No P&L column found (looked for P&L / PnL / Profit / Net P&L). "
                          f"Found: {', '.join(map(str, df.columns))}")

    warnings: list[str] = []
    days, note = parse_dates(df[dcol])
    if note:
        warnings.append(note)
    frame = pd.DataFrame({"day": days})
    charges = to_number(df[charges_col]).abs() if charges_col else None
    if gross_col is not None:
        frame["gross"] = to_number(df[gross_col])
        frame["charges"] = charges if charges is not None else np.nan
        basis = "gross P&L" + (" with a charges column" if charges is not None else
                               " (no charges column: estimated costs will be applied)")
    else:
        net = to_number(df[net_col])
        if charges is not None:
            frame["gross"] = net + charges
            frame["charges"] = charges
            basis = "net P&L + charges column"
        else:
            frame["gross"] = net
            frame["charges"] = 0.0
            basis = "net P&L (already after costs: no further costs applied)"
    frame["trades"] = to_number(df[trades_col]) if trades_col else np.nan
    frame["strategy"] = df[strat_col].astype(str).str.strip() if strat_col else None
    rows_in_file = len(frame)
    bad = frame["day"].isna() | frame["gross"].isna()
    if bad.any():
        warnings.append(f"{int(bad.sum())} row(s) skipped: unreadable date or P&L.")
        frame = frame[~bad]
    if frame.empty:
        raise IngestError("No usable rows after cleaning")

    trade_level = frame.groupby(["strategy", "day"] if strat_col else ["day"], dropna=False).size().max() > 1
    groups: dict[str | None, list[tuple]] = {}
    for name, g in (frame.groupby("strategy") if strat_col else [(None, frame)]):
        agg = g.groupby("day").agg(gross=("gross", "sum"), charges=("charges", lambda x: x.sum(min_count=1)),
                                   trades=("trades", lambda x: x.sum(min_count=1)), n=("gross", "size"))
        rows = []
        for day, r in agg.sort_index().iterrows():
            trades = r["trades"] if not pd.isna(r["trades"]) else (r["n"] if trade_level else None)
            rows.append((day, round(float(r["gross"]), 2), None if pd.isna(r["charges"]) else round(float(r["charges"]), 2),
                         None if trades is None or pd.isna(trades) else int(trades)))
        groups[name] = rows
    if trade_level:
        warnings.append("Several rows per day: treated as a trade log and summed into daily P&L.")
    weekend = sorted({r[0] for rows in groups.values() for r in rows if pd.Timestamp(r[0]).weekday() >= 5})
    if weekend:
        warnings.append(f"Weekend session(s) present: {', '.join(weekend)} (e.g. Budget day); kept but flagged.")
    all_days = [r[0] for rows in groups.values() for r in rows]
    return {"groups": groups, "rows_in_file": rows_in_file, "warnings": warnings, "basis": basis,
            "mapping": {k: v for k, v in {"date": dcol, "gross": gross_col, "net": net_col, "charges": charges_col,
                                          "trades": trades_col, "strategy": strat_col}.items() if v is not None},
            "first_day": min(all_days), "last_day": max(all_days), "trade_level": bool(trade_level)}


def match_strategy(name: str, strategies: list[dict]) -> str | None:
    """Map a strategy name from a file to a registered strategy id (exact id/name, then containment)."""
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    for s in strategies:
        if key in (s["id"].replace("_", ""), re.sub(r"[^a-z0-9]", "", s["name"].lower())):
            return s["id"]
    hits = [s["id"] for s in strategies if key and (key in re.sub(r"[^a-z0-9]", "", s["name"].lower())
                                                     or re.sub(r"[^a-z0-9]", "", s["name"].lower()) in key)]
    return hits[0] if len(hits) == 1 else None
