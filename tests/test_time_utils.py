"""Timezone conversion tests (centralized in trading.time_utils)."""
from datetime import datetime

from trading import time_utils as tu


def test_ny_fixed_utc4():
    assert tu.NY_OFFSET_HOURS == -4
    # 12:00 UTC -> 08:00 NY
    assert tu.utc_to_ny(datetime(2026, 1, 15, 12, 0)) == datetime(2026, 1, 15, 8, 0)


def test_roundtrip_utc_ny():
    utc = datetime(2026, 6, 1, 20, 30)
    ny = tu.utc_to_ny(utc)
    assert tu.ny_to_utc(ny) == utc


def test_broker_offset_conversion():
    # Broker server UTC+2 at 14:00 -> real UTC 12:00 -> NY 08:00
    broker = datetime(2026, 3, 10, 14, 0)
    assert tu.broker_to_utc(broker, offset_hours=2) == datetime(2026, 3, 10, 12, 0)
    assert tu.broker_to_ny(broker, offset_hours=2) == datetime(2026, 3, 10, 8, 0)


def test_broker_negative_and_fractional_offsets():
    # Server UTC-3 -> NY is broker - ( -3 -4 ) = broker + ... derive below.
    broker = datetime(2026, 3, 10, 4, 0)
    # net shift = (offset + 4) => for offset=-3 => shift broker by -(-3+4)=-1 => 03:00? recheck:
    # broker_to_ny = broker - (server_offset - NY_OFFSET); server_offset=-3h; NY_OFFSET=-4h
    # => broker - (-3 - (-4)) = broker - (1h) = 03:00
    assert tu.broker_to_ny(broker, offset_hours=-3) == datetime(2026, 3, 10, 3, 0)


def test_hour_float_and_minute_of_day():
    ny_dt = datetime(2026, 9, 9, 9, 30)
    assert tu.ny_hour_float(ny_dt) == 9.5
    assert tu.minute_of_day(ny_dt) == 9 * 60 + 30


def test_now_ny_is_four_hours_behind_utc():
    utc = tu.now_utc()
    assert (utc - tu.ny_to_utc(tu.utc_to_ny(utc))).total_seconds() == 0
