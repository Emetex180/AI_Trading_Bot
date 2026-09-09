"""Liquidity detector tests."""
from datetime import datetime

from trading import liquidity as liq
from trading.sessions import CORE_SESSIONS, Window

from conftest import c


def test_previous_day_high_low():
    # Day 1 candles around 100 with range 98..102.
    candles = []
    for h in range(0, 24, 2):
        t = datetime(2026, 1, 5, h, 0)  # NY Jan 5
        candles.append(c(t, 100, 102, 98, 101))
    ext = liq.previous_day_high_low(candles, "2026-01-06")
    assert ext == (102, 98)


def test_day_extremes_missing_day():
    candles = [c(datetime(2026, 1, 5, 10, 0), 100, 101, 99, 100)]
    assert liq.day_extremes(candles, "2026-01-06") is None


def test_find_swings():
    # Build a zig-zag: rising pivots then a clear high then lower.
    rows = [
        (10, 11, 9),   # rising
        (11, 12, 10),
        (12, 13, 11),  # swing high 13
        (11, 12, 10),
        (10, 11, 9),   # swing low 9
        (11, 12, 10),
    ]
    candles = []
    base = datetime(2026, 1, 5, 0, 0)
    from datetime import timedelta
    for i, (o, h, lo) in enumerate(rows):
        candles.append(c(base + timedelta(hours=i), o, h, lo, (o + h) / 2))
    swings = liq.find_swings(candles, lookback=1)
    highs = [s.price for s in swings if s.is_high]
    lows = [s.price for s in swings if not s.is_high]
    assert 13 in highs
    assert 9 in lows


def test_find_swings_requires_right_context_no_lookahead():
    # Monotonic series yields no pivot; a new high at the very end is never
    # classified because its right-hand context does not exist yet.
    candles = []
    base = datetime(2026, 1, 5, 0, 0)
    from datetime import timedelta
    for i in range(6):
        v = 10 + i
        candles.append(c(base + timedelta(hours=i), v, v + 1, v - 1, v))
    swings = liq.find_swings(candles, lookback=2)
    assert swings == []


def test_equal_levels_cluster():
    from trading.liquidity import Swing
    swings = [
        Swing("H", 100.00, 1, datetime(2026, 1, 5, 1, 0)),
        Swing("H", 100.05, 5, datetime(2026, 1, 5, 2, 0)),
        Swing("L", 98.00, 7, datetime(2026, 1, 5, 3, 0)),
        Swing("L", 98.02, 9, datetime(2026, 1, 5, 4, 0)),
    ]
    eq = liq.equal_levels(swings, tolerance=0.1)
    kinds = {(e.kind) for e in eq}
    assert kinds == {"EQ_HIGH", "EQ_LOW"}
    eq_high = next(e for e in eq if e.kind == "EQ_HIGH")
    assert eq_high.price == 100.05 and eq_high.touches == 2


def _day_candles(day, base=100.0, amplitude=3.0):
    """H1 candles for one NY day, peaking at base+amplitude and base-amplitude."""
    from datetime import timedelta
    out = []
    for h in range(0, 24):
        t = datetime(2026, 1, day, h, 0)
        o = base
        hi = base + amplitude if (h % 6) == 2 else base + 1
        lo = base - amplitude if (h % 6) == 5 else base - 1
        out.append(c(t, o, hi, lo, (hi + lo) / 2))
    return out


def test_detect_sweep_below_pdl_bullish():
    day1 = _day_candles(5)                       # establishes PDH/PDL for Jan 6
    levels = liq.build_level_snapshot(day1, as_of_date="2026-01-06")
    pdl = next(l for l in levels if l.kind == "PDL")

    # Day 2 candle sweeps below PDL then closes back above it.
    from datetime import timedelta
    t = datetime(2026, 1, 6, 10, 0)
    sweep = c(t, pdl.price, pdl.price + 3, pdl.price - 2.0, pdl.price + 1.5)
    events = liq.detect_sweeps(sweep, levels)
    bullish = [e for e in events if e.direction == "bullish"]
    assert len(bullish) >= 1
    assert any(e.level.kind == "PDL" for e in bullish)


def test_detect_sweep_requires_reclaim():
    levels = [liq.Level("PDL", 100.0, datetime(2026, 1, 5, 0, 0))]
    t = datetime(2026, 1, 6, 10, 0)
    # Closes BELOW the swept level -> genuine breakdown, NOT a purge.
    non_reclaim = c(t, 101, 101.5, 99.5, 99.6)
    assert liq.detect_sweeps(non_reclaim, levels) == []
    # Closes back above -> purge.
    reclaim = c(t, 100.5, 101.0, 99.5, 100.4)
    assert len(liq.detect_sweeps(reclaim, levels)) == 1


def test_nearest_target_above_for_buy():
    levels = [
        liq.Level("SWING_HIGH", 105.0, datetime(2026, 1, 5, 0, 0)),
        liq.Level("PDH", 110.0, datetime(2026, 1, 5, 0, 0)),
    ]
    tgt = liq.nearest_target(102.0, "buy", levels)
    assert tgt.price == 105.0
    assert liq.nearest_target(112.0, "buy", levels) is None


def test_strongest_purge():
    from trading.liquidity import Level, SweepEvent
    base = datetime(2026, 1, 5, 0, 0)
    lvl = Level("PDL", 100.0, base)
    evs = [
        SweepEvent("bullish", lvl, 0.5, 0, base),
        SweepEvent("bullish", lvl, 2.0, 0, base),
        SweepEvent("bearish", lvl, 1.0, 0, base),
    ]
    best = liq.strongest_purge(evs, "bullish")
    assert best.distance == 2.0
    assert liq.strongest_purge(evs, "sell") is None
