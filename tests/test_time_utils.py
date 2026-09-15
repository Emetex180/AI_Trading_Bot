"""Timezone conversion tests (centralized in trading.time_utils).

The NY clock is **America/New_York**, so the UTC offset is a function of the
date (EDT -4 in summer, EST -5 in winter) and never a constant. These tests pin
both sides of the DST switch and the fall-back/spring-forward instants, because
an off-by-one-hour NY clock silently moves every session window.
"""
from datetime import datetime, timedelta

from trading import time_utils as tu


# --------------------------------------------------------------------------- #
# DST awareness
# --------------------------------------------------------------------------- #
def test_ny_zone_is_america_new_york():
    assert tu.NY_TZ_NAME == "America/New_York"


def test_winter_is_est_minus_five():
    # 2026-01-15 is standard time: 12:00 UTC -> 07:00 NY (UTC-5).
    assert tu.ny_offset_hours(datetime(2026, 1, 15, 12, 0)) == -5
    assert tu.utc_to_ny(datetime(2026, 1, 15, 12, 0)) == datetime(2026, 1, 15, 7, 0)


def test_summer_is_edt_minus_four():
    # 2026-07-15 is daylight time: 12:00 UTC -> 08:00 NY (UTC-4).
    assert tu.ny_offset_hours(datetime(2026, 7, 15, 12, 0)) == -4
    assert tu.utc_to_ny(datetime(2026, 7, 15, 12, 0)) == datetime(2026, 7, 15, 8, 0)


def test_offset_tracks_the_dst_switch():
    """The two sides of the switch differ by an hour — the old UTC-4 bug."""
    est = datetime(2026, 3, 7, 12, 0)    # Sat 12:00 UTC, standard time
    edt = datetime(2026, 3, 9, 12, 0)    # Mon 12:00 UTC, daylight time
    assert tu.utc_to_ny(est) == datetime(2026, 3, 7, 7, 0)
    assert tu.utc_to_ny(edt) == datetime(2026, 3, 9, 8, 0)
    # Same UTC hour of day, one hour apart on the NY clock.
    assert tu.ny_offset_hours(edt) - tu.ny_offset_hours(est) == 1


def test_autumn_dst_switch():
    # 2026-11-01 is the US fall-back date.
    assert tu.ny_offset_hours(datetime(2026, 10, 30, 12, 0)) == -4
    assert tu.ny_offset_hours(datetime(2026, 11, 3, 12, 0)) == -5


# --------------------------------------------------------------------------- #
# Round trips and broker clock
# --------------------------------------------------------------------------- #
def test_roundtrip_utc_ny():
    for utc in (datetime(2026, 6, 1, 20, 30), datetime(2026, 1, 5, 20, 30)):
        assert tu.ny_to_utc(tu.utc_to_ny(utc)) == utc


def test_ny_to_utc_uses_the_dst_offset_for_that_date():
    # 09:30 NY on a summer day is 13:30 UTC; on a winter day it is 14:30 UTC.
    assert tu.ny_to_utc(datetime(2026, 7, 6, 9, 30)) == datetime(2026, 7, 6, 13, 30)
    assert tu.ny_to_utc(datetime(2026, 1, 6, 9, 30)) == datetime(2026, 1, 6, 14, 30)


def test_broker_offset_conversion():
    # Broker server UTC+2 at 14:00 -> real UTC 12:00 -> NY 07:00 (EST, Jan).
    broker = datetime(2026, 1, 10, 14, 0)
    assert tu.broker_to_utc(broker, offset_hours=2) == datetime(2026, 1, 10, 12, 0)
    assert tu.broker_to_ny(broker, offset_hours=2) == datetime(2026, 1, 10, 7, 0)


def test_broker_offset_conversion_in_summer():
    # Same broker clock in July resolves one hour later on the NY clock.
    broker = datetime(2026, 7, 10, 14, 0)
    assert tu.broker_to_ny(broker, offset_hours=2) == datetime(2026, 7, 10, 8, 0)


def test_broker_negative_and_fractional_offsets():
    # broker_to_ny = broker - (server_offset - ny_offset).
    # March 5 is still EST (-5) and the server is UTC-3, so the shift is
    # -(-3 - (-5)) = -2h.
    assert tu.broker_to_ny(datetime(2026, 3, 5, 4, 0), offset_hours=-3) \
        == datetime(2026, 3, 5, 2, 0)
    # The same broker clock after the DST switch lands one hour later: the
    # NY offset moved to -4, so the shift is only -1h.
    assert tu.broker_to_ny(datetime(2026, 3, 11, 4, 0), offset_hours=-3) \
        == datetime(2026, 3, 11, 3, 0)


def test_broker_roundtrip():
    broker = datetime(2026, 7, 10, 14, 0)
    utc = tu.broker_to_utc(broker, offset_hours=2)
    assert tu.utc_to_broker(utc, offset_hours=2) == broker


def test_ny_broker_helpers_roundtrip():
    ng = datetime(2026, 9, 9, 9, 30)
    assert tu.broker_to_ny(tu.ny_to_broker(ng, offset_hours=2), offset_hours=2) == ng


# --------------------------------------------------------------------------- #
# Clock helpers
# --------------------------------------------------------------------------- #
def test_hour_float_and_minute_of_day():
    ny_dt = datetime(2026, 9, 9, 9, 30)
    assert tu.ny_hour_float(ny_dt) == 9.5
    assert tu.minute_of_day(ny_dt) == 9 * 60 + 30
    assert tu.minute_of_day(datetime(2026, 9, 9, 0, 0)) == 0


def test_now_ny_is_a_fixed_point_of_the_conversion():
    utc = tu.now_utc()
    assert (utc - tu.ny_to_utc(tu.utc_to_ny(utc))).total_seconds() == 0


def test_ny_date_key():
    assert tu.ny_date_key(datetime(2026, 9, 9, 23, 0)) == "2026-09-09"
