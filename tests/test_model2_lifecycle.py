"""Model 2 corrections: causal lifecycle, quote entries and durable consumption.

Synthetic M1/quotes, in-memory SQLite and disabled execution only. No MT5.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json

import pytest

from conftest import c
from test_model2 import (ASSET, MON, TUE, _bucket, _feed, _monday_london,
                         _prices, _q, _repo, _session, _settings, _strategy)
from trading import time_utils as tu
from trading.executor import Executor
from trading.model2 import (ENTRY_TRIGGERED, EXPIRED, INVALIDATED, Model2Strategy,
                            model_meta)
from trading.model2_scanner import Model2Scanner


def mirror(candle, direction):
    if direction == "buy":
        return candle
    return replace(candle, open=200 - candle.open, high=200 - candle.low,
                   low=200 - candle.high, close=200 - candle.close)


def history(direction="buy", target=99):
    # Nested ranges: neither Monday session consumes the other's extremes.
    pool = (_session(MON, "01:00", "06:00", base=85, high=120, low=50)
            + _session(MON, "20:00", "24:00", base=80, high=target, low=60))
    purge = _bucket(TUE, 7, 0, o=65, h=75, l=59, cl=65, base=65)
    return [mirror(bar, direction) for bar in pool + purge]


def ready(direction="buy", target=99, **settings):
    engine = _strategy(**settings)
    _feed(engine, history(direction, target))
    assert engine.pending_setups == 1
    return engine


def break_bar(direction="buy", **prices):
    values = dict(o=70, h=76, l=65, cl=70)
    values.update(prices)
    return mirror(_q(TUE, 7, 15, **values), direction)


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("target,accepted", [(98, False), (99, True), (100, True)])
def test_nearest_target_then_rr_not_farther_to_pass(direction, target, accepted):
    engine = ready(direction, target)
    signals = engine.feed(break_bar(direction))
    assert bool(signals) is accepted
    if accepted:
        sig = signals[0]
        expected = target if direction == "buy" else 200 - target
        assert sig.tp == expected
        assert sig.entry == (75 if direction == "buy" else 125)
        assert sig.sl == (59 if direction == "buy" else 141)
        assert sig.rr == pytest.approx((target - 75) / 16)
        assert engine.state(direction) == ENTRY_TRIGGERED
    else:
        # The far target (120 / 80) would pass: it must NOT be substituted.
        assert engine.state(direction) == INVALIDATED
    assert engine.pending_setups == 0


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_rr_floor_cannot_be_weakened(direction):
    engine = ready(direction, target=98, min_rr=0.1)
    assert engine.min_rr == 1.5
    assert engine.feed(break_bar(direction)) == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_break_does_not_require_close_beyond_trigger(direction):
    engine = ready(direction)
    bar = break_bar(direction)  # closes back inside the purge candle
    assert engine.feed(bar)[0].direction == direction
    assert engine.feed(bar) == []
    assert engine.feed(replace(bar, t_utc=bar.t_utc + timedelta(minutes=1),
                               t_ny=bar.t_ny + timedelta(minutes=1))) == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_touch_is_not_an_entry(direction):
    engine = ready(direction)
    assert engine.feed(break_bar(direction, h=75)) == []
    assert engine.pending_setups == 1


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_gap_entry_is_not_filled_at_old_trigger(direction):
    engine = ready(direction)
    # Entry at 76 (124 SELL), not 75 (125 SELL): RR drops below 1.50.
    assert engine.feed(break_bar(direction, o=76, h=77, l=76, cl=76)) == []
    assert engine.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_m1_spanning_stop_and_trigger_has_unknown_sequence(direction):
    engine = ready(direction)
    assert engine.feed(break_bar(direction, l=58)) == []
    assert engine.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_consumed_levels_are_not_tp_candidates(direction):
    engine = _strategy()
    rows = history(direction, target=99)
    # Before Tuesday's purge, outside-session price takes just the near target.
    pool, purge = rows[:-15], rows[-15:]
    engine.warm(pool)
    sweep = mirror(_q(TUE, 6, 30, 90, 100, 85, 90), direction)
    engine.feed(sweep)
    assert engine.pending_setups == 0
    _feed(engine, purge)
    signals = engine.feed(break_bar(direction))
    assert len(signals) == 1
    assert signals[0].tp == (120 if direction == "buy" else 80)


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_missing_targets_reject(direction):
    engine = _strategy()
    rows = history(direction)
    engine.warm(rows[:-15])
    engine.feed(mirror(_q(TUE, 6, 30, 100, 121, 85, 100), direction))
    _feed(engine, rows[-15:])
    assert engine.feed(break_bar(direction)) == []
    assert engine.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_outside_purge_is_spent_no_setup_and_no_reuse(direction):
    engine = _strategy()
    rows = history(direction)
    engine.warm(rows[:-15])
    engine.feed(mirror(_q(TUE, 6, 30, 65, 75, 59, 65), direction))
    assert engine.pending_setups == 0
    consumed = dict(engine._consumed)
    assert len(consumed) == 1
    assert len(engine.session_levels()) == 3  # unrelated levels survive
    _feed(engine, rows[-15:])  # same level breached in an active session
    assert engine.feed(break_bar(direction)) == []
    assert engine.pending_setups == 0
    assert engine._consumed == consumed


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_purge_candle_is_not_replaced_by_repeated_sweep(direction):
    engine = ready(direction, target=105)
    setup = engine._setups[direction]
    later = _bucket(TUE, 7, 15, o=65, h=70, l=58, cl=65, base=65)
    _feed(engine, [mirror(bar, direction) for bar in later])
    assert engine._setups[direction] is setup
    assert setup.purge_time_ny == datetime(2026, 1, TUE, 7)
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 30))
    bid, ask = ((75.1, 75.2) if direction == "buy" else (124.8, 124.9))
    sig = engine.feed_quote(t_utc=stamp, bid=bid, ask=ask)[0]
    assert sig.sl == (59 if direction == "buy" else 141)
    assert model_meta(sig)["entry_trigger"] == (75 if direction == "buy" else 125)


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_quote_entry_uses_executable_price_not_trigger(direction):
    engine = ready(direction, target=105)
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 15, 2))
    bid, ask = ((75.1, 75.2) if direction == "buy" else (124.8, 124.9))
    sig = engine.feed_quote(t_utc=stamp, bid=bid, ask=ask)[0]
    assert sig.entry == (ask if direction == "buy" else bid)
    assert sig.rr == pytest.approx(29.8 / 16.2)
    assert sig.entry_time_utc == stamp
    assert model_meta(sig)["entry_basis"] == "live_quote"
    assert engine.feed_quote(t_utc=stamp, bid=bid, ask=ask) == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_quote_spread_or_slippage_can_fail_rr(direction):
    engine = ready(direction, target=99)
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 15))
    bid, ask = ((75.1, 75.2) if direction == "buy" else (124.8, 124.9))
    assert engine.feed_quote(t_utc=stamp, bid=bid, ask=ask) == []
    assert engine.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("sl", [75, 76, float("nan"), float("inf"), -1])
def test_invalid_sl_is_rejected_at_entry(direction, sl):
    engine = ready(direction)
    setup = engine._setups[direction]
    if direction == "buy":
        setup.purge_low = sl
    else:
        setup.purge_high = 200 - sl
    assert engine._build_signal(setup, break_bar(direction)) is None


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_expiry_at_session_end_without_a_price_update(direction):
    engine = ready(direction)
    engine.expire_at(tu.ny_to_utc(datetime(2026, 1, TUE, 11)))
    assert engine.state(direction) == EXPIRED
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 11, 1))
    bid, ask = ((75.2, 75.3) if direction == "buy" else (124.7, 124.8))
    assert engine.feed_quote(t_utc=stamp, bid=bid, ask=ask) == []
    assert engine.pending_setups == 0
    assert len(engine._consumed) == 1


def test_calendar_pool_rolls_without_waiting_for_another_m15_bar():
    engine = _strategy()
    engine.warm(_monday_london())
    assert _prices(engine.session_levels(datetime(2026, 1, 7, 23, 59))) == {2400, 2500}
    assert engine.session_levels(datetime(2026, 1, 8)) == []
    assert engine.session_levels(datetime(2026, 1, MON, 5, 59)) == []
    assert _prices(engine.session_levels(datetime(2026, 1, MON, 6))) == {2400, 2500}


def test_current_day_completed_sessions_are_included_not_future_sessions():
    engine = _strategy()
    engine.warm(_monday_london()
                + _session(TUE, "01:00", "06:00", base=2450, high=2490, low=2410))
    levels = engine.session_levels(datetime(2026, 1, TUE, 6))
    assert {lvl.source_date for lvl in levels} == {"2026-01-05", "2026-01-06"}
    assert _prices(levels) == {2400, 2410, 2490, 2500}
    assert {lvl.source_session for lvl in levels} == {"m2_london"}


@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1)])
def test_dst_transition_session_collection_on_utc_timeline(day):
    from trading.model2_sessions import MODEL_2_SESSION_INDEX, session_bounds_ny
    window = MODEL_2_SESSION_INDEX["m2_london"]
    start, end = session_bounds_ny(day, window)
    first, stop = tu.ny_to_utc(start), tu.ny_to_utc(end)
    count = int((stop - first).total_seconds() // 60)
    assert count == (240 if day.month == 3 else 360)
    engine = _strategy(trading_days=["sun"])
    for i in range(count):
        stamp = first + timedelta(minutes=i)
        from trading.bars import make_candle
        engine.feed(make_candle(stamp, 100, 110 if i == 60 else 101,
                                 90 if i == count - 30 else 99, 100))
    assert _prices(engine.session_levels(end)) == {90, 110}
    assert engine.session_levels(end - timedelta(minutes=1)) == []


def test_partial_warmup_session_and_partial_purge_are_not_used():
    engine = _strategy()
    engine.warm(_monday_london()[15:])
    assert engine.session_levels(datetime(2026, 1, MON, 6)) == []
    engine = _strategy()
    engine.warm(history()[:-15])
    partial = history()[-15:]
    _feed(engine, partial[:5] + partial[6:])
    assert engine.pending_setups == 0
    assert len(engine._consumed) == 1


def test_lookback_setting_cannot_reintroduce_trading_days_or_a_fourth_day():
    engine = _strategy(model_2_lookback_days=10)
    assert engine._pool_days(date(2026, 1, 6)) == [
        date(2026, 1, 6), date(2026, 1, 5), date(2026, 1, 4)]


@pytest.mark.parametrize("asset_disabled", [False, True])
def test_model_2_can_be_disabled_independently(asset_disabled):
    engine = (Model2Strategy(replace(ASSET, overrides={"model_2_enabled": "false"}),
                             settings=_settings()) if asset_disabled
              else _strategy(model_2_enabled=False))
    assert _feed(engine, history() + [break_bar()]) == []
    assert engine.pending_setups == 0
    assert not engine._consumed


def scanner(repo, **kwargs):
    return Model2Scanner(ASSET, settings=_settings(auto_trading_override=None),
                         repo=repo, **kwargs)


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("outcome", ["expired", "outside", "triggered"])
def test_restart_does_not_restore_consumed_entry(direction, outcome):
    repo = _repo()
    first = scanner(repo)
    rows = history(direction, target=105)
    if outcome == "outside":
        first.warm(rows[:-15])
        first.step([mirror(_q(TUE, 6, 30, 65, 75, 59, 65), direction)])
    else:
        first.warm(rows)
        if outcome == "expired":
            first.expire_at(tu.ny_to_utc(datetime(2026, 1, TUE, 11)))
        else:
            assert first.step([break_bar(direction)]) == 1
    persisted = json.loads(repo.recent_events_by_source(first._state_source, limit=1)[0].message)
    assert len(persisted["consumed"]) == 1
    second = scanner(repo)
    # Replay the same warm-up window, then the same breach. Tombstones win.
    second.warm(rows)
    assert second.step([break_bar(direction)]) == 0
    assert second.engine.pending_setups == 0
    assert repo.count_signals() == (1 if outcome == "triggered" else 0)


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_live_scanner_quote_pipeline_never_sends_with_auto_trading_false(direction, monkeypatch):
    repo = _repo()
    sc = scanner(repo, live_quotes=True)
    monkeypatch.setattr(Executor, "_order_send", lambda *a, **k: pytest.fail("order_send called"))
    sc.warm(history(direction, target=105))
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 15, 2))
    bid, ask = ((75.1, 75.2) if direction == "buy" else (124.8, 124.9))
    quote = dict(time_utc=stamp, bid=bid, ask=ask)
    assert sc.feed_quote(quote, now_utc=stamp) == 1
    assert repo.count_signals() == 1
    assert repo.recent_trades()[0].reason == "auto_trading_disabled"
    assert sc.feed_quote(quote, now_utc=stamp) == 0
    # A new scanner also checks persisted signal fingerprints before alerting.
    replay = ready(direction, target=105).feed_quote(t_utc=stamp, bid=bid, ask=ask)[0]
    assert scanner(repo)._on_signal(replay) is False
    assert len(repo.recent_trades()) == 1


def test_live_backfill_cannot_place_historical_entries():
    repo = _repo()
    sc = scanner(repo, live_quotes=True)
    sc.warm(history(target=105))
    assert sc.step([break_bar()]) == 0
    assert repo.count_signals() == 0
    assert repo.recent_trades() == []
    assert sc.engine.pending_setups == 0


@pytest.mark.parametrize("age", [-1, 30])
def test_future_or_stale_quotes_do_not_trigger(age):
    sc = scanner(_repo(), live_quotes=True)
    sc.warm(history(target=105))
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 15))
    assert sc.feed_quote(dict(time_utc=stamp, bid=75.1, ask=75.2),
                         now_utc=stamp + timedelta(seconds=age)) == 0
    assert sc.engine.pending_setups == 1


def test_persistence_failure_blocks_pipeline(monkeypatch):
    repo = _repo()
    sc = scanner(repo)
    sc.warm(history()[:-15])
    monkeypatch.setattr(repo, "log_event", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        sc.step(history()[-15:])
    assert repo.count_signals() == 0
    assert repo.recent_trades() == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_exact_rr_with_decimal_prices_also_passes_execution_risk_recheck(direction):
    def scale(bar):
        return replace(bar, open=bar.open / 100, high=bar.high / 100,
                       low=bar.low / 100, close=bar.close / 100)
    engine = _strategy()
    engine.warm([scale(bar) for bar in history(direction)])
    signal = engine.feed(scale(break_bar(direction)))[0]
    assert signal.rr == 1.5
    sc = scanner(_repo(), engine=engine)
    assert sc.executor.risk.approve(signal.entry, signal.sl, signal.tp, direction).approved


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("delay,reason", [(0, ""), (30, "model2_quote_expired"),
                                          (4 * 3600, "model2_session_expired")])
def test_final_send_guard_checks_session_and_quote_age(direction, delay, reason, monkeypatch):
    engine = ready(direction, target=105)
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 15))
    bid, ask = ((75.1, 75.2) if direction == "buy" else (124.8, 124.9))
    signal = engine.feed_quote(t_utc=stamp, bid=bid, ask=ask)[0]
    sc = scanner(_repo(), engine=engine)
    now = stamp + timedelta(seconds=delay)
    monkeypatch.setattr(tu, "now_utc", lambda: now)
    reached = []
    monkeypatch.setattr(Executor, "_send", lambda *a: reached.append(True) or "mock_send")
    # Exercise only the final guard with an inert parent, not an enabled order.
    result = sc.executor._send(signal, "TEST", 1, now)
    if reason:
        assert result.reason == reason
        assert reached == []
    else:
        assert result == "mock_send" and reached == [True]


def test_historical_signal_cannot_reach_order_funnel(monkeypatch):
    engine = ready(target=105)
    signal = engine.feed(break_bar())[0]
    sc = scanner(_repo(), engine=engine)
    monkeypatch.setattr(Executor, "_send", lambda *a: pytest.fail("historical send"))
    assert sc.executor._send(signal, "TEST", 1, signal.entry_time_utc).reason == "model2_requires_live_quote"


def test_runner_routes_existing_quote_to_real_model2_without_touching_model1(tmp_path, monkeypatch):
    from test_runner import _env, _jobs, _FakeClient, _FakeMarket, _FakeScanner, _wait_for
    settings, maker = _env(tmp_path, monkeypatch)
    settings = replace(settings, model_2_enabled=True, auto_trading=False,
                       auto_trading_override=None, ai_enabled=False, telegram_enabled=False,
                       scanner_poll_interval_ms=50, min_rr=1.5)
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 15, 2))
    monkeypatch.setattr(tu, "now_utc", lambda: stamp)
    monkeypatch.setattr(tu, "now_ny", lambda: tu.utc_to_ny(stamp))
    monkeypatch.setattr(Executor, "_order_send", lambda *a: pytest.fail("live order"))

    class QuoteClient(_FakeClient):
        def symbol_tick(self, symbol):
            self.tick_calls.append(symbol)
            return dict(time_utc=stamp, bid=75.1, ask=75.2)

    client = QuoteClient()
    market = _FakeMarket(candles=history(target=105))
    jobs = _jobs(settings, maker, client_factory=lambda s: client,
                 market_factory=lambda c, s: market)
    try:
        assert jobs.start_live()["ok"]
        assert _wait_for(lambda: jobs.live_state()["signals_session"] == 1)
    finally:
        jobs.stop_live(timeout=5)
    assert len(_FakeScanner.instances) == 1
    assert _FakeScanner.instances[0].steps == market.polls
    assert len(client.tick_calls) == market.polls
    from database.repository import Repository
    with Repository(session=maker()) as repo:
        assert repo.count_signals() == 1
        assert repo.recent_trades()[0].reason == "auto_trading_disabled"
