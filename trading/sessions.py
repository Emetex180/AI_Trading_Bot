"""ICT sessions, Silver Bullet windows and macro windows.

All windows are expressed on the project's **America/New_York** clock and are
resolved against a *minute of the NY day* so this module has no timezone
knowledge of its own — conversion is centralized in :mod:`trading.time_utils`.

The trading calendar
--------------------
A minute of the day says nothing about *which* day it is. ``02:00`` is the
London open on a Wednesday and also on a Saturday, and a minute-of-day table
alone cannot tell them apart, so every window above would report as tradeable
over a weekend. :func:`is_trading_day` supplies the missing calendar dimension,
and :func:`session_activity` is the single question the live loop asks: *should
the bot be awake right now, and why?* See :func:`tradeable_minute` for how the
window overlap rules interact with a session allow-list, and :func:`wake_minute`
for the one asymmetry between them: a window can be worth *watching* without
being tradeable. The Asian range is a no-trade block the bot nonetheless stays
awake for, because the level it builds is what the London open sweeps.

Core sessions (the master session model)
----------------------------------------
Every window below is New York **local clock** time, resolved through
:mod:`trading.time_utils` (``America/New_York``), so 07:00 means 7 AM in New York
whether the zone is on EDT (UTC-4) or EST (UTC-5):

    asian_range     19:00 - 00:00   trade: NO           (observes the Asian range)
    london_open     02:00 - 05:00   trade: YES
    ny_am           07:00 - 11:00   trade: YES
    ny_pm           13:00 - 15:00   trade: YES
    power_hour      15:00 - 16:00   trade: YES

Hours outside every window are :data:`TRADE_CLOSED`: the bot is awake for none
of them and no entry can be taken in one.

The Asian window ends *at* midnight rather than crossing it. That is a
deliberate representation, not an oversight: minutes-of-day are counted
``00:00 = 0`` through ``24:00 = 1440``, so ``19:00 - 00:00`` is the ordinary
half-open interval ``[1140, 1440)`` and the midnight boundary is a real,
unambiguous endpoint of the *same* NY day. A window that genuinely wraps (say
``23:00 - 02:00``) has ``start > end`` and :meth:`Window.contains` handles that
case too — see :attr:`Window.spans_midnight` — so the two shapes can never be
confused for one another.

Silver Bullet windows:

    london_sb     03:00 - 04:00
    ny_am_sb      10:00 - 11:00
    ny_pm_sb      14:00 - 15:00

Macro windows (each ~20 minutes):

    pre_ny_open       08:50 - 09:10
    ny_open           09:50 - 10:10
    late_morning      10:50 - 11:10
    pm_session        11:50 - 12:10
    pm_continuation   13:50 - 14:10
    pm_close          15:50 - 16:10

Silver Bullet windows and macro windows are *informational only* — they are
reported on the signal (and in backtest breakdowns) but they never gate an
entry. The trading model implements no macro logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import time_utils as tu

# Seconds in a day as the max minute marker (exclusive end of 00:00-windows).
DAY_MINUTES = 24 * 60

# --------------------------------------------------------------------------- #
# Trade modes
# --------------------------------------------------------------------------- #
TRADE_YES = "yes"                  # entries allowed
TRADE_NO = "no"                    # no new trades (range-building / lunch)
TRADE_CONDITIONAL = "conditional"  # allowed only when the setup clears the extra bar
TRADE_CLOSED = "closed"            # no session is active at all


def hm_to_minutes(hhmm: str) -> int:
    """Convert 'HH:MM' (or 'HH:MM:SS') into minutes since midnight."""
    parts = hhmm.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    s = int(parts[2]) if len(parts) > 2 else 0
    if h == 24 and m == 0 and s == 0:  # "24:00" == end of day boundary
        return DAY_MINUTES
    return h * 60 + m + s // 60


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    start: int  # minute of NY day, inclusive
    end: int    # minute of NY day, exclusive
    trade: str = TRADE_YES  # trade mode for this window

    @property
    def spans_midnight(self) -> bool:
        """Does this window wrap past the NY day boundary?

        True only for a genuinely wrapped window (``23:00 - 02:00``, where
        ``start > end``). The Asian session is **not** one of these: it ends at
        ``24:00``, which is the boundary itself, so it stays a plain interval.
        Kept explicit so the two shapes are never conflated.
        """
        return self.end <= self.start

    def contains(self, minute: int) -> bool:
        """Is ``minute`` (0..1439) inside this window?"""
        if self.spans_midnight:
            # Wrapped window: inside either the tail or the head of the day.
            return minute >= self.start or minute < self.end
        return self.start <= minute < self.end

    @property
    def start_hhmm(self) -> str:
        return f"{self.start // 60:02d}:{self.start % 60:02d}"

    @property
    def end_hhmm(self) -> str:
        return "24:00" if self.end == DAY_MINUTES else f"{self.end // 60:02d}:{self.end % 60:02d}"


def _window(key: str, label: str, start_hm: str, end_hm: str,
            trade: str = TRADE_YES) -> Window:
    return Window(key=key, label=label, start=hm_to_minutes(start_hm),
                  end=hm_to_minutes(end_hm), trade=trade)


# --------------------------------------------------------------------------- #
# Definitions (single source of truth)
# --------------------------------------------------------------------------- #
#: The operator's five ICT sessions, in New York local clock time. The order is
#: also the documented tie-break used by :func:`primary_session` for equal-width
#: overlaps — it is chosen so the session *label* matches the spec at every hour.
CORE_SESSIONS: tuple[Window, ...] = (
    _window("asian_range", "Asian", "19:00", "24:00", TRADE_NO),
    _window("london_open", "London", "02:00", "05:00", TRADE_YES),
    _window("ny_am", "NY AM", "07:00", "11:00", TRADE_YES),
    _window("ny_pm", "NY PM", "13:00", "15:00", TRADE_YES),
    _window("power_hour", "Power Hour", "15:00", "16:00", TRADE_YES),
)

#: Retired window keys -> the session that replaced them, so an existing
#: ``VALID_ENTRY_SESSIONS`` written against the old seven-window table keeps
#: working instead of silently allowing nothing. ``None`` means the window has no
#: successor and is dropped — NY Lunch was a no-trade block, so an operator who
#: listed it was excluding time, not selecting a session.
LEGACY_SESSION_ALIASES: dict[str, str | None] = {
    "ny_premarket": "ny_am",   # 07:00-09:30 is now part of NY AM
    "london_close": "ny_am",   # 10:00-12:00 overlapped NY AM
    "ny_lunch": None,          # no successor: it was a NO-trade window
}


def normalize_session_keys(keys) -> list[str]:
    """Map retired session keys onto their current equivalents.

    Accepts any iterable of keys and returns the resolved list, dropping unknown
    keys and de-duplicating while preserving order — so an allow-list both
    ``ny_premarket`` and ``ny_am`` resolve to a single ``ny_am``.
    """
    out: list[str] = []
    for key in keys or ():
        resolved = LEGACY_SESSION_ALIASES.get(key, key)
        if resolved and resolved not in out:
            out.append(resolved)
    return out

SILVER_BULLET_WINDOWS: tuple[Window, ...] = (
    _window("london_sb", "London SB", "03:00", "04:00"),
    _window("ny_am_sb", "NY AM SB", "10:00", "11:00"),
    _window("ny_pm_sb", "NY PM SB", "14:00", "15:00"),
)

MACRO_WINDOWS: tuple[Window, ...] = (
    _window("pre_ny_open", "Pre-NY Open", "08:50", "09:10"),
    _window("ny_open", "NY Open", "09:50", "10:10"),
    _window("late_morning", "Late Morning", "10:50", "11:10"),
    _window("pm_session", "PM Session", "11:50", "12:10"),
    _window("pm_continuation", "PM Continuation", "13:50", "14:10"),
    _window("pm_close", "PM Close", "15:50", "16:10"),
)

SESSION_INDEX: dict[str, Window] = {w.key: w for w in CORE_SESSIONS}

#: Session keys that permit an entry by default (``yes`` and ``conditional``).
DEFAULT_ENTRY_SESSIONS: list[str] = [w.key for w in CORE_SESSIONS
                                     if w.trade != TRADE_NO]


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #
def active_core_sessions(minute: int) -> list[Window]:
    """All core ICT sessions active at a given NY minute (may overlap)."""
    return [w for w in CORE_SESSIONS if w.contains(minute)]


def _allow_set(allowed_sessions) -> set[str] | None:
    """An operator allow-list as a set of *current* keys, or ``None`` for all.

    Runs the legacy-alias mapping so a ``VALID_ENTRY_SESSIONS`` written against
    the old table cannot silently resolve to nothing. Returns ``None`` (meaning
    "no restriction") for an empty list, which is the existing contract.
    """
    if not allowed_sessions:
        return None
    return set(normalize_session_keys(allowed_sessions))


def active_session_keys(minute: int) -> list[str]:
    return [w.key for w in active_core_sessions(minute)]


def session_trade_mode(minute: int) -> str:
    """The trade mode governing a NY minute.

    Overlapping windows are resolved with an explicit precedence rather than by
    list order, because the spec's windows genuinely overlap (London Close runs
    inside both NY AM and NY Lunch):

    1. **no** wins outright — the Asian range and NY lunch are hard blocks, so a
       ``no`` window sitting under a ``conditional`` one still blocks.
    2. otherwise **yes** wins — a ``yes`` window under a ``conditional`` one
       still permits trading (this is what keeps 10:00-11:30 NY AM = YES despite
       London Close overlapping it).
    3. otherwise **conditional** — every active window is conditional.
    4. **closed** — no core session is active at this minute.
    """
    active = active_core_sessions(minute)
    if not active:
        return TRADE_CLOSED
    modes = {w.trade for w in active}
    if TRADE_NO in modes:
        return TRADE_NO
    if TRADE_YES in modes:
        return TRADE_YES
    return TRADE_CONDITIONAL


def entry_permission(minute: int, *, conditional_ok: bool = False,
                     allowed_sessions: list[str] | None = None) -> tuple[bool, str]:
    """May a new entry be opened at this NY minute?

    Returns ``(allowed, reason)``; ``reason`` is empty exactly when allowed.
    ``conditional_ok`` is the caller's verdict on the extra bar a CONDITIONAL
    session imposes (the trading model requires a HIGH-grade liquidity target
    there — see :mod:`trading.strategy`). ``allowed_sessions`` is an optional
    additional allow-list; when supplied, the minute must also fall inside one
    of those sessions.
    """
    mode = session_trade_mode(minute)
    if mode == TRADE_CLOSED:
        return False, "outside_session"
    if mode == TRADE_NO:
        return False, "session_not_tradable"
    if allowed_sessions is not None and not _allow_set(allowed_sessions).intersection(
            active_session_keys(minute)):
        return False, "session_not_in_allowlist"
    if mode == TRADE_CONDITIONAL and not conditional_ok:
        return False, "session_conditional_low_liquidity"
    return True, ""


# --------------------------------------------------------------------------- #
# Trading calendar — weekday, and whether the bot should be awake
# --------------------------------------------------------------------------- #
#: Days the bot runs, by :meth:`datetime.datetime.weekday` index (Mon=0 .. Sun=6).
DEFAULT_TRADING_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4)

#: Names accepted in ``TRADING_DAYS``, mapped to the weekday index above.
DAY_INDEX: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

#: Reason reported when the calendar day itself is not a trading day.
WEEKEND_REASON = "market_closed_weekend"

#: Reason reported when the day is fine but no tradeable window is open.
OUTSIDE_SESSION_REASON = "outside_session"

#: Windows the bot stays awake for even though nothing can be entered in them,
#: because the range they build is an *input* to a later session: the Asian
#: high/low is a HIGH-grade liquidity level the London and NY sessions sweep, so
#: it has to be observed even though the window itself is a hard no-trade block.
#: Being awake here is not permission — see :func:`wake_minute`.
OBSERVED_WINDOWS: frozenset[str] = frozenset({"asian_range"})

#: NY weekday names, indexed by :meth:`datetime.datetime.weekday`.
DAY_NAMES: tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday",
                              "Friday", "Saturday", "Sunday")


def parse_trading_days(names) -> frozenset[int]:
    """Resolve a ``TRADING_DAYS`` value to weekday indices.

    Accepts a comma-separated string or an iterable of names. Unknown names are
    ignored rather than raising: a typo in ``.env`` must not stop the bot from
    starting, and an empty or wholly unrecognised list falls back to Mon-Fri,
    which is the conservative answer for an operator who asked for no weekend
    trading.

    Resolve once and hold the result — :func:`is_trading_day` is called per
    candle, and re-parsing a string there would be pure waste.
    """
    if names is None:
        return frozenset(DEFAULT_TRADING_DAYS)
    if isinstance(names, str):
        names = names.split(",")
    days = {DAY_INDEX[n.strip().lower()] for n in names
            if isinstance(n, str) and n.strip().lower() in DAY_INDEX}
    return frozenset(days) if days else frozenset(DEFAULT_TRADING_DAYS)


def is_trading_day(ny_dt: datetime,
                   days: frozenset[int] | tuple[int, ...] | None = None) -> bool:
    """Is this NY calendar day one the bot may trade on?

    The one dimension a minute-of-day window model cannot express. ``days`` is a
    set of :meth:`datetime.datetime.weekday` indices (see
    :func:`parse_trading_days`); ``None`` means the Mon-Fri default.
    """
    if days is None:
        return ny_dt.weekday() in DEFAULT_TRADING_DAYS
    return ny_dt.weekday() in days


def tradeable_minute(minute: int, *,
                     allowed_sessions: list[str] | None = None) -> str | None:
    """The session key permitting trading at this NY minute, else ``None``.

    Answers "is it worth being awake at this minute?", which is a *window-level*
    question — deliberately separate from :func:`entry_permission`, which judges
    a specific setup and additionally weighs liquidity grade and the
    CONDITIONAL bar.

    Overlap precedence matches :func:`session_trade_mode` so the two can never
    disagree: a ``no`` window sitting under a ``conditional`` one still blocks
    (11:45 is NY Lunch, even though London Close is open), otherwise any
    ``yes``/``conditional`` window permits.

    ``allowed_sessions`` is the operator's allow-list. A window that permits
    trading but is not on it yields ``None``: they excluded that session, so
    there is nothing to wake up for. Unlike :func:`entry_permission` this is
    intersected rather than unioned with the active windows, because the caller
    is asking whether *any* reason to be awake exists.
    """
    active = active_core_sessions(minute)
    if any(w.trade == TRADE_NO for w in active):
        return None
    allow = _allow_set(allowed_sessions)
    for w in active:
        if w.trade in (TRADE_YES, TRADE_CONDITIONAL) and (allow is None
                                                         or w.key in allow):
            return w.key
    return None


def wake_minute(minute: int, *,
                allowed_sessions: list[str] | None = None) -> str | None:
    """The session key justifying the bot being *awake* at this NY minute.

    Deliberately distinct from :func:`tradeable_minute`, which answers the
    narrower question "may an entry be taken here?". A window in
    :data:`OBSERVED_WINDOWS` is awake-but-not-tradeable: the bot watches the
    Asian range form because a later session trades against it, and takes
    nothing itself. Conflating the two would either sleep through the range
    (losing the level) or wake for it and consider it an entry window.

    Precedence is the same rule the other two use, so all three agree about any
    given minute: an unobserved ``no`` window blocks outright (11:45 is NY Lunch
    even though London Close is conditional), then any ``yes``/``conditional``
    window on the allow-list, then an observed window.

    The allow-list is intentionally *not* applied to observed windows — an
    operator who trades only NY AM still needs the Asian range built for it.
    """
    active = active_core_sessions(minute)
    if any(w.trade == TRADE_NO and w.key not in OBSERVED_WINDOWS
           for w in active):
        return None
    allow = _allow_set(allowed_sessions)
    for w in active:
        if w.trade in (TRADE_YES, TRADE_CONDITIONAL) and (allow is None
                                                         or w.key in allow):
            return w.key
    for w in active:
        if w.key in OBSERVED_WINDOWS:
            return w.key
    return None


def session_activity(ny_dt: datetime, *,
                     allowed_sessions: list[str] | None = None,
                     days: frozenset[int] | tuple[int, ...] | None = None
                     ) -> tuple[bool, str]:
    """Should the bot be awake at this NY instant, and why?

    Returns ``(active, reason)``. When active, ``reason`` is the session key that
    justified it (so the log can say *why* the bot woke up) — which may be an
    observed, non-tradeable window such as ``asian_range``; when not, it is
    :data:`WEEKEND_REASON` or :data:`OUTSIDE_SESSION_REASON`.

    This is the whole gate: the calendar is checked first, because no minute of a
    Saturday is tradeable no matter what the window table says.
    """
    if not is_trading_day(ny_dt, days):
        return False, WEEKEND_REASON
    key = wake_minute(tu.minute_of_day(ny_dt),
                      allowed_sessions=allowed_sessions)
    if key is None:
        return False, OUTSIDE_SESSION_REASON
    return True, key


def next_activity_start_utc(ny_dt: datetime, *,
                            allowed_sessions: list[str] | None = None,
                            days: frozenset[int] | tuple[int, ...] | None = None,
                            horizon_days: int = 7) -> datetime | None:
    """First UTC instant at/after ``ny_dt`` at which the bot should be awake.

    Returns a naive **UTC** datetime — the caller wants a real elapsed-seconds
    duration, and UTC has no DST discontinuities to reason about.

    The search walks the UTC timeline a minute at a time and converts each step
    back to NY before testing. That is deliberate: adding minutes to a *naive NY*
    datetime is wall-clock arithmetic, which silently skips or repeats an hour
    across a DST transition and would wake the bot an hour late twice a year.
    Stepping UTC and asking "what is the NY time now?" cannot make that mistake,
    which is why there is no hand-rolled "next 02:00" arithmetic here.

    Worst case is ``horizon_days * 1440`` iterations of two ``zoneinfo``
    conversions (tens of milliseconds), and it is called on a state *transition*,
    never once per poll. ``None`` means no active window within the horizon.
    """
    start = tu.ny_to_utc(ny_dt)
    for step in range(1, horizon_days * DAY_MINUTES + 1):
        utc_dt = start + timedelta(minutes=step)
        active, _ = session_activity(tu.utc_to_ny(utc_dt),
                                     allowed_sessions=allowed_sessions,
                                     days=days)
        if active:
            return utc_dt
    return None


#: The operator-facing line for each awake state. Observed windows say what they
#: are actually doing there ("Building liquidity") rather than claiming to scan,
#: because no entry can be taken in one.
ACTIVITY_LINES: dict[str, str] = {
    "asian_range": "Asian session active — Building liquidity",
    "london_open": "London session active — Strategy scanner ON",
    "ny_am": "NY AM active — Strategy scanner ON",
    "ny_pm": "NY PM active — Strategy scanner ON",
    "power_hour": "Power Hour active — Strategy scanner ON",
    "gate_disabled": "Activity gate disabled — Strategy scanner ON",
}


def activity_log_line(ny_dt: datetime, active: bool, reason: str) -> str:
    """The ``[SESSION] ...`` line for the current state.

    ``reason`` is :func:`session_activity`'s second element, and ``ny_dt`` is the
    NY instant it was evaluated at. Built here rather than at the call site
    because the wording depends on the window table — a name only this module
    knows — and the live loop should not have to invent it.
    """
    if active:
        body = ACTIVITY_LINES.get(reason, f"{reason} — Strategy scanner ON")
    elif reason == WEEKEND_REASON:
        body = f"{DAY_NAMES[ny_dt.weekday()]} — Weekend mode"
    else:
        # A trading day with nothing tradeable open. Name the window that is
        # blocking when the table knows one, so NY Lunch reads as a deliberate
        # block rather than an unexplained gap in the session.
        window = primary_session(tu.minute_of_day(ny_dt))
        if window is not None and window.trade == TRADE_NO:
            body = f"{window.label} — New signals blocked"
        else:
            body = "Outside trading window — Scanner idle"
    return f"[SESSION] {body}"


def active_silver_bullet(minute: int) -> Window | None:
    """The Silver Bullet window containing the minute, if any."""
    for w in SILVER_BULLET_WINDOWS:
        if w.contains(minute):
            return w
    return None


def active_silver_bullet_key(minute: int) -> str | None:
    w = active_silver_bullet(minute)
    return w.key if w else None


def active_macro_window(minute: int) -> Window | None:
    """The macro window containing the minute, if any."""
    for w in MACRO_WINDOWS:
        if w.contains(minute):
            return w
    return None


def active_macro_window_key(minute: int) -> str | None:
    w = active_macro_window(minute)
    return w.key if w else None


def primary_session(minute: int) -> Window | None:
    """Most specific active core session for display (smallest window wins)."""
    matches = active_core_sessions(minute)
    if not matches:
        return None
    # Favour the narrowest window (ties broken by definition order, which the
    # module docstring documents as chosen for the spec's overlap hours).
    return min(matches, key=lambda w: (w.end - w.start, list(CORE_SESSIONS).index(w)))


# --------------------------------------------------------------------------- #
# Public entry point: NY instant -> ICT session
# --------------------------------------------------------------------------- #
def get_trading_session(ny_dt: datetime) -> Window | None:
    """The ICT session in effect at a **New York local** instant, or ``None``.

    The single question the rest of the codebase asks about session time. It
    takes an already-normalized NY datetime — never a broker or UTC one — so the
    conversion (MT5 -> UTC -> NY) must have happened first, in
    :mod:`trading.time_utils`. Passing a broker-clock datetime here is the bug
    this signature is shaped to prevent.
    """
    return primary_session(tu.minute_of_day(ny_dt))


def get_trading_session_label(ny_dt: datetime) -> str:
    """The session's display name at a NY instant, or ``"Outside session"``."""
    window = get_trading_session(ny_dt)
    return window.label if window else "Outside session"


def get_trading_session_key(ny_dt: datetime) -> str:
    """The session's key at a NY instant, or ``""`` when none is open."""
    window = get_trading_session(ny_dt)
    return window.key if window else ""


def describe_session_at(ny_dt: datetime) -> str:
    """A one-line ``19:00-00:00 Asian (NY)``-style description for logs.

    Includes the window's own bounds so a reader can check the NY clock against
    the session it was classified into without consulting the source.
    """
    window = get_trading_session(ny_dt)
    if window is None:
        return "Outside session"
    return f"{window.start_hhmm}-{window.end_hhmm} {window.label} (NY)"
