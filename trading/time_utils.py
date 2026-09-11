"""Centralized timezone conversion.

The project runs on a FIXED algorithmic New York clock of **UTC-4** throughout
the year — it never auto-switches to UTC-5 for winter. Every session / window /
liquidity timestamp in the strategy is expressed on this NY clock as a naive
:class:`datetime.datetime`.

MT5 returns candle times in the **broker server clock**. To keep conversions in
one place and correct everywhere, only this module converts between the broker
clock, UTC, and the NY (UTC-4) clock. The broker's offset ahead of UTC is read
from ``MT5_SERVER_UTC_OFFSET`` in the environment (default +2).

Terminology used here
---------------------
* ``broker``   – naive datetime as returned by MT5 ``copy_rates*``.
* ``utc``      – naive datetime representing real UTC.
* ``ny``       – naive datetime representing the project's NY clock (UTC-4).
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timedelta

from config import get_settings

# Fixed algorithmic NY offset: UTC-4 always.
NY_OFFSET_HOURS = -4
NY_OFFSET = timedelta(hours=NY_OFFSET_HOURS)


def server_utc_offset_hours() -> float:
    """Broker server clock offset ahead of UTC, in hours (configurable)."""
    return get_settings().mt5_server_utc_offset


def server_offset(offset_hours: float | None = None) -> timedelta:
    """Return the server offset as a timedelta (explicit override wins)."""
    return timedelta(hours=server_utc_offset_hours() if offset_hours is None else offset_hours)


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
    """Broker server time -> project NY clock (naive, UTC-4).

    Net shift is ``offset_hours + 4`` (server ahead of UTC, NY behind UTC).
    """
    return broker_dt - (server_offset(offset_hours) - NY_OFFSET)


def utc_to_ny(utc_dt: datetime) -> datetime:
    """Real UTC -> project NY clock (naive, UTC-4)."""
    return utc_dt + NY_OFFSET  # NY is behind UTC, so subtract 4h => +(-4h)


def ny_to_utc(ny_dt: datetime) -> datetime:
    """Project NY clock -> real UTC (naive)."""
    return ny_dt - NY_OFFSET


def ny_to_broker(ny_dt: datetime, offset_hours: float | None = None) -> datetime:
    """Project NY clock -> broker server clock (naive)."""
    return ny_dt + (server_offset(offset_hours) - NY_OFFSET)


def now_utc() -> datetime:
    """Current real UTC as a naive datetime."""
    return datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def now_ny() -> datetime:
    """Current time on the project's NY (UTC-4) clock."""
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
