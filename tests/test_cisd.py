"""CISD module tests (timeframe selection + confirmation with synthetic data)."""
from datetime import datetime, timedelta

import pytest

from trading import cisd
from trading.cisd import CISDParams

from conftest import c


def _m(i):
    return timedelta(minutes=i)


def test_choose_timeframe_threshold():
    before = datetime(2026, 1, 5, 8, 30)   # NY 08:30 -> before 09:00
    at = datetime(2026, 1, 5, 9, 0)        # exactly 09:00 -> at/after
    after = datetime(2026, 1, 5, 11, 0)
    assert cisd.choose_timeframe(before) == "M15"
    assert cisd.choose_timeframe(at) == "M5"
    assert cisd.choose_timeframe(after) == "M5"
    # Custom threshold respected.
    assert cisd.choose_timeframe(before, threshold_hour_ny=8) == "M5"


def test_confirm_bullish_sell_side_reclaim():
    level = 100.0
    start = datetime(2026, 1, 5, 8, 0)
    candles = [
        c(start, 99.5, 99.9, 99.4, 99.6),                    # above, no sweep
        c(start + _m(1), 99.0, 99.7, 98.5, 98.9),            # sweeps below level
        c(start + _m(2), 99.0, 101.0, 98.9, 100.8),          # bullish reclaim
    ]
    found = cisd.confirm(candles, level, direction="buy")
    assert found is not None
    assert found.close > level


def test_no_confirm_when_no_reclaim():
    level = 100.0
    start = datetime(2026, 1, 5, 8, 0)
    candles = [
        c(start, 100.5, 100.8, 99.2, 99.4),                  # sweeps below, closes below
        c(start + _m(1), 99.5, 100.5, 99.0, 99.2),           # stays below
    ]
    assert cisd.confirm(candles, level, direction="buy") is None


def test_confirm_bearish_mirror():
    level = 100.0
    start = datetime(2026, 1, 5, 8, 0)
    candles = [
        c(start, 100.2, 100.5, 99.8, 100.1),
        c(start + _m(1), 100.3, 101.4, 100.1, 101.2),        # sweeps above 100
        c(start + _m(2), 101.0, 101.3, 99.7, 99.6),          # bearish reclaim
    ]
    found = cisd.confirm(candles, level, direction="sell")
    assert found is not None and found.close < level


def test_after_time_filter_and_max_candles():
    level = 100.0
    start = datetime(2026, 1, 5, 8, 0)
    early = c(start, 99.0, 100.5, 98.5, 100.4)
    later = c(start + _m(10), 100.0, 100.2, 99.8, 100.1)
    found = cisd.confirm([early, later], level, direction="buy",
                         after_time_utc=start + _m(5))
    assert found is not None and found.t_utc >= start + _m(5)

    found2 = cisd.confirm([early, later], level, direction="buy",
                          after_time_utc=start,
                          params=CISDParams(max_candles=1))
    assert found2 is later


def test_document_assumption_meta():
    """The CISD definition is explicit & configurable, not silently invented."""
    p = CISDParams(mode="displacement_reclaim", displacement_frac=0.3)
    assert p.mode == "displacement_reclaim"
    with pytest.raises(ValueError):
        cisd.confirm([], 1.0, "buy", params=CISDParams(mode="made_up_mode"))
    with pytest.raises(ValueError):
        cisd.confirm([], 1.0, "sideways")
