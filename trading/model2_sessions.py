"""Model 2's trading-session table (New York clock).

Model 2's windows are the **bot's** windows. Since the session schedule was
centralized, the times live in exactly one place — :data:`trading.sessions.CORE_SESSIONS`
— and this module no longer states them a second time. What it keeps is Model 2's
own *key namespace*:

===========  ====================  ==================
Session      Model 2 key           Shared key
===========  ====================  ==================
Asian        m2_asian              asian_range
London       m2_london             london_open
NY AM        m2_ny_am              ny_am
Lunch        m2_lunch              ny_lunch
NY PM        m2_ny_pm              ny_pm
===========  ====================  ==================

The two names exist because Model 2's keys are persisted — a stored setup records
``m2_london`` as the session it purged in, and :data:`MODEL_2_SESSION_INDEX` has
to keep resolving it. The *windows* are shared; the names are not, and
:func:`trading.sessions.normalize_session_keys` is the bridge for callers that
speak the shared names.

Every window is New York **local clock** time, resolved through
:mod:`trading.time_utils` (``America/New_York``), so 7:00 AM means 7 AM in New
York whether the zone is on EDT (UTC-4) or EST (UTC-5). No fixed UTC offset is
used anywhere, and no new timezone machinery is introduced — this module only
names windows and asks the shared :class:`trading.sessions.Window` whether a
minute falls inside one.

Because the times are read from the shared table rather than restated, a window
cannot be moved for one model without moving it for all of them, which is the
property the centralization is for. The operator's schedule, as stored (end
exclusive, half-open) and as stated (12-hour, last minute inclusive):

    Asian      20:00 - 24:00    8:00 PM - 11:59 PM
    London     01:00 - 06:00    1:00 AM -  5:59 AM
    NY AM      07:00 - 11:00    7:00 AM - 10:59 AM
    Lunch      11:00 - 13:00   11:00 AM - 12:59 PM
    NY PM      13:00 - 17:00    1:00 PM -  4:59 PM
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time as _time, timedelta

from . import time_utils as tu
from .sessions import (DAY_MINUTES, OUTSIDE_SESSION_REASON, WEEKEND_REASON,
                       SESSION_INDEX, Window, is_trading_day)

# --------------------------------------------------------------------------- #
# Definitions
#
# The times and labels are *borrowed*, not restated: each window below is the
# shared window under Model 2's own key. `replace` copies start/end/label/trade
# and swaps only the key, so there is no arithmetic here that could drift from
# the shared table.
# --------------------------------------------------------------------------- #
#: ``(shared key, Model 2 key)`` — the whole of the two tables' relationship.
_KEY_MAP: tuple[tuple[str, str], ...] = (
    ("asian_range", "m2_asian"),
    ("london_open", "m2_london"),
    ("ny_am", "m2_ny_am"),
    ("ny_lunch", "m2_lunch"),
    ("ny_pm", "m2_ny_pm"),
)

#: The same mapping the other way round, for lookups.
_M2_KEY_FOR_SHARED: dict[str, str] = dict(_KEY_MAP)


def _model_2_window(shared_key: str) -> Window:
    """The shared window, re-keyed for Model 2.

    Raises ``KeyError`` — loudly, at import — if the shared table ever loses a
    window Model 2 names. A silent skip would leave the model scanning a
    five-session schedule with four sessions in it.
    """
    return replace(SESSION_INDEX[shared_key], key=_M2_KEY_FOR_SHARED[shared_key])


#: The five Model 2 sessions, in clock order. Every one of them permits an
#: entry — Model 2 has no no-trade window; the only thing that blocks an entry
#: is being *outside* all five.
MODEL_2_SESSIONS: tuple[Window, ...] = tuple(
    _model_2_window(shared_key) for shared_key, _ in _KEY_MAP
)

MODEL_2_SESSION_INDEX: dict[str, Window] = {w.key: w for w in MODEL_2_SESSIONS}

#: Session keys in clock order, for stable iteration.
MODEL_2_SESSION_KEYS: tuple[str, ...] = tuple(w.key for w in MODEL_2_SESSIONS)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def active_sessions(minute: int) -> list[Window]:
    """Every Model 2 session containing a NY minute-of-day.

    At most one can match: the five windows are disjoint (the gaps between them
    are the hours outside every session). Expressed as a list anyway so the
    contract matches :func:`trading.sessions.active_core_sessions` and a caller
    never has to know about the disjointness.
    """
    return [w for w in MODEL_2_SESSIONS if w.contains(minute)]


def session_at(ny_dt: datetime) -> Window | None:
    """The Model 2 session in effect at a NY instant, or ``None`` outside all.

    Takes an already-normalized NY datetime — never a broker or UTC one — for
    the same reason :func:`trading.sessions.get_trading_session` does: the
    conversion must have happened first, in :mod:`trading.time_utils`.
    """
    matches = active_sessions(ny_dt.hour * 60 + ny_dt.minute)
    return matches[0] if matches else None


def session_key_at(ny_dt: datetime) -> str:
    """The Model 2 session key at a NY instant, or ``""`` when outside all."""
    window = session_at(ny_dt)
    return window.key if window else ""


def session_bounds_ny(day: date, window: Window) -> tuple[datetime, datetime]:
    """``(open, close)`` NY instants of one session on one NY calendar day.

    ``close`` is exclusive and is the instant the session stops being valid for
    an entry. Every window in the shared table ends on the same NY day it starts
    — the Asian one closes at the day boundary, ``24:00``, rather than crossing
    midnight — so no window needs the wrap-around handling a spanning window
    would.
    """
    midnight = datetime.combine(day, _time(0, 0))
    return (midnight + timedelta(minutes=window.start),
            midnight + timedelta(minutes=window.end))


def session_closed_by(window: Window, day: date, as_of_ny: datetime) -> bool:
    """Has this session's instance on ``day`` finished forming by ``as_of_ny``?

    The causality gate for the whole model: a session's high/low may only be
    used once the session has actually ended. Asking this question rather than
    comparing dates is what keeps a partially-formed Asian range out of the
    liquidity pool at, say, 21:00 NY — the window is open, its extremes are
    still moving, and using them would be lookahead.
    """
    return session_bounds_ny(day, window)[1] <= as_of_ny


# --------------------------------------------------------------------------- #
# Activity
#
# These are now belt-and-braces. The shared table is Model 2's schedule, so
# :func:`trading.sessions.session_activity` already answers correctly for every
# Model 2 window and the live loop no longer needs a second opinion to avoid
# sleeping through one. They are kept because they are the model's own contract
# — `awake_at` is Model 2's statement of when it may act, and reading it does
# not depend on the shared table continuing to match — and because removing them
# would change the live loop's structure rather than its behaviour.
#
# Both follow :func:`trading.sessions.session_activity` for the same reason: the
# calendar comes first, and the "next open" search steps the **UTC** timeline a
# minute at a time rather than adding minutes to a naive NY clock — wall-clock
# arithmetic silently skips or repeats an hour across a DST transition, and
# would wake the bot an hour late twice a year.
# --------------------------------------------------------------------------- #
#: Prefix on the awake reason. The live loop renders the reason into its own
#: ``[SESSION]`` line, so this is what tells an operator the bot is awake for
#: Model 2 rather than for one of Model 1's windows.
ACTIVITY_PREFIX = "Model 2"


def awake_at(ny_dt: datetime,
             days: frozenset[int] | tuple[int, ...] | None = None
             ) -> tuple[bool, str]:
    """Should the bot be awake for Model 2 at this NY instant, and why?

    ``(active, reason)``, mirroring
    :func:`trading.sessions.session_activity`: when active the reason names the
    session, so the log can say *why* the bot woke; when not it is
    :data:`~trading.sessions.WEEKEND_REASON` or
    :data:`~trading.sessions.OUTSIDE_SESSION_REASON`.

    Note this is only about *being awake*. Being inside a Model 2 session is not
    by itself permission to enter — an entry additionally needs a purge and a
    break of the purge candle in that session.
    """
    if not is_trading_day(ny_dt, days):
        return False, WEEKEND_REASON
    window = session_at(ny_dt)
    if window is None:
        return False, OUTSIDE_SESSION_REASON
    return True, f"{ACTIVITY_PREFIX} · {window.label}"


def next_session_start_utc(ny_dt: datetime,
                           days: frozenset[int] | tuple[int, ...] | None = None,
                           horizon_days: int = 7) -> datetime | None:
    """First UTC instant at/after ``ny_dt`` inside a Model 2 session.

    The Model 2 counterpart of
    :func:`trading.sessions.next_activity_start_utc` — same UTC minute walk, same
    horizon, same naive-UTC return — so the live loop can take the earlier of the
    two models' next wake-ups without inventing a second kind of time arithmetic.
    ``None`` means no Model 2 session opens within the horizon.
    """
    start = tu.ny_to_utc(ny_dt)
    for step in range(1, horizon_days * DAY_MINUTES + 1):
        utc_dt = start + timedelta(minutes=step)
        active, _ = awake_at(tu.utc_to_ny(utc_dt), days)
        if active:
            return utc_dt
    return None
