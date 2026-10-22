# Strategy Recommender

Tells participants which of your algo strategies to run, based **only on market data**
(1 day / 1 week / 1 month). Participants enter nothing about themselves: everyone gets
the same daily pick, judged on the indices your strategies trade. The pick reaches them on
WhatsApp every trading morning via AiSensy. Claude does the analysis, and the admin
can switch between **Haiku 5.5, Sonnet 5.5 and Opus 5.5** at any time to control cost.

```
candles (Groww / Twelve Data / yfinance) ─► indicators + regime (pandas)
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
pytest                          # 123 tests
```

* Admin: http://localhost:8000/admin (sign in with `ADMIN_TOKEN`)
* Participants: http://localhost:8000/ (sign in after accepting an invite)

## Deploy on Railway

1. **New project → Deploy from GitHub repo.** The repo's `Dockerfile` and `railway.json`
   are picked up automatically, including the health check on `/healthz`.
2. **Attach a volume** to the service with mount path `/data`. **This is required.** The
   database (participants, admins, usage), admin settings and strategies all live
   there. Without it, everything is wiped on every redeploy. The app finds the volume
   through `RAILWAY_VOLUME_MOUNT_PATH`.
3. **Variables:** `ANTHROPIC_API_KEY`, `MASTER_ADMIN_EMAIL` + `MASTER_ADMIN_PASSWORD` (and/or
   `ADMIN_TOKEN`, 24+ random characters), `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`, `AISENSY_API_KEY`, and
   your data keys (`GROWW_API_KEY` + `GROWW_TOTP_SECRET` for a TOTP key, `TWELVEDATA_API_KEY`).
4. **Networking → Generate domain.** Invite links use it automatically
   (`RAILWAY_PUBLIC_DOMAIN`), or set `PUBLIC_BASE_URL` for a custom domain.
5. **Keep replicas at 1.** The 6 AM scheduler and SQLite live in the single process.
   100 participants is far below its capacity, and a database lock also stops duplicate
   sends.

The app refuses to start in production if `ADMIN_TOKEN` is weak, and it logs a loud
warning if no volume is attached. API docs (`/docs`) are disabled in production.

## Sign-in

**Admins.** A master admin is created on first start from `MASTER_ADMIN_EMAIL` and
`MASTER_ADMIN_PASSWORD` (12+ characters).
* **Signing in:** email + password at `/admin`, or "Sign in with Google" with the same email.
* **Sub-admins:** added by the master under Admins. They manage participants and
  strategies; only the master changes the AI model, budget, WhatsApp settings and admins.
* **Admin token:** `ADMIN_TOKEN` still works as a master-level token for scripts or
  emergencies.
* **Forgotten master password:** set `MASTER_ADMIN_PASSWORD_RESET=1` with the new
  password for one deploy, then remove it.

**Participants.** Add each person with a Google email, a WhatsApp number, or both.
* **Google email:** they open the site and click "Sign in with Google". Only pre-approved
  emails get in; anyone else is told to ask the admin.
* **Phone:** they enter their mobile number, get a 6-digit **WhatsApp code** through the
  AiSensy login-code campaign (default `hub_login_code_`; Admin → WhatsApp), and they're in.
  Codes expire in 5 minutes, work once, lock after 5 wrong tries, and are rate-limited.
  The form never reveals whether a number is registered. They can also use an invite
  link to set a password. WhatsApp messages need a phone number.

**Setting up Google sign-in (one time, free):**
1. Go to Google Cloud Console → create a project → **APIs & Services → OAuth consent
   screen**. Choose **External**, fill in the app name and support email, and **publish**
   it. "Testing" mode only allows listed test users.
2. **Credentials → Create credentials → OAuth client ID**, type **Web application**.
   Authorised redirect URI: `https://<your-domain>/auth/google/callback`.
3. In Railway Variables, set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. The Google
   buttons appear automatically.

The app requests only `openid email profile`. It accepts only Google-verified emails,
checks every claim in the ID token, and stores no Google tokens.

## Participants and privacy

* **Onboarding:** the admin adds participants one by one or by CSV bulk import
  (`name,phone`). Each gets a personal invite link: one use, expires in 7 days. On it they
  set a password, then sign in with mobile number + password. "New invite" in the admin
  panel resets a forgotten password and signs out all of that person's devices.
* **Isolation:**
  * Every participant endpoint takes the user from a server-side session.
  * No endpoint accepts a user id from the browser, so nobody can request someone else's
    data.
* **No personal data is collected for recommendations.** There is no portfolio upload
  and no trading profile; the pick comes from market data alone.
* **Claude receives** index market data and the strategy catalog only, never names or
  phone numbers.
* **Deleting a participant** removes their account, sessions and message log.
* **Upgrading from an older version** deletes any stored portfolios and trading profiles
  on first start.

Security measures:

| Area | Measure |
|---|---|
| Passwords | scrypt-hashed |
| Sessions | random, stored hashed; HttpOnly + Secure + SameSite cookies, expire after 14 days |
| Brute force | login rate-limited per IP and per phone number |
| CSRF | header check on every state-changing request |
| Headers | strict CSP, no framing, `no-store` on API responses |
| Uploads | admin-only (Tradetron import); 2 MB limit |
| Cost abuse | per-participant daily cap on fresh AI analyses; one shared daily pick |

## The daily pick

**My pick** (and the 06:00 WhatsApp message) is one pick for everyone, computed once per
trading session:
1. drops strategies that don't trade that session (expiry-day rules, Groww calendar),
2. fetches the regime of each index the remaining strategies trade (strategies with no
   fixed index are judged on the benchmark, default NIFTY 50),
3. asks Claude which strategy to run and on which index, with the same "why this over
   the others" comparison as the market tool.

Every strategy is always considered; nothing is filtered per person.

## Daily WhatsApp at 06:00 IST (AiSensy)

1. In AiSensy, create a WhatsApp template, for example:
   `Good morning {{1}}! Today's suggested strategy: {{2}} on {{3}}. {{4}} Open the app for the full analysis.`
   Get it approved, then create an **API campaign** using it and set it **Live**.
2. In Admin → Daily WhatsApp notifications:
   * enter the campaign name, then **Send test message** to your own number;
   * tick **Send daily message**, set the time, weekdays only, and NSE holidays.
3. Participants opt in under Settings. Only opted-in, active participants get messages.

How it runs:
* The pick is prepared 25 minutes before send time (one Claude call, shared by everyone).
* Each participant gets at most one message per day.
* If the app was down at 6 AM, it won't send late messages after 8 AM.
* "Prepare / Send now" buttons and a delivery log are in the admin panel.

Template params are sent as: `[first name, strategy name, instrument, one-line reason]`,
with numbers as `91XXXXXXXXXX`. Check the first test send against your AiSensy account.

## Costs (approx.)

| | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 |
|---|---|---|---|
| Per analysis | ~$0.001 | ~$0.02 | ~$0.04 |
| Daily pick × 22 trading days (shared) | ~$0.02/month | ~$0.45/month | ~$0.90/month |

Market-tool results are shared across participants for 15–360 minutes. A monthly budget
can auto-downgrade to Haiku, switch to the free rule engine, or block. Model prices are in
`config/settings.yaml`.

## Strategies

### Importing from Tradetron

Admin → Strategies → **Import Tradetron JSON** accepts one or more `tt-strategy/v2`
exports. For each file the importer:

* **Translates** every entry/exit condition and order leg into plain English. This is
  the text Claude reads, e.g. "Entry when close of NIFTY BANK 15m candles on the last
  completed candle is above Supertrend(10, 3)… → BUY 1 lot NIFTY BANK CE ATM+2 strike".
* **Detects the index it trades** (`underlyings`: BANKNIFTY, NIFTY50, SENSEX, …), the style
  (option buying, directional selling, short straddle) and intraday exit times.
* **Notes the capital** (`capitalRequired`) in the logic text.
* **Suggests** ideal market conditions and "avoid when" from the structure. **Review these.**
* **Warns** when signals come from runtime variables set outside the export (e.g. the KC
  strategy's `BANKNIFTY_key1`/`key2`). Describe what sets them in Logic.

Nothing is saved until you click **Review & save**. Re-importing an updated export
refreshes the logic and underlyings, and keeps the conditions you already reviewed. Original files are kept on the volume under `imports/`.

### Each strategy is judged on its own index

A strategy with `underlyings` is scored only on that index's market regime. Bank Nifty
strategies are judged on Bank Nifty, and the NIFTY/SENSEX straddle on both indexes
(averaged).
* **Daily pick:** fetches each index the strategies trade, and
  gives Claude today's date and weekday (for expiry-day strategies).
* **Market check:** for an instrument, ranks only the strategies that trade it, plus
  generic ones.

### Which day a 1-day pick is for

A 1-day pick is for one trading session.
* **Before the 15:30 IST close on a trading day,** it's for today.
* **After the close, on weekends and on holidays** from the admin list, it's for the next
  trading session. The candles then end at the latest close.
* **What follows that day:** expiry-day checks, the date Claude is told, the cache, and the
  label on screen ("for trading on Tue, 13 Oct").
* **The 06:00 WhatsApp pick** is for that day's session. A participant who opens My pick
  the evening before gets the same pick, prepared once and reused at 06:00.

### Expiry calendar (Groww)

With Groww keys set, the app fetches each index's F&O expiry dates from Groww's
`historical/expiries` endpoint once a day, and caches them on the volume.
* **Facts per index:** next expiry, weekly or monthly, calendar and trading days to expiry
  (your holiday list is excluded), whether today is expiry day or the day before, and the
  next monthly expiry.
* **Weekly vs monthly** is worked out from the dates, so exchange schedule changes need
  no code change.
* **Strategy setting:** "Trades on which days" can be any day, expiry day only, or expiry
  day + the day before. The importer detects it from Tradetron conditions such as
  `tt_days_diff(Today, curr_weekexpiry) == 0/1`.
* **On days a strategy won't trade,** it's left out of the daily pick and the 1-day market
  check, and shown as "Not trading today" with the next expiry dates.
* **Claude sees** the expiry facts and weighs expiry-day dynamics for option strategies.
* **Admin → Expiry calendar** shows the dates and has a refresh button.
* **If Groww is down,** the last cached dates are used. If it was never reachable, nothing
  is excluded and Claude is told the calendar is unavailable.
* Index → Groww F&O underlying mapping is the `fno:` entry per instrument in
  `config/settings.yaml`.

### Shipped strategies

Ten Tradetron strategies ship with the app:

| Strategy | Index | What it does |
|---|---|---|
| BNF 15 min Supertrend | Bank Nifty | buys out-of-the-money options |
| Banknifty Supertrend 5 min | Bank Nifty | sells in-the-money options |
| BankNifty 3 min Supertrend | Bank Nifty | sells ~200-pt ITM options, 83-pt premium stop-loss |
| KC Trend ride Nifty Bank buying | Bank Nifty | directional ATM buying |
| KC Trend ride Nifty Bank selling | Bank Nifty | directional ATM selling |
| ATM Roll | NIFTY + SENSEX | expiry-day straddle selling |
| Nifty Supertrend 5 min selling | NIFTY 50 | sells ~200-pt ITM options, 38-pt premium stop-loss |
| Nifty 3 min Supertrend | NIFTY 50 | sells ~200-pt ITM options, exits on the flip |
| KC Trend ride Nifty 50 buying | NIFTY 50 | directional ATM buying |
| KC Trend ride Nifty 50 selling | NIFTY 50 | directional ATM selling |

The four KC strategies get their signals (`key1` / `key2`) from an external indicator: Renko
bricks with a 9 EMA and Supertrend(10, 3). Up = Supertrend green and price above the EMA;
down = Supertrend red and price below the EMA; exit when the Supertrend flips. This rule
is written into their Logic.

Each shipped file is added to the volume **once**:
* A fresh deployment gets all of them.
* A newly shipped strategy also appears on an existing deployment at the next deploy.
* A strategy you deleted or edited in the admin panel is never brought back or overwritten.
* When a shipped strategy is improved (e.g. the KC signal logic), copies you haven't edited
  are updated at the next deploy. After editing a file in `config/strategies/`, run
  `python tools/update_shipped_history.py` so this keeps working.

This is tracked in `strategies/.seeded.json` on the volume. The earlier generic examples
are in `config/strategies/examples/` (not loaded).

## Market data

| Provider | Key | Notes |
|---|---|---|
| **Groww** | `GROWW_API_KEY` + `GROWW_TOTP_SECRET` (TOTP key), or `GROWW_API_KEY` + `GROWW_API_SECRET` (approval key) | Best for NSE/BSE + the F&O expiry calendar. The app logs in itself (generating the 6-digit TOTP code from the secret) and again after Groww's daily 06:00 IST reset. |
| **Twelve Data** | `TWELVEDATA_API_KEY` | Backup. Free plan: 8 req/min, which is slow for many symbols. |
| **yfinance** | none | Free last resort. Unofficial and delayed. |

Check the index symbols in `config/settings.yaml` against your Groww / Twelve Data
accounts.

*Decision support only. Not financial advice.*

## Strategy research (evidence-based daily pick)

**Admin → Strategy research** (`/admin/research`) is a separate, evidence-based recommender. It runs next to
the Claude ranking above and changes nothing in it. It learns from your own stored history: TradingView
market data plus each deployed strategy's daily P&L. It then picks which of your strategies to run in the
next session, or says **"No strategy meets the current selection criteria"**. All of it is calculated on
the server; no AI API is called.

### Data and storage
* **Upload market data:** TradingView exports (CSV/Excel, 1-minute or daily) for NIFTY 50, SENSEX or
  NIFTY BANK.
  * Columns are detected by name: `time, open, high, low, close`, plus optional `Volume, VWAP, EMA, RSI,
    RSI-based MA` and Supertrend `Up Trend` / `Down Trend`.
  * Muhurat, weekend and partial sessions are flagged and kept out of the features.
* **Fetch daily candles** gets daily OHLC for all three indices through the configured data providers. This
  gives every index a regime history even without 1-minute files.
* **Upload strategy P&L:** a daily P&L table or a trade/order log (CSV/Excel). It needs a date column and a
  P&L column.
  * A charges, trades or strategy-name column is used when present.
  * DD/MM vs MM/DD dates are resolved from the data itself.
* **Originals are never modified.** Each uploaded file is stored byte-for-byte as
  `data/research/raw/<sha256>.<ext>`. Cleaned rows go to `data/research/research.db` (SQLite on the
  `/data` volume).
* **No duplicates.**
  * Re-uploading the same file is refused.
  * Overlapping files add only the new rows.
  * Conflicting values are kept unless you tick "replace".
* **Strategies and their margins** are seeded from `config/research_strategies.yaml` and editable on the
  Data tab. Margin turns P&L into return per ₹1 lakh. The cost estimate per active day comes from Tradetron's
  Cost Lab and is applied only when a P&L file has no charges column.
* **Backup / restore** (master admin) downloads or restores one zip containing the database and every
  original file.

### How the pick is made
1. **Market features at each session's close.** Returns, ATR%, realised volatility, 10-day trend
   efficiency and distance from the 20-day EMA. From 1-minute data, also VWAP/EMA position, Supertrend
   direction and flips, RSI, relative volume and last-hour moves.
2. **Regime** = trending/range-bound × high/low volatility. Thresholds are trailing medians of *earlier*
   days only. Each strategy is judged on its own index; it falls back to NIFTY 50 when that index has no
   data.
3. **Main engine** (`B_regime_shrunk`):
   * Each strategy's average net return per ₹1L in the current regime, shrunk toward its overall average
     (fewer regime days means more shrinkage).
   * Ranked on the conservative (10th-percentile) end of a bootstrap range, minus a penalty on its worst-5%
     days.
   * Strategies with too little history, or that don't trade on that weekday, are left out.
   * If no score is above zero, there is no pick.
4. **Benchmarks the main engine must beat:** best average so far, best last-60-day average, best raw
   regime average, ridge regression on the features, running everything equally, and random picks.

The recommendation for the next session is recomputed after every upload and stored with its data cutoff,
model version and full ranking. Once that day's P&L is uploaded, the actual results are filled in.
**History & validation** runs a walk-forward test: every past session is scored with data up to the
previous close only. It reports profitable-pick rate, best/top-2 hit rate, total and drawdown per ₹1L,
Sharpe, results by quarter and regime, and a p-value against random picks.

### The 8 AM pick on the participant dashboard
At **08:00 IST on every trading day** the scheduler publishes the day's pick. A trading day is a weekday
not listed under Admin → Daily WhatsApp → Market holidays.

The job does three things:
1. Fetches fresh daily candles for the indices the strategies trade.
2. Ranks the strategies for **today's** session, using only data up to the previous close.
3. Publishes the result.

What participants see in **My pick → Today's strategy**:
* Before 8 AM: when the pick will be published.
* After 8 AM: the strategy to run (or "No strategy today"), with:
  * expected net on its margin, with a range
  * the estimated chance of a profitable day
  * how many similar past days back it up
  * the reasons, and why the other strategies rank lower
  * any warnings
* On market holidays and weekends: when the next pick comes.

The AI market read stays below it as a separate method.

Rules:
* **A published pick does not change during the day.** Later uploads or recomputes do not alter what
  participants saw, and the history keeps the published version. *Publish today's pick now* on the
  admin page deliberately re-publishes.
* **If the app was down at 8 AM,** the pick is published as soon as it is back, until the 3:30 pm close.
* **If the latest stored data is older than the previous session,** the pick carries a warning that the
  market regime may be out of date.
* **Settings:** the publish time, on/off and the candle fetch are under Strategy research → Settings.

**Daily routine:** after the close, upload each strategy's P&L for the day (and the TradingView export if
you use 1-minute data). The 8 AM job does the rest.
