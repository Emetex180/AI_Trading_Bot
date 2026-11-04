"""Model 2: synthetic closed M1 replay of M15 purge -> M5 CISD.

No broker or network calls. The fixtures keep pool formation, purge and
confirmation separate so a passing negative test cannot hide a broken setup.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from config import get_settings
from database.models import Base
from database.repository import Repository, ensure_schema
from trading import model2_sessions as m2, sessions as sess, time_utils as tu
from trading.asset_manager import Asset
from trading.model2 import (MODEL_1, MODEL_2, Model2Strategy, ENTRY_TRIGGERED,
                            INVALIDATED, EXPIRED, WAITING_FOR_CISD, CISD_CONFIRMED,
                            is_model_2, model_label, model_meta)
from trading.signal_engine import Signal, build_setup_id
from trading.sessions import hm_to_minutes
from conftest import c

MON, TUE = 5, 6
ASSET = Asset(name="TEST", broker_symbol="TEST", enabled=True, digits=2)


def _settings(**kw):
    base = replace(get_settings(), auto_trading=False, auto_trading_override=None,
                   model_1_enabled=True, asset_env={}, ai_enabled=False, telegram_enabled=False,
                   telegram_bot_token="", telegram_chat_id="", telegram_channel_id="",
                   min_rr=1.5, risk_percent=1, model_2_enabled=True,
                   model_2_lookback_days=3, max_cisd_candles=8,
                   sl_min_offset_ticks=2, scanner_poll_interval_ms=5000,
                   trading_days=["mon", "tue", "wed", "thu", "fri"])
    return replace(base, **kw)


def _strategy(asset=None, log=None, **kw):
    return Model2Strategy(asset or ASSET, settings=_settings(**kw), log=log)


def _q(day, hh, mm, o, h, l, cl):
    return c(datetime(2026, 1, day, hh, mm), o, h, l, cl)


def _session(day, start_hm, end_hm, *, base, high, low, spread=1):
    start, end = hm_to_minutes(start_hm), hm_to_minutes(end_hm)
    n = end - start
    return [c(datetime(2026, 1, day) + timedelta(minutes=start + i), base,
              high if i == n // 3 else base + spread,
              low if i == 2 * n // 3 else base - spread, base) for i in range(n)]


def _bucket(day, hh, mm, *, o, h, l, cl, base, spread=1, minutes=15):
    assert l <= base - spread and base + spread <= h
    start = datetime(2026, 1, day, hh, mm)
    return [c(start, o, h, l, cl)] + [
        c(start + timedelta(minutes=i), base, base + spread, base - spread, base)
        for i in range(1, minutes)]


def _monday_london():
    return _session(MON, "01:00", "06:00", base=2450, high=2500, low=2400)


def _prices(levels):
    return {level.price for level in levels}


def _feed(engine, candles):
    return [signal for bar in candles for signal in engine.feed(bar)]


def _repo():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Repository(session=sessionmaker(bind=engine, expire_on_commit=False)())


def mirror(bar, direction):
    if direction == "buy":
        return bar
    return replace(bar, open=200 - bar.open, high=200 - bar.low,
                   low=200 - bar.high, close=200 - bar.close)


def history(direction="buy", target=99):
    # Nested completed pools; the purge spends only the Asian low (60).
    pool = (_session(MON, "01:00", "06:00", base=85, high=120, low=50)
            + _session(MON, "20:00", "24:00", base=80, high=target, low=60))
    purge = _bucket(TUE, 7, 0, o=65, h=80, l=59.02, cl=65, base=65)
    return [mirror(bar, direction) for bar in pool + purge]


def confirmation(direction="buy", hh=7, mm=15, **kw):
    # Bullish wick/reclaim of 60; entry 75 is INSIDE the M15 range 59.02..80.
    values = dict(o=59.8, h=75, l=59.5, cl=75)
    values.update(kw)
    start = datetime(2026, 1, TUE, hh, mm)
    bars = [c(start + timedelta(minutes=i), values["o"], values["h"],
              values["l"], values["cl"] if i == 4 else values["o"])
            for i in range(5)]
    return [mirror(bar, direction) for bar in bars]


def ready(direction="buy", target=99, **kw):
    engine = _strategy(**kw)
    assert _feed(engine, history(direction, target)) == []
    assert engine.pending_setups == 1
    return engine


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_purge_then_closed_cisd_enters_without_opposite_break(direction):
    engine = ready(direction)
    bars = confirmation(direction)
    assert _feed(engine, bars[:4]) == []
    setup = engine._setups[direction]
    assert setup.confirmation is None
    signal, = _feed(engine, bars[4:])
    assert (signal.direction, signal.entry, signal.sl, signal.tp) == (
        direction, 75 if direction == "buy" else 125,
        59 if direction == "buy" else 141, 99 if direction == "buy" else 101)
    assert signal.rr == 1.5
    assert signal.risk_points == 16 and signal.reward_points == 24
    assert signal.structure_extreme_price == pytest.approx(59.02 if direction == "buy" else 140.98)
    assert signal.cisd_tf == "M5" and signal.fvg_direction == ""
    assert signal.cisd_confirm_time_ny == datetime(2026, 1, TUE, 7, 15)
    assert signal.entry_time_ny == datetime(2026, 1, TUE, 7, 20)
    assert signal.purge_time_ny == datetime(2026, 1, TUE, 7)
    assert signal.model == MODEL_2 and signal.status == "APPROVED"
    assert signal.risk_approved and signal.alert_only
    assert engine.state(direction) == ENTRY_TRIGGERED and engine.pending_setups == 0
    meta = model_meta(signal)
    assert meta["entry_trigger"] == "M5_CISD"
    assert meta["entry_basis"] == "historical_m5_close"
    assert meta["cisd_confirmed_at_ny"] == "2026-01-06T07:20:00"
    assert meta["timeframe"] == "M15" and meta["cisd_timeframe"] == "M5"
    assert meta["liquidity_source_date"] == "2026-01-05"
    assert meta["liquidity_source_session"] == "m2_asian"
    assert meta["liquidity_side"] == ("SELLSIDE" if direction == "buy" else "BUYSIDE")
    assert meta["purge_candle_high"] == pytest.approx(80 if direction == "buy" else 140.98)
    assert meta["purge_candle_low"] == pytest.approx(59.02 if direction == "buy" else 120)
    assert _feed(engine, bars) == []
    assert _feed(engine, confirmation(direction, mm=20)) == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("target,accepted", [(98.99, False), (99, True), (100, True)])
def test_nearest_target_then_rr_not_farther_to_pass(direction, target, accepted):
    engine = ready(direction, target)
    signals = _feed(engine, confirmation(direction))
    assert bool(signals) is accepted
    if accepted:
        assert signals[0].tp == pytest.approx(target if direction == "buy" else 200 - target)
    else:
        assert engine.state(direction) == INVALIDATED  # far pool would pass
    assert engine.pending_setups == 0


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_rr_floor_cannot_be_weakened_and_stricter_policy_applies(direction):
    weak = ready(direction, 98.99, min_rr=0.1)
    assert weak.min_rr == 1.5
    assert _feed(weak, confirmation(direction)) == []
    strict = ready(direction, min_rr=2)
    assert _feed(strict, confirmation(direction)) == []
    assert strict.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("prices", [dict(o=75, cl=65), dict(l=60), dict(cl=60),
                                    dict(o=65, l=64, h=85, cl=82)])
def test_wrong_cisd_or_opposite_break_alone_cannot_enter(direction, prices):
    engine = ready(direction, target=105)
    assert _feed(engine, confirmation(direction, **prices)) == []
    assert engine.state(direction) == WAITING_FOR_CISD
    assert engine.pending_setups == 1


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_pre_purge_cisd_is_not_revisited(direction):
    engine = _strategy()
    rows = history(direction)
    engine.warm(rows[:-15])
    purge = rows[-15:]
    purge[-5:] = confirmation(direction, mm=10)
    assert _feed(engine, purge) == []  # valid reclaim, but M15 not closed yet
    assert engine.pending_setups == 1
    assert _feed(engine, confirmation(direction))  # later M5 really confirms


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_partial_m5_and_m15_candles_never_confirm(direction):
    engine = ready(direction)
    bars = confirmation(direction)
    assert _feed(engine, bars[:2] + bars[3:]) == []
    assert engine._setups[direction].confirmation is None
    assert _feed(engine, confirmation(direction, mm=20))
    engine = _strategy()
    rows = history(direction)
    engine.warm(rows[:-15])
    assert _feed(engine, rows[-15:-10] + rows[-9:]) == []
    assert engine.pending_setups == 0 and len(engine._consumed) == 1


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_stop_breach_invalidates_without_reanchoring_or_reviving(direction):
    engine = ready(direction)
    setup = engine._setups[direction]
    assert _feed(engine, confirmation(direction, l=58)) == []
    assert engine.state(direction) == INVALIDATED
    assert setup.sl_price == (59 if direction == "buy" else 141)
    assert _feed(engine, confirmation(direction, mm=20)) == []
    assert len(engine._consumed) == 1


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_stop_uses_tick_grid_and_existing_offset_not_cisd_extreme(direction):
    from trading.symbol_spec import SymbolSpec
    asset = replace(ASSET, spec=SymbolSpec("TEST", digits=2, tick_size=0.25))
    engine = _strategy(asset=asset)
    engine.warm(history(direction, 105))
    signal, = _feed(engine, confirmation(direction))
    assert signal.sl == (58.5 if direction == "buy" else 141.5)
    assert signal.sl % 0.25 == 0
    assert signal.structure_extreme_price != signal.sl


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_consumed_targets_are_unavailable_and_missing_targets_reject(direction):
    for high, target in [(100, 120), (121, None)]:
        engine = _strategy()
        rows = history(direction)
        engine.warm(rows[:-15])
        engine.feed(mirror(_q(TUE, 6, 30, 90, high, 85, 90), direction))
        assert engine.pending_setups == 0
        _feed(engine, rows[-15:])
        signals = _feed(engine, confirmation(direction))
        if target:
            assert signals[0].tp == (120 if direction == "buy" else 80)
        else:
            assert signals == [] and engine.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_target_taken_during_cisd_is_not_selected_with_future_information(direction):
    engine = ready(direction)
    signals = _feed(engine, confirmation(direction, h=100))
    assert signals[0].tp == (120 if direction == "buy" else 80)
    # At confirmation close, 99 has been spent; before that close no signal exists.


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_cisd_close_at_session_end_expires(direction):
    engine = ready(direction, max_cisd_candles=100)
    assert _feed(engine, confirmation(direction, hh=10, mm=55)) == []
    assert engine.state(direction) == EXPIRED
    assert _feed(engine, confirmation(direction, hh=11, mm=0)) == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_cisd_deadline_and_clock_driven_expiry(direction):
    engine = ready(direction, max_cisd_candles=1)
    assert _feed(engine, confirmation(direction))  # last permitted close is valid
    engine = ready(direction, max_cisd_candles=1)
    assert _feed(engine, confirmation(direction, mm=20)) == []
    assert engine.state(direction) == EXPIRED
    engine = ready(direction)
    engine.expire_at(tu.ny_to_utc(datetime(2026, 1, TUE, 11)))
    assert engine.state(direction) == EXPIRED


def test_the_five_windows_are_shared_and_obey_all_boundaries():
    expected = {"m2_asian": (1200, 1440), "m2_london": (60, 360),
                "m2_ny_am": (420, 660), "m2_lunch": (660, 780), "m2_ny_pm": (780, 1020)}
    assert {w.key: (w.start, w.end) for w in m2.MODEL_2_SESSIONS} == expected
    for window in m2.MODEL_2_SESSIONS:
        assert window.contains(window.start) and window.contains(window.end - 1)
        assert not window.contains(window.end)
    for minute in range(1440):
        at = datetime(2026, 1, TUE) + timedelta(minutes=minute)
        assert m2.awake_at(at)[0] == sess.session_activity(at)[0]
    assert not m2.awake_at(datetime(2026, 1, 10, 8))[0]


def test_timezone_uses_dst_and_next_open_is_valid():
    before, after = datetime(2026, 3, 6, 7), datetime(2026, 3, 10, 7)
    assert tu.ny_to_utc(before).hour == 12 and tu.ny_to_utc(after).hour == 11
    assert m2.session_key_at(before) == m2.session_key_at(after) == "m2_ny_am"
    nxt = m2.next_session_start_utc(datetime(2026, 3, 7, 18))
    assert tu.utc_to_ny(nxt) == datetime(2026, 3, 9, 1)


def test_pool_only_includes_completed_sessions_and_rolls_by_calendar_day():
    engine = _strategy(model_2_lookback_days=10)
    engine.warm(_monday_london())
    assert engine.session_levels(datetime(2026, 1, MON, 5, 59)) == []
    assert _prices(engine.session_levels(datetime(2026, 1, MON, 6))) == {2400, 2500}
    assert _prices(engine.session_levels(datetime(2026, 1, 7, 23, 59))) == {2400, 2500}
    assert engine.session_levels(datetime(2026, 1, 8)) == []
    assert engine._pool_days(date(2026, 1, 5)) == [date(2026, 1, 5), date(2026, 1, 4), date(2026, 1, 3)]
    engine.warm(_session(TUE, "01:00", "06:00", base=2450, high=2490, low=2410))
    assert _prices(engine.session_levels(datetime(2026, 1, TUE, 6))) == {2400, 2410, 2490, 2500}


def test_partial_warmup_session_cannot_supply_liquidity():
    engine = _strategy()
    engine.warm(_monday_london()[15:])
    assert engine.session_levels(datetime(2026, 1, MON, 6)) == []


@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1)])
def test_dst_session_collection_on_utc_timeline(day):
    from trading.bars import make_candle
    window = m2.MODEL_2_SESSION_INDEX["m2_london"]
    start, end = m2.session_bounds_ny(day, window)
    first, stop = tu.ny_to_utc(start), tu.ny_to_utc(end)
    count = int((stop - first).total_seconds() // 60)
    assert count == (240 if day.month == 3 else 360)
    engine = _strategy(trading_days=["sun"])
    for i in range(count):
        engine.feed(make_candle(first + timedelta(minutes=i), 100,
                               110 if i == 60 else 101, 90 if i == count - 30 else 99, 100))
    assert _prices(engine.session_levels(end)) == {90, 110}
    assert engine.session_levels(end - timedelta(minutes=1)) == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_outside_purge_is_spent_and_not_reused(direction):
    engine = _strategy()
    rows = history(direction)
    engine.warm(rows[:-15])
    engine.feed(mirror(_q(TUE, 6, 30, 65, 75, 59, 65), direction))
    assert engine.pending_setups == 0 and len(engine._consumed) == 1
    assert len(engine.session_levels()) == 3
    assert _feed(engine, rows[-15:] + confirmation(direction)) == []
    assert engine.pending_setups == 0


def test_touch_does_not_purge_and_cascade_creates_one_setup():
    engine = _strategy()
    engine.warm(history()[:-15])
    _feed(engine, _bucket(TUE, 7, 0, o=65, h=75, l=60, cl=65, base=65))
    assert engine.pending_setups == 0
    # Two available lows (60 and 50) purged; one direction / one setup.
    _feed(engine, _bucket(TUE, 7, 15, o=65, h=75, l=49, cl=65, base=65))
    assert engine.pending_setups == 1
    assert engine._setups["buy"].level.price == 50
    assert len(engine._consumed) == 2
    signals = _feed(engine, confirmation(mm=30, o=49.8, l=49.5, h=60, cl=60))
    assert len(signals) == 1


@pytest.mark.parametrize("asset_disabled", [False, True])
def test_model_2_disabled_independently(asset_disabled):
    engine = (_strategy(asset=replace(ASSET, overrides={"model_2_enabled": "false"}))
              if asset_disabled else _strategy(model_2_enabled=False))
    assert _feed(engine, history() + confirmation()) == []
    assert not engine._consumed and engine.pending_setups == 0


def test_model_1_identity_and_fingerprints_unchanged():
    stamp = datetime(2026, 1, TUE, 12)
    bare = build_setup_id("TEST", "buy", stamp, stamp, stamp)
    assert bare == build_setup_id("TEST", "buy", stamp, stamp, stamp, model="")
    assert bare != build_setup_id("TEST", "buy", stamp, stamp, stamp, model=MODEL_2)
    signal = Signal(asset="TEST", direction="buy", entry=1, sl=0.5, tp=2,
                    entry_time_utc=stamp, entry_time_ny=stamp)
    assert signal.model == model_label(signal) == MODEL_1
    assert not is_model_2(signal) and model_meta(signal) == {}


def test_existing_database_model_columns_still_compatible():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE signals (id INTEGER PRIMARY KEY, fingerprint VARCHAR(64))"))
    ensure_schema(engine)
    assert {"model", "model_meta"} <= {col["name"] for col in inspect(engine).get_columns("signals")}
    assert "ix_signals_model" in {ix["name"] for ix in inspect(engine).get_indexes("signals")}
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO signals (fingerprint) VALUES ('abc')"))
        assert tuple(conn.execute(text("SELECT model, model_meta FROM signals")).one()) == ("MODEL_1", "{}")
