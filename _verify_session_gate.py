"""End-to-end acceptance check for the session-hours gate.

Walks every minute of a simulated week on the NY clock and prints the spans the
bot would be awake for. Run it for a winter week and a summer week: the NY
wall-clock spans must be *identical*, which is the only way to show the gate
follows America/New_York rather than a fixed offset.

    python _verify_session_gate.py
"""
from datetime import datetime, timedelta

from trading import sessions as sess
from trading import time_utils as tu

# Every session the spec marks tradeable, pinned so the result does not depend
# on this machine's .env.
ALLOW = ["london_open", "ny_premarket", "ny_am", "london_close", "ny_pm"]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

#: Mon 2026-01-05 .. Sun 2026-01-11 (EST) and Mon 2026-07-06 .. Sun 2026-07-12
#: (EDT). The two weeks differ only in which side of the DST switch they sit on.
WEEKS = {"winter (EST)": datetime(2026, 1, 5),
         "summer (EDT)": datetime(2026, 7, 6)}

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {message}")
    if not ok:
        failures.append(message)


def spans_for_week(monday: datetime) -> dict[str, list[tuple[int, int]]]:
    """Contiguous awake spans per day name, as minutes-of-day."""
    out: dict[str, list[tuple[int, int]]] = {}
    for day in range(7):
        date = monday + timedelta(days=day)
        spans: list[tuple[int, int]] = []
        start = None
        for minute in range(sess.DAY_MINUTES):
            ny = date.replace(hour=minute // 60, minute=minute % 60)
            active, _ = sess.session_activity(ny, allowed_sessions=ALLOW)
            if active and start is None:
                start = minute
            elif not active and start is not None:
                spans.append((start, minute))
                start = None
        if start is not None:
            spans.append((start, sess.DAY_MINUTES))
        out[DAYS[day]] = spans
    return out


def fmt(spans: list[tuple[int, int]]) -> str:
    return ", ".join(f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"
                     for s, e in spans) or "asleep all day"


EXPECTED_WEEKDAY = [(120, 300), (420, 690), (810, 960), (1200, 1440)]
# 02:00-05:00 London, 07:00-11:30 Premarket+NY AM, 13:30-16:00 NY PM, and
# 20:00-24:00 Asian — awake to build the range, though nothing can be entered.

print("Walking a simulated week of NY minutes per season...\n")
spans_by_season = {}
for label, monday in WEEKS.items():
    spans_by_season[label] = spans_for_week(monday)

for label, spans in spans_by_season.items():
    print(f"{label} week starting {monday.strftime('%Y-%m-%d')}:")
    for day in DAYS:
        print(f"    {day}  {fmt(spans[day])}")
    print()

print("Checks:")
for label, spans in spans_by_season.items():
    check(spans["Sat"] == [] and spans["Sun"] == [],
          f"{label}: the weekend is silent")
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri"):
        check(spans[day] == EXPECTED_WEEKDAY,
              f"{label}: {day} awake exactly 02:00-05:00, 07:00-11:30, "
              "13:30-16:00, 20:00-24:00")

    # The gate is defined on the NY wall clock, so the spans must not move with
    # the season. This is what would break if a fixed UTC-4 were hard-coded.
    winter = spans_by_season["winter (EST)"]
    summer = spans_by_season["summer (EDT)"]
    check(winter == summer,
          f"{label}: the same wall-clock spans as the other season")

print("\nDST boundary check (NY wall clock on either side of 2026-03-08):")
for label, start in (("winter", datetime(2026, 3, 1, 12, 0)),
                     ("summer", datetime(2026, 3, 8, 12, 0))):
    nxt = sess.next_activity_start_utc(start, allowed_sessions=ALLOW)
    ny = tu.utc_to_ny(nxt)
    check(ny.hour == 2 and ny.minute == 0 and ny.weekday() == 0,
          f"{label} start {start:%Y-%m-%d}: next open is Monday 02:00 NY "
          f"(= {nxt:%Y-%m-%d %H:%M} UTC)")

print("\nThe regression the whole change exists for:")
check(sess.session_trade_mode(180) == sess.TRADE_YES,
      "the window table still calls 03:00 tradeable on every day of the week")
check(sess.session_activity(datetime(2026, 9, 12, 3, 0), allowed_sessions=ALLOW)
      == (False, "market_closed_weekend"),
      "the calendar refuses Saturday 03:00 anyway")

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    raise SystemExit(1)
print("all checks passed")
