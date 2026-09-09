"""Bar aggregation / BarSet tests (no lookahead guarantees)."""
from datetime import datetime, timedelta

from trading.bars import TIMEFRAME_MINUTES, BarSet, aggregate_candles, make_candle
from trading import time_utils as tu


def _m1_utc_minute(base, minute):
    return base + timedelta(minutes=minute)


def test_m5_aggregation_basic():
    # 10 consecutive synthetic M1 candles from 08:00 (UTC), each +1 up.
    base = datetime(2026, 1, 5, 8, 0)
    candles = []
    price = 100.0
    for i in range(10):
        candles.append(make_candle(_m1_utc_minute(base, i), price, price + 1, price - 1, price + 0.5))
        price += 1.0

    m5 = aggregate_candles(candles, 5, include_last_partial=False)
    # 10 M1 = two full 5-min buckets; the third (partial) bucket is dropped.
    assert len(m5) == 2
    assert m5[0].open == 100.0
    assert m5[0].close == 104.5
    assert m5[0].high == 105.0
    assert m5[0].low == 99.0
    assert m5[1].open == 105.0


def test_aggregation_never_returns_open_bucket():
    base = datetime(2026, 1, 5, 8, 0)
    candles = [make_candle(_m1_utc_minute(base, i), 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(3)]
    h1 = aggregate_candles(candles, 60, include_last_partial=False)
    assert h1 == []  # the single partial H1 bucket must be excluded


def test_h1_closed_bucket_only_after_completion():
    base = datetime(2026, 1, 5, 8, 0)
    candles = [make_candle(_m1_utc_minute(base, i), 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(60)]
    h1 = aggregate_candles(candles, 60, include_last_partial=False)
    # 60 minutes complete the bucket -> one closed H1.
    assert len(h1) == 1
    assert h1[0].t_utc == base


def test_barset_incremental_and_timeframes():
    bs = BarSet(server_offset_hours=0)
    base_utc = datetime(2026, 1, 5, 8, 0)
    for i in range(300):  # 5 hours of M1
        bs.add(make_candle(_m1_utc_minute(base_utc, i), 100 + i * 0.01,
                           100 + i * 0.01 + 1, 100 + i * 0.01 - 1, 100 + i * 0.01))
    m5 = bs.m5()
    m15 = bs.m15()
    h1 = bs.h1()
    assert len(m5) == 60
    assert len(m15) == 20
    assert len(h1) == 5
    # BarSet exposes only closed candles.
    assert bs.last_closed().t_utc == _m1_utc_minute(base_utc, 299)


def test_barset_rejects_duplicate_and_out_of_order():
    bs = BarSet(server_offset_hours=0)
    base_utc = datetime(2026, 1, 5, 8, 0)
    c1 = make_candle(base_utc, 1, 2, 0, 1.5)
    c2 = make_candle(base_utc + timedelta(minutes=1), 1, 2, 0, 1.5)
    bs.add(c1)
    bs.add(c2)
    bs.add(c2)  # idempotent re-feed of the same closed candle is ignored
    assert len(bs) == 2
    import pytest
    with pytest.raises(ValueError):
        bs.add(c1)  # strictly older candle


def test_tf_minutes_map_has_all_required():
    assert set(TIMEFRAME_MINUTES) == {"M1", "M5", "M15", "H1"}
    assert TIMEFRAME_MINUTES["H1"] == 60
