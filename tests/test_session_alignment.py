"""End-to-end session alignment: MT5 -> UTC -> New York -> ICT session.

The unit tests elsewhere pin each conversion in isolation. These pin the whole
chain, because the failure mode this work exists to prevent is not a wrong
individual step — it is a *disagreement* between steps, or between the live
scanner, the backtester and the Telegram alert. Every test here asks the same
question of a different consumer and demands the same answer.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from trading import sessions as sess
from trading import time_utils as tu
from trading.bars import make_candle
from trading.market_data import row_to_candle
from trading.signal_engine import Signal

from backtesting.engine import _bucket_key
from notifications.telegram import format_signal_alert

MON = datetime(2026, 9, 14)          # a Monday, NY clock
EPOCH = datetime(1970, 1, 1)


def broker_row(server_naive: datetime, o=1.0, h=2.0, l=0.5, c=1.5) -> tuple:
    """An MT5 rate row whose timestamp is a *broker wall clock* time."""
    epoch = int((server_naive - EPOCH).total_seconds())
    return (epoch, o, h, l, c, 7, 0, 0)


# --------------------------------------------------------------------------- #
# The five sessions, on the New York clock
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hour,minute,key,label", [
    (19, 0, "asian_range", "Asian"),
    (23, 59, "asian_range", "Asian"),
    (2, 0, "london_open", "London"),
    (4, 59, "london_open", "London"),
    (7, 0, "ny_am", "NY AM"),
    (10, 59, "ny_am", "NY AM"),
    (13, 0, "ny_pm", "NY PM"),
    (14, 59, "ny_pm", "NY PM"),
    (15, 0, "power_hour", "Power Hour"),
    (15, 59, "power_hour", "Power Hour"),
])
def test_each_session_is_detected_at_its_new_york_boundaries(hour, minute, key, label):
    at = MON.replace(hour=hour, minute=minute)
    assert sess.get_trading_session_key(at) == key
    assert sess.get_trading_session_label(at) == label


@pytest.mark.parametrize("hour,minute", [
    (0, 0), (1, 0), (5, 0), (6, 59), (11, 0), (12, 0), (12, 59),
    (16, 0), (18, 59),
])
def test_hours_outside_every_session_are_not_given_one(hour, minute):
    at = MON.replace(hour=hour, minute=minute)
    assert sess.get_trading_session(at) is None
    assert sess.get_trading_session_key(at) == ""     # documented empty sentinel
    assert sess.get_trading_session_label(at) == "Outside session"


def test_the_same_new_york_wall_clock_gives_the_same_session_all_year():
    """Sessions are New York *local* times, so DST must not move them.

    Walking each session's first and last minute through both sides of the DST
    change is what catches a hard-coded UTC-4: the EST rows would land an hour
    out and fall into a neighbouring window.
    """
    winter = datetime(2026, 1, 5)      # Monday, EST (UTC-5)
    summer = datetime(2026, 7, 6)      # Monday, EDT (UTC-4)

    for day in (winter, summer):
        for hour, minute, key in [(19, 0, "asian_range"), (2, 0, "london_open"),
                                  (7, 0, "ny_am"), (13, 0, "ny_pm"),
                                  (15, 0, "power_hour")]:
            at = day.replace(hour=hour, minute=minute)
            assert sess.get_trading_session_key(at) == key, (day, hour, minute)


# --------------------------------------------------------------------------- #
# MT5 broker row -> UTC -> New York
# --------------------------------------------------------------------------- #
def test_a_broker_row_lands_in_the_session_its_new_york_time_names():
    """Broker 14:00 with a +2 server is NY 08:00 — NY AM, not the broker's hour.

    Read on the broker clock, 14:00 would be Power Hour. The whole point of the
    conversion is that it is not.
    """
    candle = row_to_candle(broker_row(datetime(2026, 9, 14, 14, 0)), 2.0)

    assert candle.t_utc == datetime(2026, 9, 14, 12, 0)
    assert candle.t_ny == datetime(2026, 9, 14, 8, 0)
    assert sess.get_trading_session_key(candle.t_ny) == "ny_am"
    # ...and emphatically not what the raw broker hour would have implied.
    assert sess.get_trading_session_key(datetime(2026, 9, 14, 14, 0)) == "ny_pm"


def test_a_broker_row_in_winter_resolves_an_hour_later_than_in_summer():
    """Same broker clock, New York's own DST shift does the rest."""
    winter = row_to_candle(broker_row(datetime(2026, 1, 14, 14, 0)), 2.0)
    summer = row_to_candle(broker_row(datetime(2026, 7, 14, 14, 0)), 2.0)

    assert winter.t_utc == datetime(2026, 1, 14, 12, 0)
    assert summer.t_utc == datetime(2026, 7, 14, 12, 0)
    assert winter.t_ny == datetime(2026, 1, 14, 7, 0)     # EST
    assert summer.t_ny == datetime(2026, 7, 14, 8, 0)     # EDT


def test_the_asian_session_is_reached_across_midnight_without_wrapping():
    """Broker 05:00 (+2) is NY 23:00 the *previous* day — still the Asian range.

    The window ends at midnight, so the NY date rolls over while the session is
    unchanged: the candle is Monday 23:00 and belongs to Monday's Asian range.
    """
    candle = row_to_candle(broker_row(datetime(2026, 9, 15, 5, 0)), 2.0)

    assert candle.t_utc == datetime(2026, 9, 15, 3, 0)
    assert candle.t_ny == datetime(2026, 9, 14, 23, 0)     # still Monday
    assert sess.get_trading_session_key(candle.t_ny) == "asian_range"
    # One hour later the NY clock has crossed midnight and the session is over.
    later = row_to_candle(broker_row(datetime(2026, 9, 15, 6, 0)), 2.0)
    assert later.t_ny == datetime(2026, 9, 15, 0, 0)
    assert sess.get_trading_session(later.t_ny) is None


def test_a_wrong_broker_offset_moves_the_session():
    """Proof the conversion is load-bearing, not cosmetic.

    The same row read with a UTC+2 server is NY AM; read with a UTC+5 server it
    is NY 05:00, the gap between London's close and the NY AM open. A wrong
    offset silently changes which session trades — or silently loses the trade.
    """
    row = broker_row(datetime(2026, 9, 14, 14, 0))

    assert sess.get_trading_session_key(row_to_candle(row, 2.0).t_ny) == "ny_am"
    assert sess.get_trading_session(row_to_candle(row, 5.0).t_ny) is None


# --------------------------------------------------------------------------- #
# Every consumer agrees
# --------------------------------------------------------------------------- #
def _ny_instant(broker_naive: datetime, offset: float) -> datetime:
    return tu.broker_to_ny(broker_naive, offset)


def test_live_scanner_and_backtest_agree_on_the_session():
    """The live path and the backtest must never disagree about the same candle.

    Both are handed the *same* UTC instant here, which is what the shared
    conversion layer guarantees: the backtester buckets a trade by
    ``_bucket_key(entry_time_utc)`` and the live scanner labels it from
    ``candle.t_ny``, and those are the same instant expressed once.
    """
    broker_naive = datetime(2026, 9, 14, 14, 0)
    candle = row_to_candle(broker_row(broker_naive), 2.0)

    live_key = sess.get_trading_session_key(candle.t_ny)
    backtest_key = _bucket_key(candle.t_utc)
    from_ny_directly = sess.get_trading_session_key(_ny_instant(broker_naive, 2.0))

    assert live_key == backtest_key == from_ny_directly == "ny_am"


def test_the_backtest_buckets_by_new_york_session_not_utc_hour():
    """A UTC-hour bucket would file this NY AM trade under the wrong session."""
    candle = row_to_candle(broker_row(datetime(2026, 9, 14, 14, 0)), 2.0)

    assert _bucket_key(candle.t_utc) == "ny_am"
    assert candle.t_utc.hour == 12            # the UTC hour says nothing useful
    assert candle.t_ny.hour == 8


def test_a_trade_outside_every_session_buckets_as_outside():
    candle = row_to_candle(broker_row(datetime(2026, 9, 14, 18, 0)), 2.0)
    assert candle.t_ny == datetime(2026, 9, 14, 12, 0)
    assert _bucket_key(candle.t_utc) == "outside"


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def _signal_at(broker_naive: datetime, offset: float = 2.0) -> Signal:
    candle = row_to_candle(broker_row(broker_naive), offset)
    return Signal(
        asset="USDCAD", direction="sell", entry=1.3654, sl=1.3680, tp=1.3600,
        entry_time_utc=candle.t_utc, entry_time_ny=candle.t_ny,
        session_keys=["ny_am"], session_primary="ny_am", cisd_tf="M5",
        liquidity_type="PDH", liquidity_price=1.3690, rr=2.1,
    )


def test_the_telegram_alert_shows_new_york_time():
    """``Time (NY)`` must be the NY clock, and say so."""
    alert = format_signal_alert(_signal_at(datetime(2026, 9, 14, 14, 0)))

    assert "Time (NY):           2026-09-14 08:00" in alert
    # The broker's own 14:00 and the UTC 12:00 must not appear as the NY time.
    assert "2026-09-14 14:00" not in alert
    assert "2026-09-14 12:00" not in alert


def test_the_telegram_alert_session_label_matches_the_session_module():
    """The alert names the same session the strategy gated on."""
    signal = _signal_at(datetime(2026, 9, 14, 14, 0))
    alert = format_signal_alert(signal)

    expected = sess.get_trading_session_label(signal.entry_time_ny)
    assert f"Session:             {expected}" in alert


def test_the_telegram_time_follows_dst():
    """The same NY wall clock, either side of the DST change."""
    winter = format_signal_alert(_signal_at(datetime(2026, 1, 14, 14, 0)))
    summer = format_signal_alert(_signal_at(datetime(2026, 7, 14, 14, 0)))

    assert "Time (NY):           2026-01-14 07:00" in winter   # EST
    assert "Time (NY):           2026-07-14 08:00" in summer   # EDT


# --------------------------------------------------------------------------- #
# The conversion trace
# --------------------------------------------------------------------------- #
def test_the_debug_trace_reports_every_stage_of_the_chain():
    candle = row_to_candle(broker_row(datetime(2026, 9, 14, 14, 0)), 2.0)
    trace = tu.format_time_chain(datetime(2026, 9, 14, 14, 0), candle.t_utc,
                                 candle.t_ny, session="NY AM", offset_hours=2.0)

    assert "[MT5 TIME]" in trace
    assert "2026-09-14 14:00:00" in trace     # the broker clock
    assert "[UTC TIME]" in trace
    assert "2026-09-14 12:00:00" in trace     # real UTC
    assert "[NEW YORK TIME]" in trace
    assert "2026-09-14 08:00:00" in trace     # the strategy's clock
    assert "[ICT SESSION]  NY AM" in trace
    assert "EDT" in trace and "-04:00" in trace


def test_the_debug_trace_names_the_winter_offset():
    candle = row_to_candle(broker_row(datetime(2026, 1, 14, 14, 0)), 2.0)
    trace = tu.format_time_chain(datetime(2026, 1, 14, 14, 0), candle.t_utc,
                                 candle.t_ny, session="NY AM", offset_hours=2.0)
    assert "EST" in trace and "-05:00" in trace


def test_describe_candle_time_uses_the_real_pipeline_conversion(monkeypatch):
    """The scanner's trace must reflect the pipeline, not a parallel opinion."""
    from trading.market_data import describe_candle_time

    row = broker_row(datetime(2026, 9, 14, 14, 0))
    candle = row_to_candle(row, 2.0)
    text = describe_candle_time(row, candle, 2.0)

    assert "[MT5 TIME]     2026-09-14 14:00:00" in text
    assert "[UTC TIME]     2026-09-14 12:00:00" in text
    assert "[NEW YORK TIME] 2026-09-14 08:00:00" in text
    assert "[ICT SESSION]  NY AM" in text


def test_make_candle_derives_the_new_york_time_centrally():
    """Construction sites cannot supply their own NY time — it is derived."""
    utc = datetime(2026, 9, 14, 12, 0)
    candle = make_candle(utc, 1.0, 2.0, 0.5, 1.5)
    assert candle.t_ny == datetime(2026, 9, 14, 8, 0)
    assert candle.time == candle.t_ny
