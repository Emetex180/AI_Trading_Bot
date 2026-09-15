# AI ICT Multi-Asset Trading Platform

A **local, multi-asset** ICT (Inner Circle Trader) analysis, alert and backtesting
platform for MetaTrader 5. It scans several configurable symbols across all
sessions, detects the ICT setup on M1/M5/M15/H1, runs a deterministic risk
manager, overlays an **advisory-only** AI read, broadcasts **ALERT-ONLY**
Telegram messages, and stores everything in SQLite.

Everything is operated from a **Flask dashboard** — start and stop live sessions,
run backtests, and watch signals arrive — with equivalent CLI commands for
scripting.

> **Version 1 safety posture:** alerts and analysis only.
> `AUTO_TRADING` defaults to **false** and can only be set in `.env` — there is
> no dashboard control for it. While it is false the executor **never** calls
> MT5 `order_send()`; every approved signal is recorded as a `SKIPPED`
> execution attempt for the audit trail.

---

## The ICT setup (deterministic engine)

1. **H1 context / liquidity** — an H1 candle sweeps a known level (PDH/PDL,
   session extreme, equal high/low, swing high/low) and closes back inside
   (displacement + reclaim). The strongest purge per direction anchors an
   episode.
2. **CISD (confirmation inside a smaller displacement)** — the same level is
   re-confirmed on **M15** when the purge closed before `CISD_THRESHOLD_HOUR_NY`
   (09:00 NY) or on **M5** at/after it. *Configurable* `cisd_mode` definition —
   see `trading/cisd.py` — never silently invented.
3. **M1 FVG** — a 3-candle fair-value gap forms after confirmation.
4. **Retrace entry** — price retraces into the FVG and closes back through its
   midpoint (bullish reclaim / bearish rejection).

Every signal records its sessions, whether it is inside a **Silver Bullet**
window, the active **macro window**, the purged liquidity level and the
structure extreme. Signals are only emitted on **closed** candles — no
lookahead. Setups below `MIN_RR` (1.5) are rejected; the TP targets the nearest
known draw-on-liquidity beyond entry.

All sessions are analysed (Asian, London, NY Premarket, NY AM, London Close, NY
PM) even when only `VALID_ENTRY_SESSIONS` are tradeable/alerted. Note the
allow-list accepts only real session keys — `asian_range`, `london_open`,
`ny_premarket`, `ny_am`, `ny_lunch`, `london_close`, `ny_pm`. Anything else is
silently ignored, so a typo there quietly narrows what you trade.

## Time handling

The project runs on the **America/New_York** clock, resolved through
`zoneinfo`, so daylight-saving is handled for real: UTC−5 (EST) in winter,
UTC−4 (EDT) in summer. **No offset is hard-coded anywhere** — a module-level
`-4` is exactly the bug this design exists to prevent, and there is a test that
would catch it (the same simulated week is walked in January and July and the NY
wall-clock session spans must come out identical).

MT5 returns candle times in the **broker server** clock, which is not UTC and is
not New York. Only `trading/time_utils.py` converts between the three clocks, and
the broker's offset ahead of UTC comes from `MT5_SERVER_UTC_OFFSET`. A wrong
value there shifts every session boundary, so verify it against your broker.
All datetimes are naive UTC internally; `trading/time_utils.py` is the single
conversion point.

## Safety rules (hard constraints)

- `AUTO_TRADING=false` by default ⇒ `order_send()` is never called
  (`trading/executor.py` is the **only** file that may call it).
- **The dashboard cannot enable auto-trading.** It can start/stop the live
  scanner and launch backtests, but `AUTO_TRADING` is read from `.env` and shown
  read-only; no HTTP route can change it.
- AI is a read-only overlay: it can score/comment a deterministic, risk-approved
  signal but **cannot** change entry/SL/TP/direction, override the risk manager,
  bypass session rules, or resurrect a rejected setup. On AI failure the signal
  is marked `AI_UNAVAILABLE`.
- Ambiguous ICT definitions are **configurable and documented**, not invented.
- Symbols come from `assets.json` only — no symbol is hard-coded in strategy code.
- **Position size is derived from the broker's own contract specification**
  (`trading/symbol_spec.py`): contract size, tick size/value and volume
  min/step/max are read per symbol from MT5, so FX (1 lot = 100,000 units),
  metals (1 lot = 100 oz) and indices (1 lot = 1 contract) all size correctly
  from the same code. With no spec available the original index assumption is
  kept rather than guessing a contract size.
- The live scanner runs in a **background thread** of the dashboard process, and
  the MT5 reloader is force-disabled so a session can never be started twice.

## Instruments

`assets.json` is the single place instruments live. It ships a broad watchlist —
FX majors and JPY crosses, gold and silver, five indices and two crypto pairs —
with **USTEC enabled and the rest disabled**, so switching one on is a dashboard
toggle rather than a code edit:

```powershell
python run.py assets --enable-all     # opt the whole watchlist in
python run.py assets --disable-all    # back to nothing but the registry state
python run.py backtest --asset EURUSD # one asset instead of all enabled
```

Registry entries hold **portable base names** (`XAUUSD`, `USTEC`). With
`SYMBOL_AUTO_RESOLVE=true` (default) the bot asks the connected terminal for the
broker's actual spelling — `XAUUSD` → `XAUUSDm`, `USTEC` → `US100` — and selects
it in Market Watch, which MT5 requires before history can be fetched. The same
registry therefore works across brokers instead of being tied to one account's
suffixes. Set it to `false` to require exact symbol names.

## Layout

```
config.py            Central env-driven settings (no MT5 I/O).
runner.py            Shared job manager: live-session + backtest threads.
scanner.py           Per-asset live scanner (injectable, testable).
run.py               CLI: scan | backtest | web | assets | smoke
assets.json          Strategy-name → broker-symbol registry (user-editable).
trading/             Bars/stream, sessions, liquidity, CISD, FVG, strategy,
                     risk manager, signal engine, executor, MT5 client, market
                     data, symbol resolver, contract specs, asset preparation.
backtesting/         Event-driven replay of the same strategy modules + SL/TP sim.
ai/                  Advisory LLM overlay (never touches risk geometry).
notifications/       Telegram ALERT-ONLY broadcaster.
database/            SQLAlchemy models + repository (SQLite now, PG-ready).
app/                 Flask dashboard + control API (Bootstrap + Chart.js).
tests/               pytest suite, no MT5 required.
```

## Setup

Requires Python 3.12 with the installed MetaTrader5 package.

```powershell
pip install -r requirements.txt
# .env already exists locally (git-ignored). Edit it to set:
#   MT5_TERMINAL_PATH     - your terminal64.exe
#   MT5_SERVER_UTC_OFFSET - verify against your broker (see note above)
#   TELEGRAM_*            - see "Telegram setup" below
```

Edit `assets.json` to map each strategy name to the broker symbol (no code
changes to add/remove an instrument). With `SYMBOL_AUTO_RESOLVE=true` the base
name is enough — the broker's exact spelling is discovered at session start.

## Run

The dashboard is the primary interface. Start it, then use the **Live session**
card to run a session and the **Backtests** page to run backtests.

```powershell
python run.py web           # dashboard + control panel at http://127.0.0.1:5000
python run.py assets        # list the registry
python run.py assets --enable-all   # enable the whole watchlist
python run.py smoke         # quick no-MT5 self-check
python -m pytest -q         # run the full test suite
```

The same jobs are available from the CLI (useful for scripting or running
headless — both paths drive the identical code in `runner.py`):

```powershell
python run.py scan          # live scanner; blocks until Ctrl+C
python run.py backtest      # historical backtest per enabled asset
```

Dashboard pages: `/` (overview + live controls), `/signals`, `/signals/<id>`
(setup + AI + execution trail), `/backtests` (run form + results),
`/backtests/<id>` (equity curve + trade list).

Control API: `POST /api/live/start`, `POST /api/live/stop`,
`POST /api/backtest/run`, `GET /api/status`. State-changing routes reject
cross-origin callers.

> **Keep the dashboard process alive** for as long as you want to be scanning —
> closing the browser tab is fine, but stopping `python run.py web` ends the
> session. Only one live session can run at a time; a backtest requested during
> a session is queued and starts when you stop it.

### When the bot is awake

The live loop runs around the clock but only **works** inside a session on a
trading day, and sleeps the rest of the time. By default that means
Monday–Friday, and only these windows on the New York clock:

| Window | NY time | Mode | Bot |
|---|---|---|---|
| London | 02:00 – 05:00 | yes | scanning |
| NY Premarket | 07:00 – 09:30 | conditional | scanning |
| NY AM | 09:30 – 11:30 | yes | scanning |
| London Close | 10:00 – 12:00 | conditional | scanning |
| NY PM | 13:30 – 16:00 | yes | scanning |
| Asian range | 20:00 – 00:00 | no | **awake, observing only** |
| NY Lunch | 11:30 – 13:30 | no | asleep |

The Asian range is the one no-trade window the bot stays **awake** for. Nothing
can be entered in it — that is a hard block — but the high and low it builds are
the liquidity the London session trades against, so the bot watches it form.
Awake there is not permission: the entry path refuses the window independently
of the gate, so the only thing being collected is the level. Every other
no-trade window (NY Lunch) is slept through.

Note that London Close sits *inside* NY Lunch between 11:30 and 12:00; the
no-trade window wins, so those 30 minutes are slept through.

While asleep the loop polls nothing, reads no account, and touches MT5 not at
all. It does **not** disconnect: the terminal session stays open across the
sleep, and on waking it backfills every M1 candle it missed before resuming, so
the stream the model reads has no hole in it. That backfill is also what makes
it safe to sleep through the Asian range's neighbours — every level is rebuilt
from closed candles, not from having watched them arrive.

Two settings control this (`SESSION_GATE_ENABLED`, `TRADING_DAYS` — see
[Configuration knobs](#configuration-knobs-env)). The dashboard's Live session
card shows which of the two states it is in and when it will next wake.

Every transition is logged in one place, on the session clock:

```
[SESSION] NY PM active — Strategy scanner ON
[SESSION] Outside trading window — Scanner idle
[SESSION] Asian session active — Building liquidity
[SESSION] NY Lunch — New signals blocked
[SESSION] Saturday — Weekend mode
```

There is a second, independent check in the strategy itself: an entry whose
candle falls on a non-trading day is refused with a `market_closed_weekend`
reason in the log. That is defence in depth — a hand-run `python run.py scan` on
a Saturday, or a host with a wrong clock, still cannot trade a closed market.
Session restrictions apply to **new** signals only; nothing closes an existing
position merely because its session ended.

## Deploying to AWS

> **The scanner must run on Windows.** The `MetaTrader5` package ships
> Windows-only wheels because it drives the MT5 terminal through its DLL. It
> cannot run on Lambda, ECS/Fargate or Linux EC2 — there is no Linux build to
> install. The scanner needs a **Windows EC2 instance** with the MT5 terminal
> installed, logged in, and "Algo Trading" enabled in the terminal toolbar.
>
> The dashboard (`python run.py web`) reads only the database and touches no
> MT5, so it can run anywhere. Running both on the one Windows instance is the
> simplest arrangement.

**On the instance**

1. Install MetaTrader 5, log in to the broker account, and leave the terminal
   running. The bot connects to a *running* terminal; it does not start one.
2. Confirm "Algo Trading" is enabled — the dashboard reads the terminal's own
   switch and will explain a rejection that `AUTO_TRADING` alone does not.
3. Point `MT5_TERMINAL_PATH` at `terminal64.exe`, and set
   `MT5_SERVER_UTC_OFFSET` to your broker's offset ahead of UTC (see
   [Time handling](#time-handling); it is **not** assumed to be New York time).
4. Run the scanner: `python run.py scan`. To keep it up across logoffs, register
   it as a Windows service or a Task Scheduler task with *Run whether user is
   logged on or not*.

**The instance can be stopped when the market is shut.** The gate makes this
safe in both directions: stop it on Friday evening and nothing is missed, and if
you forget, the bot sleeps on its own rather than scanning a closed market. On
restart it warms up from broker history before it does anything — that is what
`WARMUP_M1_BARS` (default 5000, about 3.5 days of M1) is for, and it comfortably
covers a weekend.

**Exposing the dashboard.** Set `FLASK_HOST=0.0.0.0` and `FLASK_PORT`, then open
that port in the security group. **The dashboard has no authentication** — it can
start and stop sessions and flip the auto-trading switch — so restrict the
inbound rule to your own IP rather than `0.0.0.0/0`. The reloader is already
pinned off (`run.py` passes `use_reloader=False`) because a second process would
warm up its own engine and broadcast duplicate Telegram alerts.

**One database.** The scanner and the dashboard must resolve the same
`DATABASE_URL`, or the dashboard will show an empty bot while the scanner works
happily against a different file. The default is `data/trading.db` relative to
the project root.

Telegram alerts are the intended channel for a headless instance — with the
dashboard unreachable, they are how a setup reaches you.

## Telegram setup

Telegram is the alert channel. Without it, signals are still detected and stored
but nothing reaches your phone. Setup is two values in `.env`:

1. **Get a bot token.** In Telegram, message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts, and copy the token it returns
   (looks like `123456789:AAE...`). Put it in `TELEGRAM_BOT_TOKEN`.
2. **Get your chat id.** Send any message to your new bot from your own account,
   then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `result[0].message.chat.id`. Put it in `TELEGRAM_CHAT_ID`.
3. Set `TELEGRAM_ENABLED=true` and restart the dashboard.

The dashboard warns on the **Live session** card while Telegram is unconfigured,
so a session never runs silently.

Telegram is **ALERT ONLY** — every message carries the `ALERT ONLY` suffix and is
a one-way broadcast; it never instructs execution. AI reads an OpenAI-compatible
chat endpoint; when disabled or unreachable the signal keeps its deterministic
status and is marked `AI_DISABLED` / `AI_UNAVAILABLE`.

## Configuration knobs (`.env`)

`MT5_TERMINAL_PATH`, `MT5_SERVER_UTC_OFFSET`, `AUTO_TRADING`, `MIN_RR`,
`TARGET_RR_RANGE`, `RISK_PERCENT`, `SL_BUFFER_ATR`, `VALID_ENTRY_SESSIONS`,
`CISD_THRESHOLD_HOUR_NY`, `SCANNER_POLL_INTERVAL_MS`, `BACKTEST_M1_BARS`,
`WARMUP_M1_BARS`, `TELEGRAM_*`, `AI_ENABLED`/`LLM_*`, `FLASK_*`. Per-asset
overrides can be added to an asset's `overrides` object in `assets.json`.

Session and activity control: `ENFORCE_SESSIONS` (whether sessions gate entries
at all) and, separately, `SESSION_GATE_ENABLED` / `TRADING_DAYS` (when the live
loop is awake). The ICT model adds `CISD_TIMEFRAME`, `SL_MIN_OFFSET_TICKS`,
`TP_LIQUIDITY_OFFSET_ATR`/`_TICKS`, `MIN_TP_LIQUIDITY_GRADE`,
`CONDITIONAL_MIN_LIQUIDITY_GRADE`, `MAX_CISD_CANDLES`, `FVG_WAIT_M1`,
`RETRACE_WAIT_M1`, `FVG_MIN_ATR_FRAC`, `SWING_LOOKBACK`, `SESSION_LOOKBACK_DAYS`,
`MAX_SIGNALS_PER_SESSION`, `INVALIDATE_ON_NO_TRADE_SESSION` and the
`EFFICIENCY_*` weights. `.env.example` documents each one with its default.
#   A I _ T r a d i n g _ B o t  
 