"""Shared test fixtures — synthetic candle builders (no MT5 required)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from config import reload_settings
from trading.bars import Candle, make_candle
from trading import time_utils as tu


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the cached Settings around every test.

    ``get_settings`` caches a single snapshot for the whole process — the
    database URL included. Tests routinely point ``DATABASE_URL`` at a temp
    file, so without this the next test would inherit a snapshot aimed at a
    database that no longer exists.
    """
    reload_settings()
    yield
    reload_settings()


def ny(minute_of_day: int, day: int = 1, hour_offset: int = 0) -> datetime:
    """NY (UTC-4) datetime for a given day-of-month and minute-of-day."""
    base = datetime(2026, 1, day, 0, 0)
    return base + timedelta(minutes=minute_of_day) + timedelta(hours=hour_offset)


def c(ny_dt: datetime, o: float, h: float, l: float, cl: float, vol: int = 1) -> Candle:
    """Single closed candle labelled by its NY open time."""
    return make_candle(t_utc=tu.ny_to_utc(ny_dt), open_=o, high=h, low=l, close=cl, volume=vol)


def series(start_ny: datetime, rows: list[tuple], vol: int = 1) -> list[Candle]:
    """Build consecutive M1 candles starting at ``start_ny``.

    ``rows`` is a list of ``(open, high, low, close)``; times tick forward 1 min.
    """
    out = []
    t = start_ny
    for (o, h, l, cl) in rows:
        out.append(c(t, o, h, l, cl, vol))
        t += timedelta(minutes=1)
    return out


@pytest.fixture
def make_candles():
    """Factory returning a callable that produces synthetic candles."""
    return c


@pytest.fixture
def h1_days():
    """A helper producing one H1 candle per hour across two NY days."""
    def build(start_day: int, hours: int, base: float = 100.0):
        candles = []
        for day in range(start_day, start_day + 2):
            for h in range(24):
                if len(candles) >= hours:
                    return candles
                t = datetime(2026, 1, day, h, 0)
                o = base + (h % 5)
                cl = base + (h % 5) + 0.2
                hi = max(o, cl) + 0.3
                lo = min(o, cl) - 0.3
                candles.append(c(t, o, hi, lo, cl))
        return candles
    return build
