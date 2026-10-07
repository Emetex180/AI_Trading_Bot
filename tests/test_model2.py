"""Model 2 tests — sessions, liquidity causality, the purge, entry, and expiry.

Everything here is synthetic and MT5-free, like the rest of the suite. The
fixture is one Monday of session ranges and one Tuesday morning that trades
through them:

* **Monday 2026-01-05** (a trading day). London runs 01:00-05:59 and prints a
  2500 high and a 2400 low; the Asian session runs 20:00-23:59 and prints a 2520
  high and a 2300 low. Those four prices are the whole liquidity pool Tuesday
  can draw on.
* **Tuesday 2026-01-06** (also a trading day). The 07:00 NY AM bucket is the
  purge candle, and a 07:15 M1 candle is the break that fills the entry.

Session ranges are fed **minute by minute** rather than as a skeleton of
quarter-hour candles. A 15-minute bucket only closes when the *next* bucket's
first minute arrives, so a sparse feed would leave buckets hanging open across
the gaps and finalise them at an unrelated instant later — which would make the
purge fire at the wrong time for reasons that have nothing to do with the model.

Two consequences of the model's own causality are asserted directly, because
they are behaviour a reader might otherwise mistake for a bug:

* A purge is only known once its candle has **closed**, so feeding the purge
  bucket alone creates nothing; the premise appears on the next candle.
* A session's extremes enter the pool only once that session has **closed**, so
  the Asian range is not available at 23:59 — one minute before the window ends.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from ai.analyzer import AiAnalyzer
from backtesting.engine import BacktestRunner
from config import get_settings
from database.models import Base
from database.repository import Repository, ensure_schema
from notifications.telegram import TelegramNotifier, format_signal_alert
from trading import model2_sessions as m2
from trading import time_utils as tu
from trading.asset_manager import Asset
from trading.executor import Executor
from trading.model2 import (BUYSIDE, ENTRY_TRIGGERED, EXPIRED, INVALIDATED,
                            MODEL_1, MODEL_2, SELLSIDE, WAITING,
                            WAITING_FOR_PURGE_CANDLE_BREAK, Model2Strategy,
                            is_model_2, model_label, model_meta)
from trading.model2_scanner import Model2Scanner
from trading.sessions import hm_to_minutes
from trading.signal_engine import Signal, build_setup_id

from conftest import c

# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #
MON, TUE = 5, 6                  # 2026-01-05 (Monday), 2026-01-06 (Tuesday)
LONDON_HIGH, LONDON_LOW = 2500.0, 2400.0
ASIAN_HIGH, ASIAN_LOW = 2520.0, 2300.0

ASSET = Asset(name="TEST", broker_symbol="TEST", enabled=True, digits=2)


class _FakeSend:
    ok = True


def _settings(**kw):
    """Hermetic settings: never inherit the developer's real ``.env``."""
    base = replace(get_settings(), auto_trading=False, ai_enabled=False,
                   telegram_enabled=False, telegram_bot_token="",
                   telegram_chat_id="", telegram_channel_id="",
                   min_rr=1.5, risk_percent=1.0,
                   model_2_enabled=True, model_2_lookback_days=3,
                   trading_days=["mon", "tue", "wed", "thu", "fri"])
    return replace(base, **kw)


def _strategy(asset=None, log=None, **kw):
    return Model2Strategy(asset or ASSET, settings=_settings(**kw), log=log)


def _q(day, hh, mm, o, h, l, cl):
    """One closed M1 candle at a NY minute."""
    return c(datetime(2026, 1, day, hh, mm), o, h, l, cl)


def _session(day, start_hm, end_hm, *, base, high, low, spread=1.0):
    """Every M1 candle of one NY session, carrying exactly these extremes."""
    start, end = hm_to_minutes(start_hm), hm_to_minutes(end_hm)
    n = end - start
    peak, trough = n // 3, (2 * n) // 3
    t = datetime(2026, 1, day) + timedelta(minutes=start)
    out = []
    for i in range(n):
        out.append(c(t, base,
                     high if i == peak else base + spread,
                     low if i == trough else base - spread,
                     base))
        t += timedelta(minutes=1)
    return out


def _bucket(day, hh, mm, *, o, h, l, cl, base, spread=1.0, minutes=15):
    """A full M15 bucket: the given M1 first, then a band inside its range.

    The band sits strictly inside ``[l, h]`` so the bucket's extremes are exactly
    the ones asked for — which is what makes the purge candle's high and low
    predictable in the assertions below.
    """
    assert l <= base - spread and base + spread <= h, \
        "the filler band must sit inside the purge candle's range"
    t = datetime(2026, 1, day, hh, mm)
    out = [c(t, o, h, l, cl)]
    for _ in range(1, minutes):
        t += timedelta(minutes=1)
        out.append(c(t, base, base + spread, base - spread, base))
    return out


def _monday_london():
    """Monday's London range *alone* — a pool nothing on the day can purge.

    Feed to the tests that are about detection rather than about the pool. With
    the evening range absent, no candle on Monday trades through either of these
    two levels, so the state machine is still in ``WAITING`` when Tuesday's
    candles arrive and "nothing happened" is an unambiguous claim. The full
    :func:`_monday_pool` cannot make that claim: its Asian range sweeps London's
    high and low as it forms, which is correct behaviour (those are live levels
    once London closes) but leaves premises behind that expire at midnight.
    """
    return _session(MON, "01:00", "06:00", base=2450.0,
                    high=LONDON_HIGH, low=LONDON_LOW)


def _monday_pool():
    """Monday's London and Asian ranges — everything Tuesday can purge."""
    return (_monday_london()
            + _session(MON, "20:00", "24:00", base=2450.0,
                       high=ASIAN_HIGH, low=ASIAN_LOW))


def _feed(strategy, candles):
    signals = []
    for candle in candles:
        signals += strategy.feed(candle)
    return signals


def _prices(levels):
    return {lvl.price for lvl in levels}


def _repo():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return Repository(session=maker())


# --------------------------------------------------------------------------- #
# Sessions and the New York clock
# --------------------------------------------------------------------------- #
def test_the_five_windows_are_the_windows_the_spec_names():
    expected = {
        "m2_asian": ("Asian", 20 * 60, 24 * 60),
        "m2_london": ("London", 1 * 60, 6 * 60),
        "m2_ny_am": ("NY AM", 7 * 60, 11 * 60),
        "m2_lunch": ("Lunch", 11 * 60, 13 * 60),
        "m2_ny_pm": ("NY PM", 13 * 60, 17 * 60),
    }
    assert {w.key for w in m2.MODEL_2_SESSIONS} == set(expected)
    for window in m2.MODEL_2_SESSIONS:
        label, start, end = expected[window.key]
        assert (window.label, window.start, window.end) == (label, start, end)
        # The spec states the last minute inclusively ("23:59"); the window
        # carries it as an exclusive end, which is the same interval.
        assert window.contains(start) and window.contains(end - 1)
        assert not window.contains(end)


def test_the_windows_open_and_close_on_their_stated_minutes():
    assert m2.session_key_at(datetime(2026, 1, TUE, 1, 0)) == "m2_london"
    assert m2.session_key_at(datetime(2026, 1, TUE, 5, 59)) == "m2_london"
    assert m2.session_key_at(datetime(2026, 1, TUE, 6, 0)) == ""
    assert m2.session_key_at(datetime(2026, 1, TUE, 10, 59)) == "m2_ny_am"
    assert m2.session_key_at(datetime(2026, 1, TUE, 11, 0)) == "m2_lunch"
    assert m2.session_key_at(datetime(2026, 1, TUE, 12, 59)) == "m2_lunch"
    assert m2.session_key_at(datetime(2026, 1, TUE, 13, 0)) == "m2_ny_pm"
    assert m2.session_key_at(datetime(2026, 1, TUE, 16, 59)) == "m2_ny_pm"
    assert m2.session_key_at(datetime(2026, 1, TUE, 17, 0)) == ""
    assert m2.session_key_at(datetime(2026, 1, TUE, 20, 0)) == "m2_asian"
    assert m2.session_key_at(datetime(2026, 1, TUE, 23, 59)) == "m2_asian"


def test_outside_every_window_there_is_no_session():
    assert m2.session_at(datetime(2026, 1, TUE, 7, 0)).key == "m2_ny_am"
    for hh, mm in ((0, 30), (6, 0), (6, 59), (17, 0), (18, 30), (19, 59)):
        assert m2.session_at(datetime(2026, 1, TUE, hh, mm)) is None, f"{hh}:{mm}"


def test_the_new_york_clock_follows_dst_without_a_hardcoded_offset():
    """2026-03-08 is the US spring-forward Sunday.

    NY 07:00 is 12:00 UTC before the shift and 11:00 UTC after it, and it is
    NY AM on both days: the offset changes, the session table does not. A
    hardcoded UTC-4 or UTC-5 would fail one of these two assertions.
    """
    before, after = datetime(2026, 3, 6, 7, 0), datetime(2026, 3, 10, 7, 0)
    assert tu.ny_to_utc(before).hour == 12          # EST, UTC-5
    assert tu.ny_to_utc(after).hour == 11           # EDT, UTC-4
    assert m2.session_key_at(before) == "m2_ny_am"
    assert m2.session_key_at(after) == "m2_ny_am"

    london = m2.MODEL_2_SESSION_INDEX["m2_london"]
    for day in (date(2026, 3, 6), date(2026, 3, 10)):
        open_ny, close_ny = m2.session_bounds_ny(day, london)
        assert (open_ny.hour, open_ny.minute) == (1, 0)
        assert (close_ny.hour, close_ny.minute) == (6, 0)


def test_the_live_gate_stays_awake_for_model_2_windows_model_1_sleeps_through():
    """Model 1's London ends at 05:00 and it has no Lunch window at all."""
    assert m2.awake_at(datetime(2026, 1, TUE, 5, 30))[0] is True
    assert m2.awake_at(datetime(2026, 1, TUE, 11, 30))[0] is True
    assert m2.awake_at(datetime(2026, 1, TUE, 16, 30))[0] is True
    assert m2.awake_at(datetime(2026, 1, TUE, 6, 30))[0] is False
    assert m2.awake_at(datetime(2026, 1, 10, 8, 0))[0] is False      # Saturday
    assert m2.awake_at(datetime(2026, 1, TUE, 11, 30))[1] == "Model 2 · Lunch"


def test_the_next_open_search_lands_inside_a_window_across_the_dst_shift():
    """The search steps the UTC timeline, so DST cannot make it an hour late."""
    nxt = m2.next_session_start_utc(datetime(2026, 3, 7, 18, 0))   # Saturday
    assert nxt is not None
    ny = tu.utc_to_ny(nxt)
    assert (ny.month, ny.day, ny.hour, ny.minute) == (3, 9, 1, 0)
    assert m2.session_key_at(ny) == "m2_london"


# --------------------------------------------------------------------------- #
# The liquidity pool
# --------------------------------------------------------------------------- #
def test_a_session_extreme_is_usable_only_once_its_session_has_closed():
    """Complete sessions only — a range still forming is not liquidity."""
    strategy = _strategy()
    _feed(strategy, _monday_pool())

    # Mid-Asian on the Monday: only London has closed, so only London counts.
    mid_asian = strategy.session_levels(datetime(2026, 1, MON, 21, 0))
    assert _prices(mid_asian) == {LONDON_HIGH, LONDON_LOW}
    assert {lvl.source_session for lvl in mid_asian} == {"m2_london"}

    # One minute before the Asian window ends it is *still* running, and still
    # excluded. 23:59 is not "yesterday's Asian" — it is today's, unfinished.
    assert _prices(strategy.session_levels(datetime(2026, 1, MON, 23, 59))
                   ) == {LONDON_HIGH, LONDON_LOW}

    # The next morning all four levels are available.
    assert _prices(strategy.session_levels(datetime(2026, 1, TUE, 7, 0))) == {
        LONDON_HIGH, LONDON_LOW, ASIAN_HIGH, ASIAN_LOW}


def test_a_session_still_forming_is_never_used_as_liquidity():
    """Tuesday's own NY AM keeps making highs; none of it may enter the pool."""
    strategy = _strategy()
    _feed(strategy, _monday_pool())
    _feed(strategy, _bucket(TUE, 7, 0, o=2540, h=2560, l=2530, cl=2550, base=2545))
    _feed(strategy, [_q(TUE, 7, 15, 2550, 2600, 2545, 2590)])
    _feed(strategy, [_q(TUE, 7, 30, 2590, 2600, 2570, 2590)])

    pool = strategy.session_levels(datetime(2026, 1, TUE, 7, 45))
    assert {lvl.source_date for lvl in pool} == {"2026-01-05"}
    assert _prices(pool) == {LONDON_HIGH, LONDON_LOW, ASIAN_HIGH, ASIAN_LOW}

    # 2560 and 2600 were both traded through by Tuesday's own range, and neither
    # is a premise: the setup is anchored on Monday's Asian high.
    setup = strategy._setups["sell"]
    assert setup.level.price == ASIAN_HIGH
    assert setup.level.source_date == "2026-01-05"
    assert setup.session_key == "m2_ny_am"


def test_the_pool_reaches_back_over_the_previous_three_trading_days():
    """Tuesday 2026-01-06 looks back over Mon 5th, Fri 2nd and Thu 1st."""
    strategy = _strategy()
    assert strategy._pool_days(date(2026, 1, 6)) == [
        date(2026, 1, 6), date(2026, 1, 5), date(2026, 1, 2), date(2026, 1, 1)]
    # The weekend is skipped, not counted: Monday reaches back to the Friday.
    assert strategy._pool_days(date(2026, 1, 5)) == [
        date(2026, 1, 5), date(2026, 1, 2), date(2026, 1, 1), date(2025, 12, 31)]


# --------------------------------------------------------------------------- #
# The purge
# --------------------------------------------------------------------------- #
def test_a_buyside_purge_sells_the_break_of_the_purge_candle():
    """The whole model, end to end, on the winning side."""
    strategy = _strategy()
    _feed(strategy, _monday_pool())

    purge = _bucket(TUE, 7, 0, o=2505, h=2530, l=2502, cl=2520, base=2520)
    _feed(strategy, purge)
    # The purge candle is not a premise until it has closed, and it closes when
    # the next bucket's first minute arrives — not one candle earlier.
    assert strategy.pending_setups == 0

    # 07:15 closes the bucket and holds inside it: the premise now exists, its
    # candle's extremes are final, and it is waiting for the break.
    assert _feed(strategy, [_q(TUE, 7, 15, 2520, 2527, 2510, 2520)]) == []
    setup = strategy._setups["sell"]
    assert setup.state == WAITING_FOR_PURGE_CANDLE_BREAK
    assert setup.level.price == ASIAN_HIGH
    assert (setup.purge_high, setup.purge_low) == (2530.0, 2502.0)
    assert setup.purge_time_ny == datetime(2026, 1, TUE, 7, 0)
    assert setup.trigger_price == 2502.0 and setup.sl_price == 2530.0

    signals = _feed(strategy, [_q(TUE, 7, 16, 2520, 2522, 2480, 2490)])
    assert len(signals) == 1
    sig = signals[0]

    # SELL at the purge candle's low; the stop is its high.
    assert (sig.direction, sig.entry, sig.sl) == ("sell", 2502.0, 2530.0)
    # TP is the nearest available session low below the entry, not a fixed
    # multiple: London's 2400 is nearer than the Asian 2300.
    assert (sig.target_kind, sig.target_price, sig.tp) == (
        "SESSION_LOW", LONDON_LOW, LONDON_LOW)
    assert sig.rr == pytest.approx(102 / 28)
    assert sig.risk_points == 28.0 and sig.reward_points == 102.0

    # 2530 swept both of Monday's highs; the level anchored on is the furthest
    # one taken — the Asian high at 2520 — and exactly one setup came of it.
    assert sig.liquidity_type == "SESSION_HIGH"
    assert sig.liquidity_price == ASIAN_HIGH
    assert sig.session_primary == "m2_ny_am"
    assert sig.purge_time_ny == datetime(2026, 1, TUE, 7, 0)
    assert sig.entry_time_ny == datetime(2026, 1, TUE, 7, 16)
    assert sig.entry_time_utc == tu.ny_to_utc(datetime(2026, 1, TUE, 7, 16))

    # No CISD, no FVG: the schema fields exist but claim nothing that happened.
    assert sig.cisd_tf == "M15"
    assert sig.fvg_direction == "" and sig.fvg_lower == 0.0

    assert sig.model == MODEL_2
    assert sig.state == "TRADE_CONFIRMED" and sig.status == "APPROVED"
    assert sig.risk_approved is True and sig.alert_only is True
    assert strategy.state("sell") == ENTRY_TRIGGERED
    assert strategy.pending_setups == 0

    meta = model_meta(sig)
    assert meta["model"] == MODEL_2 and meta["timeframe"] == "M15"
    assert meta["liquidity_side"] == BUYSIDE
    assert meta["liquidity_level"] == ASIAN_HIGH
    assert meta["liquidity_source_session"] == "m2_asian"
    assert meta["liquidity_source_session_label"] == "Asian"
    assert meta["liquidity_source_date"] == "2026-01-05"
    assert meta["purge_candle_high"] == 2530.0
    assert meta["purge_candle_low"] == 2502.0
    assert meta["originating_session"] == "m2_ny_am"
    assert meta["setup_status"] == "TRADE_CONFIRMED"


def test_a_sellside_purge_buys_the_break_of_the_purge_candle():
    strategy = _strategy()
    _feed(strategy, _monday_pool())
    _feed(strategy, _bucket(TUE, 7, 0, o=2410, h=2415, l=2370, cl=2380, base=2400))

    signals = _feed(strategy, [_q(TUE, 7, 15, 2400, 2420, 2395, 2415)])
    assert len(signals) == 1
    sig = signals[0]

    # BUY at the purge candle's high; the stop is its low.
    assert (sig.direction, sig.entry, sig.sl) == ("buy", 2415.0, 2370.0)
    assert (sig.target_kind, sig.target_price, sig.tp) == (
        "SESSION_HIGH", LONDON_HIGH, LONDON_HIGH)
    assert sig.rr == pytest.approx(85 / 45)
    assert sig.liquidity_type == "SESSION_LOW"
    assert sig.liquidity_price == LONDON_LOW

    meta = model_meta(sig)
    assert meta["liquidity_side"] == SELLSIDE
    assert meta["liquidity_source_session"] == "m2_london"


def test_a_candle_that_merely_touches_a_level_does_not_purge_it():
    """A high *at* the level leaves it resting; nothing is sold into it."""
    strategy = _strategy()
    _feed(strategy, _monday_london())
    # The bucket's high is exactly London's high and its low exactly London's
    # low: both are touched, neither is traded through.
    _feed(strategy, _bucket(TUE, 7, 0, o=2495, h=LONDON_HIGH, l=LONDON_LOW,
                            cl=2450, base=2450))
    assert strategy.pending_setups == 0

    # A break clean through where a purge candle's low would have been fills
    # nothing, because there is no setup to fill.
    assert _feed(strategy, [_q(TUE, 7, 15, 2450, 2451, 2380, 2390)]) == []
    assert strategy.state("buy") == WAITING
    assert strategy.state("sell") == WAITING


def test_a_purge_outside_every_window_builds_no_setup():
    """06:30 NY sits between Model 2's London and NY AM windows."""
    strategy = _strategy()
    _feed(strategy, _monday_london())
    _feed(strategy, _bucket(TUE, 6, 30, o=2505, h=2530, l=2502, cl=2520, base=2520))
    assert strategy.pending_setups == 0
    assert _feed(strategy, [_q(TUE, 6, 45, 2520, 2522, 2480, 2490)]) == []
    assert strategy.state("sell") == WAITING


# --------------------------------------------------------------------------- #
# One event, one signal
# --------------------------------------------------------------------------- #
def test_one_candle_taking_several_levels_is_one_setup():
    """A cascade of crossed levels must not become duplicated signals."""
    strategy = _strategy()
    _feed(strategy, _monday_pool())
    # 07:00 trades through both of Monday's highs at once.
    _feed(strategy, _bucket(TUE, 7, 0, o=2530, h=2540, l=2510, cl=2535, base=2530))

    signals = _feed(strategy, [_q(TUE, 7, 15, 2535, 2536, 2500, 2510)])
    assert len(signals) == 1
    sig = signals[0]
    assert sig.liquidity_price == ASIAN_HIGH          # the furthest taken
    assert (sig.entry, sig.sl, sig.tp) == (2510.0, 2540.0, LONDON_LOW)
    assert model_meta(sig)["liquidity_source_session"] == "m2_asian"


def test_the_break_fills_once_and_never_twice():
    strategy = _strategy()
    _feed(strategy, _monday_pool())
    _feed(strategy, _bucket(TUE, 7, 0, o=2505, h=2530, l=2502, cl=2520, base=2520))

    first = _feed(strategy, [_q(TUE, 7, 15, 2520, 2522, 2480, 2490)])
    assert len(first) == 1
    # Further minutes trading well below the same boundary produce nothing: the
    # premise was consumed by its entry.
    later = _feed(strategy, [_q(TUE, 7, 16, 2490, 2492, 2470, 2480),
                             _q(TUE, 7, 17, 2480, 2482, 2450, 2460)])
    assert later == []
    assert strategy.pending_setups == 0


def test_warm_up_loads_history_without_emitting_or_deciding():
    """Warm-up replays must not re-alert a setup that fired before attach."""
    strategy = _strategy()
    strategy.warm(_monday_pool()
                  + _bucket(TUE, 7, 0, o=2505, h=2530, l=2502, cl=2520, base=2520)
                  + [_q(TUE, 7, 15, 2520, 2522, 2480, 2490)])
    assert strategy.pending_setups == 0
    assert strategy.state("sell") == WAITING
    # The history is loaded all the same, so the pool is there for the live feed.
    assert _prices(strategy.session_levels()) == {
        LONDON_HIGH, LONDON_LOW, ASIAN_HIGH, ASIAN_LOW}


# --------------------------------------------------------------------------- #
# Session validity, expiry, and rejection
# --------------------------------------------------------------------------- #
def test_an_entry_outside_its_own_session_expires_instead_of_carrying_over():
    strategy = _strategy()
    _feed(strategy, _monday_pool())
    # Purged in the last bucket of NY AM (10:45-10:59)...
    _feed(strategy, _bucket(TUE, 10, 45, o=2505, h=2530, l=2502, cl=2520, base=2520))
    assert strategy.pending_setups == 0

    # ...and the break only arrives at 11:00, in Lunch. Same NY day, different
    # session — so it expires rather than filling.
    assert _feed(strategy, [_q(TUE, 11, 0, 2520, 2522, 2400, 2410)]) == []
    assert strategy.state("sell") == EXPIRED
    assert strategy.pending_setups == 0

    # An expired setup can never trigger later, however clean the break.
    assert _feed(strategy, [_q(TUE, 11, 1, 2410, 2412, 2300, 2310)]) == []
    assert strategy.pending_setups == 0


def test_a_purge_and_its_break_must_share_one_session():
    """05:59 is a valid entry for a London purge; 06:00 the next minute is not."""
    london = m2.MODEL_2_SESSION_INDEX["m2_london"]
    strategy = _strategy()
    _feed(strategy, _monday_pool())
    # A London-window purge whose candle closes at 06:00 — the session's own end.
    _feed(strategy, _bucket(TUE, 5, 45, o=2505, h=2530, l=2502, cl=2520, base=2520))
    assert strategy.pending_setups == 0

    assert _feed(strategy, [_q(TUE, 6, 0, 2520, 2522, 2400, 2410)]) == []
    assert strategy.state("sell") == EXPIRED
    assert london.contains(5 * 60 + 45) and not london.contains(6 * 60)


def test_no_session_liquidity_in_the_trade_direction_rejects_the_setup():
    """No fallback target: an untargetable setup is refused, not widened."""
    console = []
    strategy = _strategy(log=console.append)
    # Monday ranges with no room below on either side of the purge candle.
    _feed(strategy, _session(MON, "01:00", "06:00", base=2495.0,
                             high=2500.0, low=2490.0)
          + _session(MON, "20:00", "24:00", base=2515.0,
                     high=2520.0, low=2510.0))
    # One candle takes every level at once: above the highs and below the lows.
    _feed(strategy, _bucket(TUE, 7, 0, o=2505, h=2545, l=2450, cl=2500, base=2520))

    assert _feed(strategy, [_q(TUE, 7, 15, 2500, 2546, 2449, 2500)]) == []
    assert strategy.state("sell") == INVALIDATED
    assert strategy.state("buy") == INVALIDATED
    assert strategy.pending_setups == 0
    assert any("no session liquidity" in m for m in console), console
    assert all(m.startswith("[TEST] [M2]") for m in console), console


def test_the_shared_risk_policy_applies_to_model_2():
    """Model 2 cannot trade geometry the operator's MIN_RR has refused."""
    strict = replace(ASSET, overrides={"min_rr": "99"})
    console = []
    strategy = _strategy(asset=strict, log=console.append)
    _feed(strategy, _monday_pool())
    _feed(strategy, _bucket(TUE, 7, 0, o=2410, h=2415, l=2370, cl=2380, base=2400))

    assert _feed(strategy, [_q(TUE, 7, 15, 2400, 2420, 2395, 2415)]) == []
    assert strategy.state("buy") == INVALIDATED
    assert any("RR below the minimum" in m for m in console), console


# --------------------------------------------------------------------------- #
# Isolation from Model 1
# --------------------------------------------------------------------------- #
def test_model_1_setup_ids_and_signals_are_untouched_by_the_model_2_namespace():
    stamp = datetime(2026, 1, 6, 12, 0)
    bare = build_setup_id("TEST", "buy", stamp, stamp, stamp)
    # Model 1 passes no model, so its ids — and the fingerprints already stored
    # under them — are reproduced byte for byte.
    assert bare == build_setup_id("TEST", "buy", stamp, stamp, stamp, model="")
    assert bare != build_setup_id("TEST", "buy", stamp, stamp, stamp,
                                  model=MODEL_2)

    # A hand-built signal is everything Model 1 ever produced: Model 1 by default,
    # with no provenance payload and no Model 2 presentation.
    sig = Signal(asset="TEST", direction="buy", entry=1.0, sl=0.5, tp=2.0,
                 entry_time_utc=stamp, entry_time_ny=stamp)
    assert sig.model == MODEL_1 and sig.model_meta == ""
    assert model_label(sig) == MODEL_1
    assert not is_model_2(sig) and model_meta(sig) == {}


def test_the_alert_reads_as_model_2_and_never_claims_a_cisd():
    strategy = _strategy()
    _feed(strategy, _monday_pool())
    _feed(strategy, _bucket(TUE, 7, 0, o=2505, h=2530, l=2502, cl=2520, base=2520))
    sig = _feed(strategy, [_q(TUE, 7, 15, 2520, 2522, 2480, 2490)])[0]

    text = format_signal_alert(sig)
    fields = {ln.split(":", 1)[0].strip(): ln.split(":", 1)[1].strip()
              for ln in text.splitlines() if ":" in ln}
    assert fields["Model"] == MODEL_2
    assert fields["Session"] == "NY AM"
    assert "Sequence (Model 2):" in text
    assert "Purge (M15):" in text and "Purge candle:" in text
    assert "Entry trigger:" in text and "Stop anchor:" in text
    assert "Asian High @ 2520" in text and "2026-01-05" in text
    # The steps Model 2 does not take are not described as if it took them.
    assert "CISD" not in text and "FVG" not in text

    # Model 1's alert keeps its own sequence, and gains no Model 2 header.
    model1 = Signal(asset="TEST", direction="buy", entry=1.0, sl=0.5, tp=2.0,
                    entry_time_utc=datetime(2026, 1, 6, 12, 0),
                    entry_time_ny=datetime(2026, 1, 6, 12, 0),
                    session_keys=["ny_am"], session_primary="ny_am",
                    liquidity_type="PDL", liquidity_price=0.5,
                    purge_time_ny=datetime(2026, 1, 6, 11, 0),
                    cisd_tf="M15", cisd_confirm_time_ny=datetime(2026, 1, 6, 11, 30),
                    fvg_direction="bullish")
    text1 = format_signal_alert(model1)
    fields1 = {ln.split(":", 1)[0].strip(): ln.split(":", 1)[1].strip()
               for ln in text1.splitlines() if ":" in ln}
    assert "Sequence:" in text1
    assert "CISD (M15):" in text1 and "FVG (1M):" in text1
    assert "Model" not in fields1
    assert "Model 2" not in text1
    assert "Sequence (Model 2)" not in text1


# --------------------------------------------------------------------------- #
# The existing pipeline
# --------------------------------------------------------------------------- #
def test_a_model_2_signal_runs_through_the_shared_scanner_and_database():
    """Persistence, dedupe, alerting and the safe executor, all reused."""
    sent = []
    settings = _settings(auto_trading=False, ai_enabled=False,
                         telegram_enabled=True, telegram_bot_token="tok",
                         telegram_chat_id="chat")
    repo = _repo()
    scanner = Model2Scanner(
        ASSET, settings=settings, repo=repo,
        analyzer=AiAnalyzer(settings=settings, transport=lambda p: None),
        notifier=TelegramNotifier(
            settings=settings,
            transport=lambda text: sent.append(text) or _FakeSend()),
        executor=Executor(settings=settings))

    candles = _monday_pool() + _bucket(TUE, 7, 0, o=2505, h=2530, l=2502,
                                       cl=2520, base=2520)
    scanner.warm(candles)                    # the purge bucket has not closed
    assert repo.count_signals() == 0

    assert scanner.step([_q(TUE, 7, 15, 2520, 2522, 2480, 2490)]) == 1
    assert repo.count_signals() == 1

    row = repo.recent_signals()[0]
    assert row.model == MODEL_2
    assert row.asset == "TEST" and row.direction == "sell"
    assert row.status == "APPROVED"
    meta = json.loads(row.model_meta)
    assert meta["liquidity_side"] == BUYSIDE
    assert meta["liquidity_level"] == ASIAN_HIGH
    assert meta["liquidity_source_session"] == "m2_asian"

    assert scanner.setup_states()["sell"] == ENTRY_TRIGGERED

    # AUTO_TRADING is off, so the shared executor records a SKIPPED attempt.
    trades = repo.recent_trades()
    assert len(trades) == 1 and trades[0].status == "SKIPPED"

    assert len(sent) == 1
    assert "Sequence (Model 2)" in sent[0] and "ALERT ONLY" in sent[0]

    # Re-delivering the same closed candles is a no-op, as for Model 1.
    assert scanner.feed_new(candles) == 0
    assert repo.count_signals() == 1


def test_the_backtester_replays_model_2_through_its_own_engine():
    settings = _settings()
    runner = BacktestRunner(
        ASSET, model=MODEL_2,
        strategy_factory=lambda asset: Model2Strategy(asset, settings=settings))
    summary, trades = runner.run(
        _monday_pool()
        + _bucket(TUE, 7, 0, o=2505, h=2530, l=2502, cl=2520, base=2520)
        + [_q(TUE, 7, 15, 2520, 2522, 2480, 2490)])

    assert summary.n_signals == 1
    assert summary.params["model"] == MODEL_2
    assert len(trades) == 1
    assert trades[0].direction == "sell"
    assert trades[0].entry == 2502.0


def test_an_existing_database_gains_the_model_columns():
    """create_all never ALTERs, so the additive migration has to."""
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE signals (id INTEGER PRIMARY KEY, "
                          "fingerprint VARCHAR(64))"))
    ensure_schema(engine)

    columns = {col["name"] for col in inspect(engine).get_columns("signals")}
    assert {"model", "model_meta"} <= columns
    indexes = {ix["name"] for ix in inspect(engine).get_indexes("signals")}
    assert "ix_signals_model" in indexes

    # The declared defaults are what an existing row reads as.
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO signals (fingerprint) VALUES ('abc')"))
        row = conn.execute(text(
            "SELECT model, model_meta FROM signals WHERE fingerprint = 'abc'")
        ).one()
    assert row[0] == "MODEL_1" and row[1] == "{}"
