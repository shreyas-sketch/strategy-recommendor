"""Import Tradetron strategy exports ("_format": "tt-strategy/v2") as draft strategies.

The export holds the executable logic as condition ASTs ("conditionJson") plus order legs.
This module turns that into plain-English rules Claude can read, detects what the strategy
trades and how (option buying/selling, straddles, directional), and suggests suitability
and ideal market conditions. The admin reviews the draft before it is saved.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

SUPPORTED_FORMATS = {"tt-strategy/v2"}
MAX_BYTES = 2_000_000

# Tradetron underlying names -> instrument keys in config/settings.yaml
UNDERLYING_KEYS = {
    "NIFTY BANK": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY",
    "NIFTY 50": "NIFTY50", "NIFTY": "NIFTY50",
    "NIFTY FIN SERVICE": "FINNIFTY", "FINNIFTY": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX", "BANKEX": "BANKEX",
}
COMPARATORS = {">": "is above", "<": "is below", ">=": "is at or above", "<=": "is at or below",
               "==": "equals", "!=": "is not", "crosses above": "crosses above", "crosses below": "crosses below"}
TIME_COMPARATORS = {">": "is after", "<": "is before", ">=": "is at or after", "<=": "is at or before"}


class TradetronError(ValueError):
    pass


# ---------------------------------------------------------------------------
# AST -> English
# ---------------------------------------------------------------------------
def _instrument(spec: str) -> str:
    """'NFO,NIFTY 50,Current Week,CE,ATM-SPOT,0,0' -> 'NIFTY 50 current-week ATM CE'."""
    parts = [p.strip() for p in str(spec).split(",")]
    parts += [""] * (7 - len(parts))
    _exch, underlying, expiry, opt, strike, offset = parts[:6]
    if not (expiry or opt):
        return f"{underlying} (spot/index)"
    text = underlying
    if expiry:
        text += f" {expiry.lower()}"
    if strike:
        s = strike.replace("-SPOT", "").replace("ATM", "ATM")
        if offset and offset not in ("0", ""):
            s += f"{'+' if not offset.startswith('-') else ''}{offset}"
        text += f" {s}"
    if opt:
        text += f" {opt}"
    return text


def _time(v: str) -> str:
    v = str(v)
    return f"{v[:2]}:{v[2:]}" if re.fullmatch(r"\d{4}", v) else v


def _value(p: dict) -> str:
    if p.get("type") == "keyword":
        return _kw(p["keyword"])
    return str(p.get("value", ""))


def _kw(k: dict) -> str:
    name = k.get("name", "?")
    ps = k.get("params") or []
    vals = [_value(p) for p in ps]
    raw = [p.get("value") for p in ps if p.get("type") == "value"]
    if name == "Instrument Name":
        return _instrument(raw[0] if raw else "")
    if name == "Symbol":                         # (instrument, timeframe, session)
        return f"{vals[0]} {vals[1]} candles" if len(vals) > 1 else vals[0]
    if name == "OHLC Sum":                       # combined premium of two legs
        tf = raw[-1] if raw else ""
        legs = [v for p, v in zip(ps, vals) if p.get("type") == "keyword"]
        return f"combined premium of {' + '.join(legs)} ({tf} candles)"
    if name in ("CLOSE", "OPEN", "HIGH", "LOW", "VOLUME"):
        return f"{name.lower()} of {vals[0]}" if vals else name.lower()
    if name in ("SUPERTREND", "EMA", "SMA", "RSI", "ATR", "ADX", "VWAP", "BBANDS", "KELTNER"):
        args = ", ".join(vals[1:])
        return f"{name.title() if name != 'VWAP' else 'VWAP'}({args}) of {vals[0]}" if vals else name
    if name == "Position":                       # (series, index): -1 last completed candle, 0 current
        idx = raw[-1] if raw else ""
        when = {"-1": "last completed candle", "0": "current (forming) candle"}.get(str(idx), f"candle {idx}")
        return f"{vals[0]} on the {when}"
    if name == "Time":
        return "the time"
    if name == "Number":
        return raw[0] if raw else "?"
    if name == "Positions Detail":               # (set, leg, option type, field, underlying)
        return f"open {raw[2] if len(raw) > 2 else ''} {raw[3] if len(raw) > 3 else 'quantity'} on {raw[4] if len(raw) > 4 else ''}".strip()
    if name == "Traded Instrument Name":         # (stage, field, underlying, set, ?, leg)
        return f"the traded leg {raw[5] if len(raw) > 5 else '?'} of set {raw[3] if len(raw) > 3 else '?'}"
    if name == "Days Difference (D2-D1)":
        return f"days from {vals[0]} to {vals[1]}" if len(vals) > 1 else "days difference"
    if name == "Today":
        return "today"
    if name == "Current Week Expiry":
        return f"the {raw[0] if raw else ''} current weekly expiry".replace("  ", " ")
    if name == "Current Month Expiry":
        return f"the {raw[0] if raw else ''} current monthly expiry".replace("  ", " ")
    if name == "Week Day":
        return "the weekday number"
    if name == "Get Runtime":
        return f"runtime variable '{raw[0] if raw else '?'}' (set outside this strategy)"
    if name == "ATM SPOT":
        off = raw[1] if len(raw) > 1 else "0"
        return f"ATM{'' if off in ('0', '') else ('+' + off if not off.startswith('-') else off)} strike (spot-based)"
    return f"{name}({', '.join(vals)})"


def _rule(elements: list[dict]) -> str:
    if len(elements) == 3 and elements[1].get("name") in COMPARATORS:
        lhs, op, rhs = elements
        left, right = _kw(lhs), _kw(rhs)
        verb = COMPARATORS[op["name"]]
        if lhs.get("name") == "Time":
            right = _time(right)
            verb = TIME_COMPARATORS.get(op["name"], verb)
        return f"{left} {verb} {right}"
    return " ".join(_kw(e) if e.get("name") not in COMPARATORS else COMPARATORS[e["name"]] for e in elements)


def translate_condition(node: dict | None, depth: int = 0) -> str:
    if not node:
        return ""
    if "operator" in node:
        parts = [translate_condition(o, depth + 1) for o in node.get("operands", [])]
        parts = [p for p in parts if p]
        joined = f" {node['operator'].upper()} ".join(parts)
        return f"({joined})" if depth and len(parts) > 1 else joined
    if node.get("type") == "group":
        return translate_condition(node.get("children"), depth)
    if node.get("type") == "rule":
        return _rule(node.get("elements", []))
    return ""


def _parse_condition(cond: dict) -> str:
    raw = cond.get("conditionJson")
    if raw:
        try:
            return translate_condition(json.loads(raw) if isinstance(raw, str) else raw)
        except (ValueError, TypeError, KeyError, IndexError):
            pass
    return cond.get("conditionDisplay") or cond.get("conditionValue") or ""


def describe_leg(leg: dict) -> str:
    side = {"B": "BUY", "S": "SELL"}.get(leg.get("buySell"), leg.get("buySell") or "?")
    lots = leg.get("qtyDisplay") or "?"
    unit = "lot" if str(lots) == "1" and leg.get("qtyType") == "Lots" else ("lots" if leg.get("qtyType") == "Lots" else "qty")
    opt = leg.get("optionType") or ""
    underlying = leg.get("underlyingSymbol") or ""
    if leg.get("strikeType") == "ATM":
        strike = "ATM"
    elif leg.get("strikeJson"):
        try:
            strike = translate_condition(json.loads(leg["strikeJson"]))
        except (ValueError, TypeError):
            strike = leg.get("strikeDisplay") or leg.get("strike") or ""
    else:
        strike = leg.get("strikeDisplay") or leg.get("strike") or ""
    instrument = f"{opt} {strike}".strip() if opt in ("CE", "PE") else (opt or "futures/stock")
    expiry = (leg.get("expiryType") or "").lower()
    product = {"MIS": "intraday (MIS)", "NRML": "NRML"}.get(leg.get("productType"), leg.get("productType") or "")
    extras = [x for x in (f"{expiry} expiry" if expiry else "", product) if x]
    sl = " with stop-loss" if leg.get("sLTrigger") else ""
    tgt = " with target" if leg.get("targetTrigger") else ""
    return f"{side} {lots} {unit} {underlying} {instrument} ({', '.join(extras)}){sl}{tgt}".replace("  ", " ")


# ---------------------------------------------------------------------------
# Classification and suggestions
# ---------------------------------------------------------------------------
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (s or "strategy")[:40].rstrip("_")


def _intraday_exit(sets: list[dict]) -> str | None:
    for st in sets:
        for c in st.get("conditions", []):
            if c.get("type") == "Universal Exit":
                m = re.search(r"tt_Number \( '(\d{4})' \)", c.get("conditionValue") or "")
                if m and "Time" in (c.get("conditionValue") or ""):
                    return _time(m.group(1))
    return None


def analyse(data: dict) -> dict:
    """Return a draft strategy dict (matching app.config.Strategy) plus warnings and the translated logic."""
    if data.get("_format") not in SUPPORTED_FORMATS:
        raise TradetronError(f"Unsupported format {data.get('_format')!r} — expected a Tradetron 'tt-strategy/v2' export")
    name = (data.get("name") or "Imported strategy").strip()[:80]
    sets = data.get("sets") or []
    warnings: list[str] = []

    blocks, legs_all, entry_sets = [], [], []
    for i, st in enumerate(sets, start=1):
        lines = []
        set_legs = []
        for c in st.get("conditions", []):
            ctype = c.get("type")
            if ctype == "Repair Once" or not c.get("visible", 1) and not c.get("conditionJson"):
                continue
            text = _parse_condition(c)
            if ctype == "Universal Exit":
                lines.append(f"  Exit everything when {text}.")
                continue
            if ctype in ("Entry", "Exit"):
                lines.append(f"  {ctype} when {text}." if text else f"  {ctype}: (no condition)")
            else:
                lines.append(f"  {ctype}: {text}")
            for leg in c.get("legs") or []:
                set_legs.append(leg)
                lines.append(f"    -> {describe_leg(leg)}")
        if not lines:
            continue
        has_entry = any(c.get("type") == "Entry" for c in st.get("conditions", []))
        title = f"Set {i}" if has_entry else "All sets"
        blocks.append(f"{title}:\n" + "\n".join(lines))
        if set_legs:
            entry_sets.append(set_legs)
            legs_all.extend(set_legs)

    exit_time = _intraday_exit(sets)
    underlyings_raw = [leg.get("underlyingSymbol") for leg in legs_all if leg.get("underlyingSymbol")]
    underlyings = []
    for u in dict.fromkeys(underlyings_raw):
        key = UNDERLYING_KEYS.get(u.upper())
        if key:
            underlyings.append(key)
        else:
            warnings.append(f"Unknown underlying '{u}' — add it to config/settings.yaml instruments and to 'underlyings'.")

    options = [leg for leg in legs_all if leg.get("optionType") in ("CE", "PE")]
    sells = [leg for leg in options if leg.get("buySell") == "S"]
    buys = [leg for leg in options if leg.get("buySell") == "B"]
    straddle_sets = sum(1 for s in entry_sets if {l.get("optionType") for l in s if l.get("buySell") == "S"} >= {"CE", "PE"})
    uses_runtime = "get_runtime" in json.dumps(data)
    indicators = sorted(set(re.findall(r"tt_(SUPERTREND|EMA|SMA|RSI|ATR|ADX|VWAP|BBANDS|KELTNER)", json.dumps(data))))
    expiry_bound = "weekexpiry" in json.dumps(data) and "tt_days_diff" in json.dumps(data) or "tt_Today" in json.dumps(data)

    # --- style ---------------------------------------------------------------
    if straddle_sets:
        style, category = "Short straddle / premium selling", "volatility_selling"
        ideal = {"trend": ["sideways"], "trend_strength": ["weak", "moderate"], "volatility": ["low", "normal"], "squeeze": "any"}
        avoid = ["Strong trending or gap days — one leg runs away",
                 "Volatility expanding or event days (RBI policy, budget, results)"]
        risk_notes = "Naked short options: losses are open-ended on a large move; margin-heavy."
        risk, exp = "high", "advanced"
    elif sells and not buys:
        style, category = "Directional option selling", "trend"
        ideal = {"trend": ["up", "down"], "trend_strength": ["moderate", "strong"], "volatility": ["normal", "high"], "squeeze": "any"}
        avoid = ["Sharp reversals against the position", "Choppy days that flip the signal repeatedly"]
        risk_notes = "Sells naked options in the trend direction: collects premium but a fast reversal causes large losses."
        risk, exp = "high", "advanced"
    elif buys and not sells:
        style, category = "Directional option buying (trend following)", "trend"
        ideal = {"trend": ["up", "down"], "trend_strength": ["moderate", "strong"], "volatility": ["normal", "high"], "squeeze": "any"}
        avoid = ["Sideways, choppy sessions — repeated whipsaws and premium decay",
                 "Very low volatility — option premiums barely move"]
        risk_notes = "Bought options lose value through time decay; whipsaws in a range cause a string of small losses."
        risk, exp = "medium", "intermediate"
    else:
        style, category = "Mixed / other", "other"
        ideal = {"trend": [], "trend_strength": [], "volatility": [], "squeeze": "any"}
        avoid, risk_notes, risk, exp = [], "", "medium", "intermediate"
        warnings.append("Could not classify the strategy style — set category and ideal conditions manually.")

    instruments = sorted({"options" if leg.get("optionType") in ("CE", "PE") else "futures" for leg in legs_all}) or ["index"]
    horizons = ["1d", "1w"] if exit_time else ["1d", "1w", "1m"]
    capital = int(data.get("capitalRequired") or 0)

    facts = []
    if exit_time:
        facts.append(f"Intraday: everything is closed by {exit_time} IST.")
    if expiry_bound:
        facts.append("Entries are tied to the weekly-expiry calendar (see the conditions) — it trades only on specific days.")
    if indicators:
        facts.append("Indicators used: " + ", ".join(i.title() for i in indicators) + ".")
    lots = Counter(str(s[0].get("qtyDisplay")) for s in entry_sets)
    if len(lots) > 1:
        facts.append("Position size varies by set: " + ", ".join(f"{n} set(s) with {q} lot(s) per leg" for q, n in lots.items()) + ".")
    if capital:
        facts.append(f"Capital required (per Tradetron): ₹{capital:,}.")
    if uses_runtime:
        keys = sorted(set(re.findall(r"get_runtime \( 'strategy_id', 'run_counter', '([^']+)' \)", json.dumps(data))))
        warnings.append("Entry/exit signals come from runtime variables set outside this export "
                        f"({', '.join(keys)}). Describe what sets them (e.g. the Keltner Channel rule) in Logic — "
                        "otherwise the recommender cannot judge when this strategy fires.")
        facts.append(f"Signals come from runtime variables {', '.join(keys)} that are set by an external process "
                     "(logic not included in the export).")

    logic = f"Style: {style} on {', '.join(dict.fromkeys(underlyings_raw)) or 'unknown underlying'}.\n"
    logic += "\n".join(facts) + "\n\n" + "\n\n".join(blocks)
    description = (f"{style} on {', '.join(dict.fromkeys(underlyings_raw)) or 'index'}"
                   f"{' (intraday)' if exit_time else ''}"
                   f"{' using ' + ', '.join(i.title() for i in indicators) if indicators else ''}.")[:300]

    draft = {
        "id": _slug(name), "name": name, "category": category, "instruments": instruments, "horizons": horizons,
        "description": description, "logic": logic[:12000], "ideal_conditions": ideal,
        "avoid_conditions": avoid, "risk_notes": risk_notes,
        "risk_level": risk, "min_capital_inr": capital, "min_experience": exp,
        "underlyings": underlyings,
    }
    warnings.append("Ideal market conditions, risk level and experience are suggestions from the strategy's structure — review them.")
    return {"draft": draft, "warnings": warnings}


def load_export(raw: bytes) -> tuple[dict, str]:
    if len(raw) > MAX_BYTES:
        raise TradetronError("File is larger than 2 MB")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TradetronError("Not a valid JSON file") from exc
    if not isinstance(data, dict):
        raise TradetronError("Unexpected JSON structure")
    return data, hashlib.sha256(raw).hexdigest()
