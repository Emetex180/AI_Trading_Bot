"""FVG detection tests (three-candle gaps on M1)."""
from datetime import datetime, timedelta

from trading.fvg import FVG, classify, fvg_at_three, fvg_on_tail

from conftest import c, series


def _bullish_three(start):
    """c0 high 10, displacement, c2 low > 10 -> bullish gap [10, 12]. """
    candles = [
        c(start, 9.0, 10.0, 8.8, 9.5),
        c(start + timedelta(minutes=1), 9.5, 13.0, 9.6, 12.5),
        c(start + timedelta(minutes=2), 12.5, 12.8, 12.2, 12.6),
    ]
    return candles


def test_bullish_fvg_boundaries():
    start = datetime(2026, 1, 5, 8, 0)
    candles = _bullish_three(start)
    f = fvg_at_three(*candles)
    assert f is not None
    assert f.direction == "bullish"
    assert (f.lower, f.upper) == (10.0, 12.2)


def test_bearish_fvg_boundaries():
    start = datetime(2026, 1, 5, 8, 0)
    candles = [
        c(start, 11.0, 12.0, 10.8, 11.4),   # high-ish
        c(start + timedelta(minutes=1), 11.0, 10.6, 9.0, 9.4),  # big drop
        c(start + timedelta(minutes=2), 9.4, 9.8, 9.2, 9.5),    # low 9.2
    ]
    # c0.low = 10.8 ; c2.high = 9.8 < 10.8 => bearish gap (upper=c0.low, lower=c2.high)
    f = fvg_at_three(*candles)
    assert f is not None and f.direction == "bearish"
    assert (f.lower, f.upper) == (9.8, 10.8)


def test_no_fvg_when_third_candle_overlaps():
    start = datetime(2026, 1, 5, 8, 0)
    candles = [
        c(start, 9.0, 10.0, 8.8, 9.5),
        c(start + timedelta(minutes=1), 9.5, 11.0, 9.6, 10.5),
        c(start + timedelta(minutes=2), 10.4, 10.6, 9.9, 10.2),  # low 9.9 < c0.high
    ]
    assert fvg_at_three(*candles) is None


def test_fvg_on_tail_and_time_filter():
    start = datetime(2026, 1, 5, 8, 0)
    candles = _bullish_three(start)
    assert fvg_on_tail(candles) is not None
    # Requires pattern formation at/after a later time -> none (time is UTC).
    assert fvg_on_tail(candles, after_time_utc=candles[-1].t_utc + timedelta(minutes=10)) is None


def test_retrace_and_invalidation():
    start = datetime(2026, 1, 5, 8, 0)
    candles = _bullish_three(start)
    f = fvg_on_tail(candles)
    # A candle pulling back into the gap is a retracement.
    pull = c(start + timedelta(minutes=3), 12.0, 12.2, 11.0, 11.5)
    assert classify(pull, f) == "retraced"
    # Closing through the lower boundary invalidates a bullish FVG.
    kill = c(start + timedelta(minutes=4), 11.5, 11.6, 9.5, 9.6)
    assert classify(kill, f) == "invalidated"
    # Untouched candle.
    far = c(start + timedelta(minutes=5), 13.0, 13.2, 12.6, 13.0)
    assert classify(far, f) == "untouched"


def test_requires_three_closed_candles():
    start = datetime(2026, 1, 5, 8, 0)
    assert fvg_on_tail([]) is None
    assert fvg_on_tail([c(start, 1, 2, 0, 1), c(start + timedelta(minutes=1), 1, 2, 0, 1)]) is None
