# Strategy Recommender

Tells each participant which of your algo strategies to run, based on the market
(1 day / 1 week / 1 month) and on **their own portfolio**. The pick reaches them on
WhatsApp every trading morning via AiSensy. Claude does the analysis, and the admin
can switch between **Haiku 5.5, Sonnet 5.5 and Opus 5.5** at any time to control cost.

```
candles (Groww / Twelve Data / yfinance) ─► indicators + regime (pandas)
                                              │
portfolio upload (CSV/XLSX) ─► holdings ──────┤
                                              ▼
                     Claude (admin-selected model) ranks your strategies
                     + explains why the pick beats each alternative
                                              │
                      web app ◄───────────────┴──► 06:00 IST WhatsApp (AiSensy)
```

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in ANTHROPIC_API_KEY, ADMIN_TOKEN, …
echo "COOKIE_SECURE=0" >> .env  # local http only
uvicorn app.main:app --reload
pytest                          # 59 tests
```

* Admin: http://localhost:8000/admin (sign in with `ADMIN_TOKEN`)
* Participants: http://localhost:8000/ (sign in after accepting an invite)

## Deploy on Railway

1. **New project → Deploy from GitHub repo.** The repo's `Dockerfile` and `railway.json`
   are picked up automatically, including the health check on `/healthz`.
2. **Attach a volume** to the service with mount path `/data`. **This is required.** The
   database (participants, portfolios, usage), admin settings and strategies all live
   there. Without it, everything is wiped on every redeploy. The app finds the volume
   through `RAILWAY_VOLUME_MOUNT_PATH`.
3. **Variables:** `ANTHROPIC_API_KEY`, `ADMIN_TOKEN` (at least 24 random characters, e.g.
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`), `AISENSY_API_KEY`, and
   your data keys (`GROWW_API_KEY` + `GROWW_API_SECRET`, `TWELVEDATA_API_KEY`).
4. **Networking → Generate domain.** Invite links use it automatically
   (`RAILWAY_PUBLIC_DOMAIN`), or set `PUBLIC_BASE_URL` for a custom domain.
5. **Keep replicas at 1.** The 6 AM scheduler and SQLite live in the single process.
   100 participants is far below its capacity, and a database lock also stops duplicate
   sends.

The app refuses to start in production if `ADMIN_TOKEN` is weak, and it logs a loud
warning if no volume is attached. API docs (`/docs`) are disabled in production.

## Participants and privacy

* **Onboarding:** the admin adds participants one by one or by CSV bulk import
  (`name,phone`). Each gets a personal invite link: one use, expires in 7 days. On it they
  set a password, then sign in with mobile number + password. "New invite" in the admin
  panel resets a forgotten password and signs out all of that person's devices.
* **Isolation:**
  * Every participant endpoint takes the user from a server-side session.
  * No endpoint accepts a user id from the browser, so nobody can request someone else's
    data.
  * Tests verify that one participant can't read or delete another's portfolio or
    recommendation.
* **The admin sees** who has uploaded a portfolio and how many holdings it has, never
  the holdings themselves.
* **Claude receives** symbols, weights, prices and P&L percentages only, never names or
  phone numbers.
* **Deleting a participant** removes their portfolio, recommendations, sessions and
  message log.

Security measures:

| Area | Measure |
|---|---|
| Passwords | scrypt-hashed |
| Sessions | random, stored hashed; HttpOnly + Secure + SameSite cookies, expire after 14 days |
| Brute force | login rate-limited per IP and per phone number |
| CSRF | header check on every state-changing request |
| Headers | strict CSP, no framing, `no-store` on API responses |
| Uploads | 2 MB request limit; files parsed in memory, never stored; XML-attack-safe Excel parsing |
| Cost abuse | per-participant daily caps on fresh AI analyses and uploads |

## Trading profiles (suitability)

Each participant fills in **risk tolerance, experience, capital and whether they have an
F&O account** (Settings → My trading profile). Each strategy has a **risk level, minimum
capital and minimum experience** (Admin → Strategies). Strategies whose instruments are
only options/futures also need an F&O account.

* **Hard filter, applied by fixed rules before any AI:** a strategy the participant
  doesn't meet is never recommended to them. It's listed under "Not suitable for your
  profile", with the reason.
* **When the market's best fit is filtered out,** the page says so and picks the best of
  the strategies that do suit them. If nothing suits them, the answer is "sit out".
* **Claude weighs the profile** within the suitable set, e.g. simpler rules for beginners.
  It receives a capital band (e.g. "₹1–5 lakh"), never the exact amount.
* **No extra AI cost on the market tool:** the analysis is still shared across
  participants, and suitability is applied per person on top.
* **Without a profile** nothing is filtered, and participants are prompted to fill it in.
* **Admin view:** the admin sees only whether a profile is set, not its contents.

Strategies created before this feature default to medium risk, ₹0 minimum and beginner.
Review them in Admin → Strategies.

## Portfolio → today's strategy

Participants upload a broker holdings export (.csv or .xlsx), or fill in the template
CSV (`symbol,quantity,avg_price`).
* **Symbols:** common broker column names are recognised.
* **Company names:** exports that list names + ISIN (e.g. Groww) are matched through
  Groww's public instrument list.
* **Unmatched rows** are shown to the participant, never silently dropped.

Each morning the app:
1. takes their 15 largest holdings,
2. measures trend, volatility, 1-month return and P&L for each,
3. combines that with the benchmark index regime (default NIFTY 50),
4. asks Claude which strategy to run today and on what (the index or a specific
   holding), with the same "why this over the others" comparison as the market tool.

Participants without a portfolio get the market-only recommendation.

## Daily WhatsApp at 06:00 IST (AiSensy)

1. In AiSensy, create a WhatsApp template, for example:
   `Good morning {{1}}! Today's suggested strategy: {{2}} on {{3}}. {{4}} Open the app for the full analysis.`
   Get it approved, then create an **API campaign** using it and set it **Live**.
2. In Admin → Daily WhatsApp notifications:
   * enter the campaign name, then **Send test message** to your own number;
   * tick **Send daily message**, set the time, weekdays only, and NSE holidays.
3. Participants opt in under Settings. Only opted-in, active participants get messages.

How it runs:
* Picks are prepared 25 minutes before send time, so 100 Claude calls aren't squeezed
  into one minute.
* Each participant gets at most one message per day.
* If the app was down at 6 AM, it won't send late messages after 8 AM.
* "Prepare / Send now" buttons and a delivery log are in the admin panel.

Template params are sent as: `[first name, strategy name, instrument, one-line reason]`,
with numbers as `91XXXXXXXXXX`. Check the first test send against your AiSensy account.

## Costs (approx.)

| | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 |
|---|---|---|---|
| Per analysis | ~$0.001 | ~$0.02 | ~$0.04 |
| 100 daily portfolio picks × 22 trading days | ~$2/month | ~$45/month | ~$90/month |

Market-tool results are shared across participants for 15–360 minutes. A monthly budget
can auto-downgrade to Haiku, switch to the free rule engine, or block. Model prices are in
`config/settings.yaml`.

## Strategies

Manage them in Admin → Strategies (stored on the volume as YAML), or copy
`config/strategies/_TEMPLATE.yaml`. The five shipped strategies are **examples** and are
seeded on first start. Replace them with your real logic. `logic`, `avoid_conditions` and
`risk_notes` are read by Claude. `ideal_conditions` drive the rule-engine pre-score and
the fallback.

## Market data

| Provider | Key | Notes |
|---|---|---|
| **Groww** | `GROWW_ACCESS_TOKEN`, or `GROWW_API_KEY` + `GROWW_API_SECRET` | Best for NSE/BSE. The app mints the daily token from key + secret. |
| **Twelve Data** | `TWELVEDATA_API_KEY` | Backup. Free plan: 8 req/min, which is too slow for many portfolios. |
| **yfinance** | none | Free last resort. Unofficial and delayed. |

Check the index symbols in `config/settings.yaml` against your Groww / Twelve Data
accounts.

*Decision support only. Not financial advice.*
