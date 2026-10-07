"""Model 2's trading-session table (New York clock).

Deliberately a **separate table** from :mod:`trading.sessions`. Model 2's
windows are not Model 1's, and the two must not be able to influence one
another:

===========  ==================  ==================
Session      Model 2 (this file) Model 1 (sessions)
===========  ==================  ==================
Asian        20:00 - 23:59       19:00 - 24:00
London       01:00 - 05:59       02:00 - 05:00
NY AM        07:00 - 10:59       07:00 - 11:00
Lunch        11:00 - 12:59       (no window)
NY PM        13:00 - 16:59       13:00 - 15:00 + 15:00 - 16:00
===========  ==================  ==================

Every window is New York **local clock** time, resolved through
:mod:`trading.time_utils` (``America/New_York``), so 07:00 means 7 AM in New
York whether the zone is on EDT (UTC-4) or EST (UTC-5). No fixed UTC offset is
used anywhere, and no new timezone machinery is introduced — this module only
names windows and asks the shared :class:`trading.sessions.Window` whether a
minute falls inside one.

The spec states each window with an **inclusive** last minute ("20:00 - 23:59").
Minutes-of-day are half-open here (``start <= m < end``), exactly as in
:mod:`trading.sessions`, so the spec's ``23:59`` is expressed as an exclusive
``24:00`` — the same interval, spelled in the representation the shared
:class:`Window` understands. The conversions live in :data:`_SPEC` so the
spec's own wording stays visible next to the value it produces.
"""
from __future__ import annotations

from datetime import date, datetime, time as _time, timedelta

from . import time_utils as tu
from .sessions import (DAY_MINUTES, OUTSIDE_SESSION_REASON, WEEKEND_REASON,
                       Window, hm_to_minutes, is_trading_day)

# --------------------------------------------------------------------------- #
# Definitions (the spec's five windows, verbatim)
# --------------------------------------------------------------------------- #
#: ``(key, label, first minute, last minute)`` — the bounds exactly as the
#: specification writes them, inclusive at both ends. The exclusive end used by
#: :class:`Window` is derived one minute past ``last``.
_SPEC: tuple[tuple[str, str, str, str], ...] = (
    ("m2_asian", "Asian", "20:00", "23:59"),
    ("m2_london", "London", "01:00", "05:59"),
    ("m2_ny_am", "NY AM", "07:00", "10:59"),
    ("m2_lunch", "Lunch", "11:00", "12:59"),
    ("m2_ny_pm", "NY PM", "13:00", "16:59"),
)


def _inclusive_window(key: str, label: str, start_hm: str, last_hm: str) -> Window:
    """A :class:`Window` from the spec's inclusive ``start``-``last`` bounds."""
    start = hm_to_minutes(start_hm)
    end = hm_to_minutes(last_hm) + 1
    return Window(key=key, label=label, start=start, end=end)


#: The five Model 2 sessions, in clock order. Every one of them permits an
#: entry — unlike Model 1, Model 2 has no no-trade window; the only thing that
#: blocks an entry is being *outside* all five.
MODEL_2_SESSIONS: tuple[Window, ...] = tuple(
    _inclusive_window(*spec) for spec in _SPEC
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
    an entry. Every Model 2 window ends on the same NY day it starts, including
    the Asian one (which the spec closes at 23:59, i.e. before midnight), so no
    window needs the wrap-around handling a spanning window would.
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
# Model 2's windows are not Model 1's, so the live loop's sleep/wake gate has to
# consult this table as well or it would sleep straight through part of a Model 2
# session. The two functions below are the Model 2 counterparts of
# :func:`trading.sessions.session_activity` and
# :func:`trading.sessions.next_activity_start_utc`, and follow them for the same
# reason: the calendar comes first, and the "next open" search steps the **UTC**
# timeline a minute at a time rather than adding minutes to a naive NY clock —
# wall-clock arithmetic silently skips or repeats an hour across a DST
# transition, and would wake the bot an hour late twice a year.
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
