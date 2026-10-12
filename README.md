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
pytest                          # 92 tests
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

Eight Tradetron strategies ship with the app:

| Strategy | Index | What it does |
|---|---|---|
| BNF 15 min Supertrend | Bank Nifty | buys out-of-the-money options |
| Banknifty Supertrend 5 min | Bank Nifty | sells in-the-money options |
| KC Trend ride (Bank Nifty) | Bank Nifty | directional ATM selling |
| ATM Roll | NIFTY + SENSEX | expiry-day straddle selling |
| Nifty Supertrend 5 min selling | NIFTY 50 | sells ~200-pt ITM options, 38-pt premium stop-loss |
| Nifty 3 min Supertrend | NIFTY 50 | sells ~200-pt ITM options, exits on the flip |
| KC Trend ride Nifty 50 buying | NIFTY 50 | directional ATM buying |
| KC Trend ride Nifty 50 selling | NIFTY 50 | directional ATM selling |

The three KC strategies take their signals from runtime variables set outside Tradetron's
export. Add the Keltner Channel rule to their Logic in the admin panel.

Each shipped file is added to the volume **once**:
* A fresh deployment gets all of them.
* A newly shipped strategy also appears on an existing deployment at the next deploy.
* A strategy you deleted or edited in the admin panel is never brought back or overwritten.

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
