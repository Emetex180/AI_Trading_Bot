"""Centralized timezone conversion.

The project runs on the **America/New_York** clock, resolved through
:mod:`zoneinfo` so daylight-saving transitions are handled correctly: UTC-5
(EST) in winter, UTC-4 (EDT) in summer. There is no hard-coded offset anywhere
in the codebase — every session / window / liquidity timestamp is expressed on
this NY clock as a naive :class:`datetime.datetime`.

MT5 returns candle times in the **broker server** clock. To keep conversions in
one place and correct everywhere, only this module converts between the broker
clock, UTC, and the NY clock.

The broker's offset ahead of UTC is **never assumed**. It is resolved at
runtime, in this order:

1. ``MT5_SERVER_UTC_OFFSET`` — only when the operator explicitly sets a number.
   This is a deliberate pin, so it wins; a disagreement with the discovered
   value is logged loudly rather than silently obeyed.
2. The offset **discovered from the live MT5 terminal** by
   :meth:`trading.mt5_client.MT5Client.discover_server_utc_offset_hours`, which
   compares the broker's own tick/bar clock against real UTC.
3. ``NY_FALLBACK_OFFSET_HOURS`` — only when neither exists, and always logged as
   an error, because trading on an unverified broker offset mis-dates every
   session window.

There is no ``BROKER_TIMEZONE = "UTC+3"`` constant anywhere in the codebase.

Terminology used here
---------------------
* ``broker``   – naive datetime as returned by MT5 ``copy_rates*``.
* ``utc``      – naive datetime representing real UTC.
* ``ny``       – naive datetime representing the project's NY wall clock.

DST edge cases
--------------
A naive NY datetime is *localised* into the zone when converting to UTC. During
the autumn fall-back hour a wall time is ambiguous; the earlier (``fold=0``)
instant is used, which is the standard interpretation. During the spring
forward-gap the wall time does not exist; :mod:`zoneinfo` maps it forward by the
gap, so an input inside the gap resolves to the same instant as the wall time
immediately after it. Both cases are documented behaviour rather than silent
arithmetic on a guessed offset.
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import get_settings

#: The single source of truth for the project's wall clock.
NY_TZ_NAME = "America/New_York"

#: Nominal NY offset, used *only* on a machine with no IANA timezone database
#: (a bare Windows Python without ``tzdata``). Every normal run is DST-aware.
NY_FALLBACK_OFFSET_HOURS = -4

#: Last-resort stand-in for a broker offset that could not be verified. It is a
#: guess, so it is never allowed to drive a live scan: :func:`broker_offset_verified`
#: is False whenever this value is in play, and the live loop refuses to scan on
#: it rather than trading on a session clock that may be hours out. It remains
#: only so that non-trading reads (a history probe, a backtest replay of candles
#: already fetched) cannot crash on a missing offset; every use is logged at
#: ERROR by :func:`warn_if_server_offset_unverified`.
BROKER_FALLBACK_OFFSET_HOURS = 0.0

try:  # pragma: no cover - depends on the host's tz database
    NY_TZ: ZoneInfo | None = ZoneInfo(NY_TZ_NAME)
except Exception:  # pragma: no cover
    NY_TZ = None

_UTC = _dt.timezone.utc


# --------------------------------------------------------------------------- #
# Broker offset resolution — discovered, never assumed
# --------------------------------------------------------------------------- #
#: Offset discovered from the live terminal this session, and where it came from.
#: Module state because :func:`broker_to_utc` is called from deep inside the
#: candle pipeline with no handle on the MT5 client; the runner resolves it once
#: per terminal session (:meth:`trading.mt5_client.MT5Client.connect`) and every
#: downstream conversion then reads the same verified number.
_discovered_offset: float | None = None
_discovered_source: str = "unresolved"


def set_server_utc_offset(hours: float | None, source: str = "discovered") -> None:
    """Record the broker offset discovered from the MT5 terminal.

    ``None`` clears it back to unresolved, which re-arms the fallback path.
    """
    global _discovered_offset, _discovered_source
    _discovered_offset = None if hours is None else float(hours)
    _discovered_source = source


def clear_server_utc_offset() -> None:
    """Forget the discovered offset (tests, and on terminal disconnect)."""
    set_server_utc_offset(None, "unresolved")


def server_offset_source() -> str:
    """How the current broker offset was obtained — for the debug log line."""
    return _discovered_source


def configured_server_utc_offset() -> float | None:
    """The operator's explicit ``MT5_SERVER_UTC_OFFSET``, or ``None`` for auto.

    ``None``/blank means "discover it", which is the default. A number means the
    operator pinned it and is taking responsibility for it.
    """
    return get_settings().mt5_server_utc_offset


def server_utc_offset_hours() -> float:
    """Broker server clock offset ahead of UTC, in hours.

    Priority: explicit configuration, then the value discovered from the live
    terminal, then an unverified fallback. See the module docstring.
    """
    configured = configured_server_utc_offset()
    if configured is not None:
        return configured
    if _discovered_offset is not None:
        return _discovered_offset
    return BROKER_FALLBACK_OFFSET_HOURS


def broker_offset_verified() -> bool:
    """Was the broker offset *established*, rather than fallen back to?

    True when the operator pinned ``MT5_SERVER_UTC_OFFSET`` or the live terminal
    was measured, False when neither exists and :func:`server_utc_offset_hours`
    is returning the unverified fallback. The scanner reads this to **fail
    closed**: with an unverified broker clock every session window may be
    mis-dated by hours, and mis-dated windows produce confident, wrong signals —
    a missed session is recoverable, a wrong one is not.
    """
    return (configured_server_utc_offset() is not None
            or _discovered_offset is not None)


def warn_if_server_offset_unverified() -> str | None:
    """A loud message when the broker offset is a guess, else ``None``.

    Called by the runner once per terminal session. Silent when the offset was
    configured or discovered — only the unverified fallback is worth an ERROR,
    because that is the state in which every session window may be mis-dated.
    """
    if configured_server_utc_offset() is not None or _discovered_offset is not None:
        return None
    return ("MT5 broker offset could NOT be verified: no MT5_SERVER_UTC_OFFSET "
            "set and live discovery failed. Falling back to "
            f"UTC{BROKER_FALLBACK_OFFSET_HOURS:+.0f} — every ICT session window "
            "may be mis-dated until the terminal is reachable while the market "
            "is open.")


def server_offset(offset_hours: float | None = None) -> timedelta:
    """Return the server offset as a timedelta (explicit override wins)."""
    return timedelta(hours=server_utc_offset_hours() if offset_hours is None else offset_hours)


# --------------------------------------------------------------------------- #
# MT5 epoch parsing
# --------------------------------------------------------------------------- #
def utc_epoch_to_naive(epoch_seconds: float) -> datetime:
    """An MT5 row timestamp -> the **broker wall clock** as a naive datetime.

    MT5's ``rates['time']`` is an epoch whose *calendar value* is the server's
    wall clock, so it must be read back as UTC. ``datetime.fromtimestamp(t)``
    without a timezone reads it in the **host machine's** local zone instead,
    which silently shifts every candle by the machine's own UTC offset before a
    broker offset is even applied — the bug this function exists to prevent.
    """
    return datetime.fromtimestamp(float(epoch_seconds), tz=_UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# NY clock
# --------------------------------------------------------------------------- #
def ny_offset(utc_dt: datetime) -> timedelta:
    """The NY offset in effect at a given UTC instant (-5h EST / -4h EDT)."""
    if NY_TZ is None:  # pragma: no cover - no tz database on this host
        return timedelta(hours=NY_FALLBACK_OFFSET_HOURS)
    return utc_dt.replace(tzinfo=_UTC).astimezone(NY_TZ).utcoffset() or timedelta(0)


def ny_offset_hours(utc_dt: datetime | None = None) -> float:
    """The NY offset in hours at ``utc_dt`` (default: now).

    A *function*, not a constant: a module-level ``-4`` is exactly the
    hard-coded offset this module exists to avoid. Callers that need to hand an
    offset to the browser (the dashboard charts) call this per instant.
    """
    return ny_offset(utc_dt if utc_dt is not None else now_utc()).total_seconds() / 3600.0


def utc_to_ny(utc_dt: datetime) -> datetime:
    """Real UTC -> project NY clock (naive). DST-aware."""
    if NY_TZ is None:  # pragma: no cover
        return utc_dt + timedelta(hours=NY_FALLBACK_OFFSET_HOURS)
    return utc_dt.replace(tzinfo=_UTC).astimezone(NY_TZ).replace(tzinfo=None)


def ny_to_utc(ny_dt: datetime) -> datetime:
    """Project NY clock -> real UTC (naive). DST-aware.

    ``ny_dt`` is read as a New York *wall clock* time. Ambiguous times resolve
    to the earlier instant (``fold=0``); non-existent times (the spring-forward
    gap) map forward by the gap — see the module docstring.
    """
    if NY_TZ is None:  # pragma: no cover
        return ny_dt - timedelta(hours=NY_FALLBACK_OFFSET_HOURS)
    return ny_dt.replace(tzinfo=NY_TZ).astimezone(_UTC).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Broker clock
# --------------------------------------------------------------------------- #
def broker_to_utc(broker_dt: datetime, offset_hours: float | None = None) -> datetime:
    """Broker server time -> real UTC (naive)."""
    return broker_dt - server_offset(offset_hours)


def utc_to_broker(utc_dt: datetime, offset_hours: float | None = None) -> datetime:
    """Real UTC -> broker server clock (naive).

    The exact inverse of :func:`broker_to_utc`. Needed wherever a *time range*
    has to be handed to MT5, because ``copy_rates_range`` filters bars on the
    broker clock rather than on UTC.
    """
    return utc_dt + server_offset(offset_hours)


def broker_to_ny(broker_dt: datetime, offset_hours: float | None = None) -> datetime:
    """Broker server time -> project NY clock (naive)."""
    return utc_to_ny(broker_to_utc(broker_dt, offset_hours))


def ny_to_broker(ny_dt: datetime, offset_hours: float | None = None) -> datetime:
    """Project NY clock -> broker server clock (naive)."""
    return utc_to_broker(ny_to_utc(ny_dt), offset_hours)


# --------------------------------------------------------------------------- #
# "Now" and small formatting helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current real UTC as a naive datetime."""
    return datetime.now(_UTC).replace(tzinfo=None)


def now_ny() -> datetime:
    """Current time on the project's NY clock."""
    return utc_to_ny(now_utc())


def get_new_york_time(utc_dt: datetime | None = None) -> datetime:
    """A real-UTC instant as New York local time (naive).

    The named entry point the strategy asks for. ``None`` means *now*. Every
    session decision must be taken on the value this returns, never on a broker
    or UTC datetime and never on a fixed UTC-4 — the offset is read from the
    IANA database for the date in question.
    """
    return utc_to_ny(utc_dt if utc_dt is not None else now_utc())


def get_new_york_time_from_broker(broker_dt: datetime,
                                  offset_hours: float | None = None) -> datetime:
    """An MT5 broker-clock instant as New York local time (naive)."""
    return broker_to_ny(broker_dt, offset_hours)


def ny_hour_float(ny_dt: datetime) -> float:
    """Hour-of-day as a float, e.g. 09:30 -> 9.5. Operates on the NY clock."""
    return ny_dt.hour + ny_dt.minute / 60.0 + ny_dt.second / 3600.0


def minute_of_day(ny_dt: datetime) -> int:
    """Minutes since midnight on the NY clock (0..1439)."""
    return ny_dt.hour * 60 + ny_dt.minute


def ny_date_key(ny_dt: datetime) -> str:
    """ISO date string (YYYY-MM-DD) of the NY calendar day."""
    return ny_dt.strftime("%Y-%m-%d")


def ny_offset_label(utc_dt: datetime | None = None) -> str:
    """The NY offset at ``utc_dt`` as ``-04:00``/``-05:00``, plus its zone name.

    A label, not an arithmetic input — the point is that it *changes* across the
    DST switch, which is how an operator confirms the tz database is in play
    rather than a frozen constant.
    """
    utc_dt = utc_dt if utc_dt is not None else now_utc()
    off = ny_offset(utc_dt)
    total = int(off.total_seconds())
    sign = "-" if total < 0 else "+"
    hh, mm = divmod(abs(total) // 60, 60)
    name = "EDT" if off == timedelta(hours=-4) else "EST"
    return f"{name} ({sign}{hh:02d}:{mm:02d})"


def ny_zone_abbr(utc_dt: datetime | None = None) -> str:
    """``EDT`` / ``EST`` at a UTC instant, read from the zone database.

    A separate function from :func:`ny_offset_label` because the abbreviation is
    what a rendered time needs next to it, without the offset arithmetic.

    It cannot be had from ``utc_to_ny(...).strftime("%Z")``: that returns a
    *naive* NY wall clock (every stored timestamp in this project is naive, so
    nothing downstream has to think about tzinfo), and ``%Z`` on a naive
    datetime is the empty string on every platform. The abbreviation has to be
    taken from the aware value, before the tzinfo is dropped.
    """
    if NY_TZ is None:  # pragma: no cover - no tz database on this host
        return ""
    instant = utc_dt if utc_dt is not None else now_utc()
    return instant.replace(tzinfo=_UTC).astimezone(NY_TZ).tzname() or ""


# --------------------------------------------------------------------------- #
# Debug: the MT5 -> UTC -> New York -> session chain
# --------------------------------------------------------------------------- #
#: Either source of truth for the temporary conversion logging.
DEBUG_ENV = "TIME_DEBUG"


def time_debug_enabled() -> bool:
    """Whether the MT5 -> UTC -> NY -> session trace should be logged.

    Reads the project's own ``TIME_DEBUG`` flag (honoured by ``.env`` like every
    other setting) and falls back to ``FLASK_DEBUG``, so an operator who already
    runs the bot in debug mode gets the trace without a second switch.
    """
    settings = get_settings()
    if getattr(settings, "time_debug", None):
        return True
    return bool(getattr(settings, "flask_debug", False))


def format_time_chain(broker_dt: datetime | None, utc_dt: datetime,
                      ny_dt: datetime, session: str = "",
                      offset_hours: float | None = None) -> str:
    """The four debug lines proving one candle's conversion.

    Every value is passed in already-converted, so the formatter cannot invent a
    conversion of its own — it is a view of what the pipeline actually did.
    """
    offset = server_utc_offset_hours() if offset_hours is None else offset_hours
    broker_line = broker_dt.strftime("%Y-%m-%d %H:%M:%S") if broker_dt else "-"
    lines = [
        f"[MT5 TIME]     {broker_line}  (broker clock, UTC{offset:+.2f}, "
        f"source: {server_offset_source()})",
        f"[UTC TIME]     {utc_dt.strftime('%Y-%m-%d %H:%M:%S')}",
        f"[NEW YORK TIME] {ny_dt.strftime('%Y-%m-%d %H:%M:%S')}  {ny_offset_label(utc_dt)}",
    ]
    if session:
        lines.append(f"[ICT SESSION]  {session}")
    return "\n".join(lines)
