"""Centralized timezone conversion.

The project runs on the **America/New_York** clock, resolved through
:mod:`zoneinfo` so daylight-saving transitions are handled correctly: UTC-5
(EST) in winter, UTC-4 (EDT) in summer. There is no hard-coded offset anywhere
in the codebase — every session / window / liquidity timestamp is expressed on
this NY clock as a naive :class:`datetime.datetime`.

MT5 returns candle times in the **broker server** clock. To keep conversions in
one place and correct everywhere, only this module converts between the broker
clock, UTC, and the NY clock. The broker's offset ahead of UTC is read from
``MT5_SERVER_UTC_OFFSET`` in the environment (default +2).

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

try:  # pragma: no cover - depends on the host's tz database
    NY_TZ: ZoneInfo | None = ZoneInfo(NY_TZ_NAME)
except Exception:  # pragma: no cover
    NY_TZ = None

_UTC = _dt.timezone.utc


def server_utc_offset_hours() -> float:
    """Broker server clock offset ahead of UTC, in hours (configurable)."""
    return get_settings().mt5_server_utc_offset


def server_offset(offset_hours: float | None = None) -> timedelta:
    """Return the server offset as a timedelta (explicit override wins)."""
    return timedelta(hours=server_utc_offset_hours() if offset_hours is None else offset_hours)


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


def ny_hour_float(ny_dt: datetime) -> float:
    """Hour-of-day as a float, e.g. 09:30 -> 9.5. Operates on the NY clock."""
    return ny_dt.hour + ny_dt.minute / 60.0 + ny_dt.second / 3600.0


def minute_of_day(ny_dt: datetime) -> int:
    """Minutes since midnight on the NY clock (0..1439)."""
    return ny_dt.hour * 60 + ny_dt.minute


def ny_date_key(ny_dt: datetime) -> str:
    """ISO date string (YYYY-MM-DD) of the NY calendar day."""
    return ny_dt.strftime("%Y-%m-%d")
