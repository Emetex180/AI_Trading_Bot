"""Client-facing trading-analysis platform.

The product. Everything here is **read-only**: it renders what the strategy
engine and the scanner already produced. No route in this module can start a
session, place an order, change an asset or reach MT5. The one exception is the
per-user broker *link* bookkeeping, which is a client describing their own
account and never a read of it.

Where the numbers come from
---------------------------
Two sources, and nothing is derived from anything else:

* **Persisted setups** — ``signals`` rows. Only setups the engine reached
  ``TRADE_CONFIRMED`` on and that passed the risk gate are written (see
  ``scanner.AssetScanner._on_signal``), so every card on this platform is a setup
  the engine actually stood behind.
* **Live engine state** — ``JobManager.live_state()``, which publishes the
  per-asset setup state machine and the last closed price each poll. This is how
  the platform can show "waiting for FVG retracement" for an asset that has
  produced no signal: that state exists only in the running engine, and the
  runner publishes it precisely so a UI can report it.

Nothing here invents a value to fill a panel. Where the engine has produced no
answer, the UI says so — an unread price renders as an em dash, never as zero,
and a setup's condition checklist is built from the columns that record it.

Time
----
Every instant is stored naive-UTC and rendered on the New York clock through
:mod:`app.display`, which delegates to :mod:`trading.time_utils`
(``America/New_York``, DST-aware). No offset is hard-coded anywhere: the
abbreviation shown next to a time is read from the zone database, so it reads
EDT in July and EST in January.
"""
from __future__ import annotations

import os

from flask import (Blueprint, current_app, g, jsonify, render_template,
                   request, url_for)

from trading import sessions as sess
from trading import time_utils as tu
from trading.strategy import (CISD_CONFIRMED, FVG_FOUND, INVALIDATED,
                              LIQUIDITY_PURGED, NO_SETUP, RETRACE_CONFIRMED,
                              TRADE_CONFIRMED, WAITING_FOR_FVG_RETRACE)

from .api import asset_choices
from .auth import login_required
from .display import digits_for, ny_str, price, ratio

client_bp = Blueprint("client", __name__)

# --------------------------------------------------------------------------- #
# Strategy state vocabulary
#
# The engine's constants are the source of truth; these are the words a client
# reads and the tone the UI paints them. Kept in one table so a state can never
# be labelled one way on the dashboard and another on the market page.
# --------------------------------------------------------------------------- #
#: state -> (short label, tone class, one-line explanation)
STATE_INFO: dict[str, tuple[str, str, str]] = {
    NO_SETUP: ("Watching", "is-idle",
               "No qualifying liquidity purge yet this session."),
    LIQUIDITY_PURGED: ("Liquidity purged", "is-working",
                       "A 1H candle closed beyond a liquidity level. Waiting on "
                       "the 5M CISD."),
    CISD_CONFIRMED: ("CISD confirmed", "is-working",
                     "Change in state of delivery confirmed. Waiting on the "
                     "first 1M fair value gap."),
    FVG_FOUND: ("FVG formed", "is-working",
                "The first qualifying 1M FVG is in place. Waiting for price to "
                "trade back into it."),
    WAITING_FOR_FVG_RETRACE: ("Waiting for FVG retracement", "is-working",
                              "FVG established; waiting for price to retrace "
                              "into the gap."),
    RETRACE_CONFIRMED: ("Retracement confirmed", "is-ready",
                        "Price has traded into the gap. Waiting on a candle to "
                        "close inside it."),
    TRADE_CONFIRMED: ("Trade confirmed", "is-confirmed",
                      "Full setup confirmed and risk-validated."),
    INVALIDATED: ("Invalidated", "is-dead",
                  "The setup broke its own rules and was discarded. The engine "
                  "resets cleanly from here."),
}

#: The order the model runs in — drives the progress display on a setup page.
STATE_SEQUENCE: tuple[str, ...] = (
    NO_SETUP, LIQUIDITY_PURGED, CISD_CONFIRMED, FVG_FOUND,
    RETRACE_CONFIRMED, TRADE_CONFIRMED,
)

#: Human labels for the event sources the analysis feed draws on.
FEED_SOURCES: dict[str, str] = {
    "strategy": "Strategy",
    "scanner": "Scanner",
}


def state_info(state: str | None) -> dict:
    """Label, tone and explanation for one strategy state."""
    label, tone, note = STATE_INFO.get(
        state or "", ((state or "Unknown").replace("_", " ").title(),
                      "is-idle", ""))
    return {"key": state or "", "label": label, "tone": tone, "note": note}


def session_label(key: str | None) -> str:
    """Display label for a session key (``ny_am`` -> ``NY AM``)."""
    window = sess.SESSION_INDEX.get((key or "").strip())
    return window.label if window else (key or "").replace("_", " ").upper()


def _jobs():
    return current_app.config["JOBS"]


def _cfg():
    return current_app.config["CFG"]


# --------------------------------------------------------------------------- #
# Setup view models
# --------------------------------------------------------------------------- #
def setup_conditions(s) -> list[dict]:
    """Which model conditions a persisted setup satisfied, from its own columns.

    Each entry is ``(label, met, detail)`` where ``met`` is read from the column
    that *records* that step, not inferred from the final status. A rejected
    setup can therefore show honestly which steps it did clear — the reason it
    was refused is in ``reason``, not disguised as a missing purge.

    The detail strings carry the real recorded value (which liquidity level, which
    CISD timeframe, the actual gap bounds) so the checklist is evidence rather
    than a row of ticks.
    """
    gap_formed = bool(s.fvg_direction) and s.fvg_upper > s.fvg_lower
    retraced = s.state in (RETRACE_CONFIRMED, TRADE_CONFIRMED)

    return [
        {
            "label": "1H liquidity purge",
            "met": bool(s.liquidity_type and s.purge_time_ny),
            "detail": (f"{s.liquidity_type} at {ny_str(s.purge_time_ny, '%H:%M')}"
                       + (f" (grade {s.purge_grade})" if s.purge_grade else "")
                       + f" · {ny_str(s.purge_time_ny)} NY"
                       if s.liquidity_type and s.purge_time_ny else ""),
        },
        {
            "label": f"{s.cisd_tf or '5M'} CISD",
            "met": bool(s.cisd_tf and s.cisd_confirm_time_ny),
            "detail": (f"confirmed {ny_str(s.cisd_confirm_time_ny)} NY"
                       if s.cisd_confirm_time_ny else ""),
        },
        {
            "label": "1M FVG",
            "met": gap_formed,
            "detail": (f"{s.fvg_direction} gap "
                       f"{price(s.fvg_lower, s.asset)} – {price(s.fvg_upper, s.asset)}"
                       if gap_formed else ""),
        },
        {
            "label": "FVG retracement",
            "met": retraced,
            "detail": ("price traded back into the gap" if retraced else
                       "not yet reached" if gap_formed else ""),
        },
        {
            "label": "SL from the M5 liquidity-taking candle",
            "met": bool(s.structure_extreme_price),
            "detail": (f"anchor {price(s.structure_extreme_price, s.asset)}"
                       + (f" · {ny_str(s.structure_time_ny)} NY"
                          if s.structure_time_ny else "")
                       if s.structure_extreme_price else ""),
        },
        {
            "label": "TP at the nearest liquidity pull",
            "met": bool(s.target_price and s.target_kind),
            "detail": (f"{s.target_kind} at {price(s.target_price, s.asset)}"
                       + (f" (grade {s.target_grade})" if s.target_grade else "")
                       if s.target_price and s.target_kind else ""),
        },
        {
            "label": "Risk/reward requirement",
            "met": bool(s.risk_approved),
            "detail": (f"{ratio(s.rr)} · risk {price(s.risk_points, s.asset)} "
                       f"→ reward {price(s.reward_points, s.asset)}"
                       if s.risk_approved else
                       (s.reason or "not satisfied")),
        },
    ]


def setup_card(s, *, detail: bool = False) -> dict:
    """One setup, shaped for both the template and the JSON API.

    A single builder for both surfaces so the poller and the first server-rendered
    paint can never disagree about a value.
    """
    conditions = setup_conditions(s)
    return {
        "id": s.id,
        "asset": s.asset,
        "direction": (s.direction or "").upper(),
        "is_buy": (s.direction or "").lower() == "buy",
        "session": s.session_primary or "",
        "session_label": session_label(s.session_primary),
        "session_keys": list(s.session_keys or []),
        "silver_bullet": s.silver_bullet or "",
        "macro": s.macro or "",
        "entry": s.entry,
        "sl": s.sl,
        "tp": s.tp,
        "rr": s.rr,
        "rr_label": ratio(s.rr),
        "digits": s.digits or digits_for(s.asset),
        "risk_points": s.risk_points,
        "reward_points": s.reward_points,
        "efficiency_score": s.efficiency_score,
        "status": s.status,
        "state": s.state,
        "state_info": state_info(s.state),
        "reason": s.reason or "",
        "risk_approved": bool(s.risk_approved),
        "entry_time_utc": s.entry_time_utc,
        "entry_time_ny": ny_str(s.entry_time_utc),
        "entry_time_short": ny_str(s.entry_time_utc, "%H:%M"),
        "entry_date_ny": ny_str(s.entry_time_utc, "%Y-%m-%d"),
        "created_at": s.created_at,
        "conditions": conditions if detail else conditions[:7],
        "conditions_met": sum(1 for c in conditions if c["met"]),
        "conditions_total": len(conditions),
        "ai_status": s.ai_status,
        "ai_decision": s.ai_decision,
        "ai_score": s.ai_score,
        "ai_confidence": s.ai_confidence,
        "ai_reasoning": s.ai_reasoning,
        "ai_strengths": list(s.ai_strengths or []),
        "ai_risks": list(s.ai_risks or []),
        "url": url_for("client.setup_detail", signal_id=s.id),
    }


# --------------------------------------------------------------------------- #
# Live market
# --------------------------------------------------------------------------- #
def _enabled_assets(cfg, repo) -> list[dict]:
    """The registry entries the scanner is actually running, enabled first.

    The registry is authoritative (it is what the scanner reads), so a market
    table built from anything else could show an instrument the engine never
    runs. Every entry travels, enabled or not, so a switched-off asset is
    visibly off rather than missing.
    """
    rows = asset_choices(cfg, repo)
    return sorted(rows, key=lambda a: (not a.get("enabled"), a.get("name", "")))


# --------------------------------------------------------------------------- #
# The engine's live state, across a process boundary
#
# The dashboard and the scanner are deliberately separate processes (MT5's
# Python API binds a whole process to one terminal — see
# ``deploy/install-service.ps1``), so the engine's live state cannot be read
# from this process' memory unless this process is the one scanning. It is
# published to the database by ``runner.JobManager`` instead, and read back
# here. Without that, every price would render as unknown and the header would
# claim the scanner was stopped while it was running perfectly well.
# --------------------------------------------------------------------------- #
def _parse_iso(value):
    """A persisted ISO-8601 timestamp back into a naive-UTC datetime.

    The engine-state row stores times as strings because JSON has no datetime.
    Returns ``None`` for anything unreadable rather than raising: a stale row
    from an older build must degrade to "no value", never to a broken poll.
    """
    from datetime import datetime

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _age_seconds(heartbeat) -> float | None:
    """Seconds since a heartbeat, or ``None`` when there is nothing to age.

    The same quantity the lease reader computes for a scanner in another
    process, so an in-process engine and a remote one are described by one
    measure rather than by two that drift apart.
    """
    stamp = _parse_iso(heartbeat)
    if stamp is None:
        return None
    return max(0.0, (tu.now_utc() - stamp).total_seconds())


def live_view(jobs, repo=None) -> dict:
    """The engine's live state, from whichever process is actually running it.

    Three cases, and the caller must be able to tell them apart:

    * **this process** is scanning — its own in-memory state. Unchanged, so the
      operator console's Start button behaves exactly as it always has.
    * **another process** is scanning — the persisted engine-state row, but only
      while its lease is still valid. A crashed engine stops heartbeating and
      the lease expires, so a dead scanner cannot be reported as live.
    * **nobody** is — an idle view, with every price unknown rather than zero.

    ``source`` names which of the three answered, because "no engine" and "an
    engine that has nothing to report" are different facts and the UI must not
    render them identically.
    """
    if jobs is not None and jobs.is_live_running():
        live = dict(jobs.live_state())
        live["source"] = "this process"
        live["alive"] = True
        # The heartbeat the live loop stamps on each pass, carried through
        # rather than blanked. It used to be forced to ``None`` here on the
        # reasoning that an in-process read is current by definition — which is
        # true of the *state*, but left the dashboard's "last heartbeat" tile
        # reading "none" for an engine that was demonstrably beating. The
        # instant the loop last ran is a fact the engine already records; this
        # simply stops the in-process path from throwing it away.
        live["pid"] = os.getpid()
        live["age_seconds"] = _age_seconds(live.get("heartbeat_utc"))
        return live

    lease = jobs.engine_lease() if jobs is not None else {"alive": False}
    if lease.get("alive"):
        return {
            "state": lease.get("state") or "",
            "assets": list(lease.get("assets") or []),
            "setups": dict(lease.get("setups") or {}),
            "prices": dict(lease.get("prices") or {}),
            "price_times": {k: _parse_iso(v) for k, v
                            in (lease.get("price_times") or {}).items()},
            "quotes": dict(lease.get("quotes") or {}),
            "active": bool(lease.get("active")),
            "activity": lease.get("activity") or "",
            "next_open_utc": lease.get("next_open_utc"),
            "last_candle_utc": lease.get("last_candle_utc"),
            "signals_session": int(lease.get("signals_session") or 0),
            "last_error": "",
            "source": "scanner process",
            "alive": True,
            "pid": lease.get("pid"),
            "heartbeat_utc": lease.get("heartbeat_utc"),
            "age_seconds": lease.get("age_seconds"),
        }

    # Nobody is scanning. Reported as an empty view rather than as the last
    # thing the engine said before it stopped: a frozen price reads as live.
    return {
        "state": _state_of_a_dead_lease(lease.get("state")),
        "assets": [], "setups": {}, "prices": {}, "price_times": {}, "quotes": {},
        "active": False, "activity": "", "next_open_utc": None,
        "last_candle_utc": None, "signals_session": 0,
        "last_error": lease.get("last_error") or "",
        "source": "none", "alive": False,
        "pid": lease.get("pid"), "heartbeat_utc": lease.get("heartbeat_utc"),
        "age_seconds": lease.get("age_seconds"),
    }


def _state_of_a_dead_lease(state: str | None) -> str:
    """The engine state to report for a lease nothing is heartbeating.

    A lease whose engine stopped heartbeating keeps whatever that engine last
    wrote — normally ``running`` — and reporting that next to ``alive: False``
    contradicts itself: the dashboard would hold ``scanner_running: false``
    beside ``scanner_state: "running"``, and a reader (or an API client) looking
    at the state alone would conclude the engine was up after all.

    Only the states that *assert* the engine is up are rewritten, and they are
    rewritten to ``stopped``. A lease that already says ``stopped``, ``error``
    or ``idle`` is left exactly as it is: those are not claims of liveness, and
    "it crashed with this error" is the one thing an operator needs from a dead
    engine.

    The vocabulary belongs to :mod:`runner` and is imported here rather than
    restated, so a renamed state cannot quietly stop matching. Imported lazily
    because this is a read-only UI module: nothing else in it needs the engine's
    import graph, and the import is a ``sys.modules`` lookup after the first
    call.
    """
    from runner import (LIVE_RUNNING, LIVE_STARTING, LIVE_STOPPED,
                        LIVE_STOPPING)

    if state in (LIVE_STARTING, LIVE_RUNNING, LIVE_STOPPING):
        return LIVE_STOPPED
    return state or ""


def market_rows(cfg, repo, jobs) -> list[dict]:
    """One row per registered asset, combining registry, live state and price.

    The live half is only present while a session runs; with the scanner
    stopped, every row reports ``running: False`` and an idle state rather than
    the last thing the engine happened to say before it died.

    Two prices travel per row and they mean different things: ``price`` is the
    last *closed* M1 close, which is the price the strategy itself acted on, and
    ``bid``/``ask`` are the live quote from the terminal. A reader needs both to
    see the difference between what the model decided on and what the market is
    doing now.
    """
    live = live_view(jobs, repo)
    setups = live.get("setups") or {}
    prices = live.get("prices") or {}
    price_times = live.get("price_times") or {}
    quotes = live.get("quotes") or {}
    monitored = set(live.get("assets") or [])
    running = bool(live.get("alive"))

    rows = []
    for asset in _enabled_assets(cfg, repo):
        name = asset.get("name") or ""
        state_map = setups.get(name) or {}
        buy = state_map.get("buy") or NO_SETUP
        sell = state_map.get("sell") or NO_SETUP
        digits = asset.get("digits") or digits_for(name)
        quote = quotes.get(name) or {}

        # The furthest-along direction is the one worth leading with: an asset
        # with a live sell setup and an idle buy is "working on a sell".
        lead = _lead_state(buy, sell)
        rows.append({
            "name": name,
            "symbol": asset.get("broker_symbol") or "",
            "enabled": bool(asset.get("enabled")),
            "digits": digits,
            # ``None`` when no session is running or the stream is empty, which
            # the template renders as an em dash rather than as zero.
            "price": prices.get(name),
            "price_time_ny": ny_str(price_times.get(name)) if price_times.get(name) else "",
            "monitored": name in monitored,
            "running": running,
            "states": {"buy": buy, "sell": sell},
            "buy": state_info(buy),
            "sell": state_info(sell),
            "lead": lead,
            "is_active_setup": lead["key"] not in (NO_SETUP, INVALIDATED, ""),
            # The live quote. Absent fields stay ``None`` so the template can
            # render an em dash — never a fabricated 0.00000, which would read
            # as a real quote of zero.
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "spread": quote.get("spread"),
            "spread_points": quote.get("spread_points"),
            "quote_time_ny": (ny_str(_parse_iso(quote.get("time_utc")))
                              if quote.get("time_utc") else ""),
        })
    return rows


def _quote_label(quote_row: dict, field: str, name: str) -> str:
    """A quote figure rendered at the asset's own precision, or ``""``.

    ``""`` rather than a dash so the caller decides how to mark an absent value;
    the template and the poller both use the platform's em dash for it.
    """
    value = quote_row.get(field)
    if value is None:
        return ""
    return price(value, name)


def _lead_state(buy: str, sell: str) -> dict:
    """The more advanced of the two directions' states."""
    def rank(state: str) -> int:
        try:
            return STATE_SEQUENCE.index(state)
        except ValueError:
            # INVALIDATED (and anything unknown) ranks below NO_SETUP: it is a
            # dead setup, not a progressing one.
            return -1

    return state_info(buy if rank(buy) >= rank(sell) else sell)


def platform_status(cfg, jobs, repo=None) -> dict:
    """Session, scanner and clock state for the dashboard header.

    ``scanner_running`` and ``session_active`` are deliberately separate. The
    live thread stays up around the clock and only polls inside a tradeable
    window, so "running but asleep" is a real and common state that the UI must
    be able to name — collapsing them would have the platform claim setups are
    being watched at 3am on a Sunday.

    ``scanner_running`` now means "an engine is alive", which it can be in
    another process. That distinction is the whole point of ``engine_source``:
    Flask answering a request says nothing about whether the scanner is up, and
    the header must report the engine's state rather than the web server's.
    """
    live = live_view(jobs, repo)
    now_ny = tu.now_ny()
    window = sess.get_trading_session(now_ny)
    alive = bool(live.get("alive"))
    heartbeat = live.get("heartbeat_utc")
    age = live.get("age_seconds")

    #: One word per component, from the one engine state. The scanner loop, the
    #: setup pipeline inside it and the asset watch are the same running
    #: process, so they are derived from the same two facts — is an engine alive,
    #: and is it awake — rather than being asked separately and allowed to
    #: disagree. "WAITING" is the state that matters most here: an engine that
    #: is up and correctly asleep outside its session is not "STOPPED", and
    #: saying so is the difference between "nothing is wrong" and "go restart
    #: it".
    def component(active_now: bool) -> str:
        if not alive:
            return "STOPPED"
        return "ACTIVE" if active_now else "WAITING"

    awake = bool(live.get("active"))
    return {
        "ny_time": now_ny.strftime("%H:%M:%S"),
        "ny_date": now_ny.strftime("%A %d %B %Y"),
        "ny_zone": tu.ny_zone_abbr(),
        "ny_offset_hours": tu.ny_offset_hours(),
        "session_key": window.key if window else "",
        "session_label": window.label if window else "Outside session",
        "session_window": (f"{window.start_hhmm}–{window.end_hhmm}"
                           if window else ""),
        "session_trade": window.trade if window else sess.TRADE_CLOSED,
        "session_accepts_entries": bool(window and window.trade != sess.TRADE_NO),
        # Whether a configured window covers *now*. Different from
        # ``session_accepts_entries`` above, which is about permission: the Asian
        # range is a window the engine is awake for and may not enter. Collapsing
        # them would either claim entries are possible in the Asian range or
        # report the engine as asleep while it is working.
        "session_open": window is not None,
        "session_status": "ACTIVE" if window is not None else "CLOSED",
        "scanner_state": live.get("state") or "",
        "scanner_running": alive,
        "scanner_active": awake,
        "scanner_activity": live.get("activity") or "",
        "next_open_utc": live.get("next_open_utc"),
        "next_open_ny": (ny_str(live.get("next_open_utc"))
                         if live.get("next_open_utc") else ""),
        "last_candle_utc": live.get("last_candle_utc"),
        "last_candle_ny": (ny_str(live.get("last_candle_utc"))
                           if live.get("last_candle_utc") else ""),
        "monitored_assets": list(live.get("assets") or []),
        "signals_session": live.get("signals_session") or 0,
        # ---- named components, each derived from the one engine state ------ #
        # The web server answering this page is the only thing here that is true
        # by construction rather than by reading the engine, and it is labelled
        # as such. Everything below it is read back out of the engine's own
        # state, so the dashboard cannot claim a component is up when it is not.
        "web_app_running": True,
        "backend_running": alive,
        "backend_state": live.get("state") or "",
        # The engine only writes a heartbeat from inside its MT5 session
        # (``runner.JobManager._live_worker`` runs entirely within
        # ``_mt5_session``), so a live engine *is* a connected terminal. Stated
        # as unknown — not as False — when there is no engine to ask.
        "mt5_connected": True if alive else None,
        # Live means the engine is awake *and* has actually read a candle. An
        # engine still warming up, or awake with an empty history, has no market
        # data yet and must not be reported as though it did.
        "market_data_live": bool(alive and awake
                                 and live.get("last_candle_utc") is not None),
        # The scanner loop, the setup pipeline that runs inside it and the asset
        # watch are one process, so one word each from the same two facts rather
        # than three independent readings that could disagree.
        "scanner_status": component(awake),
        "setup_scanner_status": component(awake),
        "assets_status": component(awake),
        "assets_count": len(live.get("assets") or []),
        "telegram_status": ("READY" if (cfg.telegram_enabled
                                        and cfg.telegram_bot_token
                                        and cfg.telegram_chat_id)
                            else "OFF"),
        # ---- engine identity, so "is the bot up" is answerable honestly ---- #
        # Where that state came from: this process, the scanner process, or
        # nowhere. The badge reads this so "Flask is running" can never be
        # mistaken for "the engine is running".
        "engine_alive": alive,
        "engine_source": live.get("source") or "none",
        "engine_pid": live.get("pid"),
        "engine_age_seconds": age,
        "engine_heartbeat_ny": ny_str(heartbeat) if heartbeat else "",
        # Last successful scan, in the same sense the engine uses: the close
        # time of the newest M1 candle it processed.
        "last_scan_utc": live.get("last_candle_utc"),
        "last_scan_ny": (ny_str(live.get("last_candle_utc"))
                         if live.get("last_candle_utc") else ""),
        "engine_last_error": live.get("last_error") or "",
    }


def analysis_feed(repo, jobs, limit: int = 60) -> list[dict]:
    """Recent engine decisions, newest first.

    Read from the persisted event log, which is where the strategy's own decision
    lines land (``scanner.AssetScanner._strategy_log``): purges found, CISDs
    confirmed, FVGs formed, and the reason a candidate was refused. This is the
    engine narrating itself — the platform does not generate commentary, and
    there is no model here writing prose about the market.

    Scanner-level lines (start/stop, warm-up, backfill warnings) are included
    too, because "why is nothing happening" is usually answered by one of them.
    """
    rows = repo.recent_events_by_source(
        "", limit=limit, sources=tuple(FEED_SOURCES))
    return [{
        "id": r.id,
        "time_utc": r.event_time_utc,
        "time_ny": ny_str(r.event_time_utc),
        "time_short": ny_str(r.event_time_utc, "%H:%M:%S"),
        "date_ny": ny_str(r.event_time_utc, "%Y-%m-%d"),
        "level": r.level,
        "source": r.source,
        "source_label": FEED_SOURCES.get(r.source, r.source.title()),
        "message": r.message,
    } for r in rows]


def active_setups(repo, limit: int = 60) -> list[dict]:
    """Setups the engine confirmed and the risk manager approved.

    "Active" means exactly that — see :meth:`Repository.active_setups`. Setups
    in an intermediate state (waiting on a retrace) are not here: they are not
    rows yet, and they are shown on the market page from live state instead.
    """
    return [setup_card(s) for s in repo.active_setups(limit=limit)]


# --------------------------------------------------------------------------- #
# Filters for the history page
# --------------------------------------------------------------------------- #
def _history_filters() -> dict:
    """Read the history page's filter query string.

    Dates arrive as ``YYYY-MM-DD`` on the **New York** clock, because that is the
    clock every time on the page is shown in. They are converted to the naive-UTC
    instants the column stores through :mod:`trading.time_utils`, so a filter
    "5 January" means the NY day, not the UTC one.
    """
    args = request.args
    date_from = _ny_date_bound(args.get("from"), end_of_day=False)
    date_to = _ny_date_bound(args.get("to"), end_of_day=True)
    return {
        "asset": (args.get("asset") or "").strip() or None,
        "direction": (args.get("direction") or "").strip().lower() or None,
        "session": (args.get("session") or "").strip() or None,
        "status": (args.get("status") or "").strip().upper() or None,
        "date_from": date_from,
        "date_to": date_to,
        "raw_from": (args.get("from") or "").strip(),
        "raw_to": (args.get("to") or "").strip(),
    }


def _ny_date_bound(raw, *, end_of_day: bool):
    """A ``YYYY-MM-DD`` NY date as a naive-UTC instant, or ``None``."""
    from datetime import datetime

    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return tu.ny_to_utc(parsed)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@client_bp.get("/dashboard")
@login_required
def overview():
    repo, cfg, jobs = g.repo, _cfg(), _jobs()
    status = platform_status(cfg, jobs, g.repo)
    recent = [setup_card(s) for s in repo.find_signals(limit=6)]
    return render_template(
        "client/overview.html",
        nav="overview",
        status=status,
        recent=recent,
        active=active_setups(repo, limit=4),
        feed=analysis_feed(repo, jobs, limit=8),
        counts={
            "setups_total": repo.count_signals(),
            "setups_approved": repo.count_signals("APPROVED"),
            "setups_rejected": repo.count_signals("REJECTED"),
            "assets_monitored": len(status["monitored_assets"]),
        },
    )


@client_bp.get("/market")
@login_required
def market():
    cfg, jobs = _cfg(), _jobs()
    return render_template(
        "client/market.html",
        nav="market",
        status=platform_status(cfg, jobs, g.repo),
        rows=market_rows(cfg, g.repo, jobs),
    )


@client_bp.get("/setups")
@login_required
def setups():
    cfg, jobs = _cfg(), _jobs()
    return render_template(
        "client/setups.html",
        nav="setups",
        status=platform_status(cfg, jobs, g.repo),
        setups=active_setups(g.repo, limit=100),
    )


@client_bp.get("/setups/<int:signal_id>")
@login_required
def setup_detail(signal_id: int):
    row = g.repo.get_signal(signal_id)
    if row is None:
        from flask import abort

        abort(404)
    cfg, jobs = _cfg(), _jobs()
    return render_template(
        "client/setup_detail.html",
        nav="setups",
        status=platform_status(cfg, jobs, g.repo),
        s=setup_card(row, detail=True),
        raw=row,
        sequence=[state_info(k) for k in STATE_SEQUENCE],
    )


@client_bp.get("/history")
@login_required
def history():
    repo, cfg, jobs = g.repo, _cfg(), _jobs()
    filters = _history_filters()
    rows = repo.find_signals(
        status=filters["status"], asset=filters["asset"],
        direction=filters["direction"], session=filters["session"],
        date_from=filters["date_from"], date_to=filters["date_to"],
        limit=500,
    )
    return render_template(
        "client/history.html",
        nav="history",
        status=platform_status(cfg, jobs, g.repo),
        setups=[setup_card(s) for s in rows],
        filters=filters,
        assets=_enabled_assets(cfg, repo),
        sessions=[{"key": k, "label": session_label(k)}
                  for k in repo.distinct_sessions()],
        total=len(rows),
    )


@client_bp.get("/analysis")
@login_required
def analysis():
    cfg, jobs = _cfg(), _jobs()
    return render_template(
        "client/analysis.html",
        nav="analysis",
        status=platform_status(cfg, jobs, g.repo),
        feed=analysis_feed(g.repo, jobs, limit=120),
        rows=[r for r in market_rows(cfg, g.repo, jobs) if r["enabled"]],
        # The five core windows, read from trading/sessions.py rather than
        # restated here. They are shown so a reader can tell "the engine found
        # nothing" apart from "the engine is not looking right now" — and so the
        # New York clock the page runs on is visibly the one the windows use.
        session_windows=[{
            "key": w.key, "label": w.label,
            "start_hhmm": w.start_hhmm, "end_hhmm": w.end_hhmm,
            "trade": w.trade,
        } for w in sess.CORE_SESSIONS],
    )


# --------------------------------------------------------------------------- #
# JSON API (the poller)
# --------------------------------------------------------------------------- #
@client_bp.get("/api/client/overview")
@login_required
def api_overview():
    repo, cfg, jobs = g.repo, _cfg(), _jobs()
    status = platform_status(cfg, jobs, g.repo)
    return jsonify({
        "status": status,
        "counts": {
            "setups_total": repo.count_signals(),
            "setups_approved": repo.count_signals("APPROVED"),
            "assets_monitored": len(status["monitored_assets"]),
        },
        "last_signal_id": repo.max_signal_id(),
        "signals": [setup_card(s) for s in repo.active_setups(limit=4)],
    })


@client_bp.get("/api/client/market")
@login_required
def api_market():
    cfg, jobs = _cfg(), _jobs()
    rows = market_rows(cfg, g.repo, jobs)
    return jsonify({
        "status": platform_status(cfg, jobs, g.repo),
        # Labels are rendered here rather than in the browser, so a polled cell
        # is formatted by exactly the same code that painted it on page load.
        # An absent figure is an empty string, which the poller draws as an em
        # dash — never as a zero that would read as a real quote of zero.
        "rows": [{**r,
                  "price_label": (price(r["price"], r["name"])
                                  if r["price"] is not None else ""),
                  "bid_label": _quote_label(r, "bid", r["name"]),
                  "ask_label": _quote_label(r, "ask", r["name"]),
                  "spread_label": _quote_label(r, "spread", r["name"]),
                  "spread_points_label": (str(r["spread_points"])
                                          if r["spread_points"] is not None
                                          else "")}
                 for r in rows],
    })


@client_bp.get("/api/client/setups")
@login_required
def api_setups():
    repo, cfg, jobs = g.repo, _cfg(), _jobs()
    after = request.args.get("after_id", type=int)
    if after is not None:
        # The poller's cursor: only rows newer than the highest id it has seen,
        # so a 5s poll costs one indexed comparison rather than a full listing.
        # Filtered to APPROVED because "a new setup appeared" is the only thing
        # this endpoint means; a rejected row is history, not a new setup.
        rows = [s for s in repo.signals_after_id(after, limit=50)
                if s.status == "APPROVED"]
    else:
        rows = repo.active_setups(limit=100)
    return jsonify({
        "status": platform_status(cfg, jobs, g.repo),
        "last_signal_id": repo.max_signal_id(),
        "total": repo.count_signals(),
        "setups": [setup_card(s) for s in rows],
    })


@client_bp.get("/api/client/feed")
@login_required
def api_feed():
    jobs = _jobs()
    return jsonify({
        "status": platform_status(_cfg(), jobs, g.repo),
        "feed": analysis_feed(g.repo, jobs, limit=40),
    })


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def register_client(app) -> None:
    """Attach the client platform to ``app`` (called from ``create_app``)."""
    app.register_blueprint(client_bp)

