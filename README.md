# Strategy Recommender

Looks at the market over a **1-day, 1-week or 1-month** horizon and tells
participants which of your algo strategies fits current conditions best,
or tells them to sit out. Claude does the analysis, and the admin can switch
between **Haiku 5.5, Sonnet 5.5 and Opus 5.5** at any time to control cost.

```
candles (Groww / Twelve Data / yfinance)
   └─► indicators + regime (pandas, deterministic)  ── trend, ADX, volatility pct, squeeze, gaps…
         └─► rule engine pre-score per strategy
               └─► Claude (admin-selected model) ranks every strategy  ──► participant UI
                     ↑ strategy catalog from config/strategies/*.yaml
```

Claude never reads raw candles. All the maths runs in Python, and Claude gets a
summary of about 1–2k tokens plus your strategy catalog. That keeps each analysis at
roughly **$0.001 on Haiku, $0.02 on Sonnet and $0.04 on Opus**.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in ANTHROPIC_API_KEY, ADMIN_TOKEN, data keys
uvicorn app.main:app --reload
```

* Participants: http://localhost:8000/
* Admin: http://localhost:8000/admin (sign in with `ADMIN_TOKEN`)
* Tests: `pytest`

## Adding your strategies

Copy `config/strategies/_TEMPLATE.yaml` to `config/strategies/<id>.yaml` and fill it in.
The five files in that folder are **examples**. Replace them with your real logic.

* `logic`, `avoid_conditions`, `risk_notes`: free text that Claude reads. Be concrete.
* `ideal_conditions`: machine-readable hints (trend / strength / volatility / squeeze).
  The built-in rule engine uses them as a pre-score and as the fallback.
* `horizons`: which of 1d / 1w / 1m the strategy is meant for.

After editing, click **Reload strategy files** in the admin panel. No restart is needed.

## Admin controls (cost)

| Control | What it does |
|---|---|
| Model | Haiku 5.5 / Sonnet 5.5 / Opus 5.5. Applies to the next analysis. |
| Thinking effort | low / medium / high. Lower means fewer tokens. |
| Monthly budget | USD cap, tracked from real token usage per call. |
| When budget runs out | switch to Haiku, stop AI and use the rule engine, or block. |
| Re-use result (min) | Cache per instrument + horizon. 100 participants checking NIFTY in 15 min cost 1 call. |
| Provider order | Which data source to try first. |

Usage and cost per call are logged in `data/app.db` and shown in the admin panel.
Model prices are in `config/settings.yaml`. Update them there if pricing changes.

## Market data

| Provider | Key | Notes |
|---|---|---|
| **Groww** | `GROWW_ACCESS_TOKEN`, or `GROWW_API_KEY` + `GROWW_API_SECRET` | Best for NSE/BSE: exchange-grade, intraday candles. Default first choice. The app mints the daily token itself from key + secret. |
| **Twelve Data** | `TWELVEDATA_API_KEY` | Good backup. Free plan: 8 req/min, and Indian indices may need a paid plan. |
| **yfinance** | none | Free last-resort fallback. Unofficial and can lag about 15 min. Fine for 1w / 1m. |

Providers without keys are skipped. If one fails, the next is tried.
Instrument symbols for each provider live in `config/settings.yaml`. **Check the index
symbols against your Groww / Twelve Data accounts.** Plain NSE stocks (e.g. `RELIANCE`) are
mapped automatically.

## Reliability

* Claude's output is schema-validated (structured outputs). Unknown strategy ids are dropped
  and missing ones are filled from the rule engine.
* On Sonnet / Opus, the server-side refusal fallback (`fallbacks: "default"`) is enabled.
  If Claude errors or declines, the participant gets the rule-engine result with a notice.
* Only the admin can bypass the cache ("Run fresh analysis").

*Decision support only. Not financial advice.*
