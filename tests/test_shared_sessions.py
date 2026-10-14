"""One session schedule for every model.

This file verifies the three claims the centralized session schedule makes:

1. **Shared** — Model 2's sessions are the bot's sessions. Every model, present
   and future, resolves its trading hours from :data:`trading.sessions.CORE_SESSIONS`;
   :mod:`trading.model2_sessions` keeps Model 2's own key names but *derives* its
   windows from that table rather than stating times a second time.
2. **New York local clock** — the windows are NY wall-clock intervals, so DST is
   absorbed by the timezone database rather than by arithmetic. Nothing here adds
   or subtracts a UTC offset.
3. **12-hour display** — every session time the bot *shows* (dashboard, logs,
   alerts, reports) is written on the operator's clock, from the window object
   the gate itself consults.

The distinction between (2) and (3) is the point of several of these tests:
display conventions must not be able to move a session boundary, and the stored
minutes must not leak into what an operator reads.
"""
import importlib
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from trading import model2_sessions as m2
from trading import sessions as sess
from trading import time_utils as tu

#: A Wednesday and a Saturday in summer (EDT, UTC-4), and the same weekdays in
#: winter (EST, UTC-5) — see the DST section.
WED_JUL = datetime(2026, 7, 15)
WED_JAN = datetime(2026, 1, 14)


# --------------------------------------------------------------------------- #
# 1. One table for every model
# --------------------------------------------------------------------------- #
def test_model_2_windows_are_the_shared_windows():
    """Slot for slot: same label, same minutes, same trade mode.

    Only the key differs — and the key is Model 2's own because stored setups
    persist it.
    """
    by_label = {(w.label, w.start, w.end, w.trade): w.key
                for w in sess.CORE_SESSIONS}

    assert len(m2.MODEL_2_SESSIONS) == len(sess.CORE_SESSIONS)
    for window in m2.MODEL_2_SESSIONS:
        assert window.key.startswith("m2_"), window.key
        shared_key = by_label.get((window.label, window.start, window.end,
                                   window.trade))
        assert shared_key is not None, window.key


def test_the_two_tables_describe_the_same_intervals_all_day():
    """Stronger than the slot-for-slot check: the *day* is covered identically.

    Every minute of the NY day has to resolve to the same interval in both
    tables, which is the property that lets the live loop treat Model 1's and
    Model 2's wake-up questions as the same question.
    """
    def shape(windows, minute):
        return sorted((w.label, w.start, w.end, w.trade)
                      for w in windows if w.contains(minute))

    for minute in range(24 * 60):
        assert shape(m2.MODEL_2_SESSIONS, minute) == \
            shape(sess.CORE_SESSIONS, minute), minute


def test_model_2_rekeys_the_shared_table_rather_than_restating_it():
    """Move a window in the shared table and Model 2's window moves with it.

    The value-equality tests above would still pass if Model 2 restated the same
    numbers by hand — they would just both be numbers someone typed. This is the
    test that distinguishes the two: the module is re-imported against a patched
    shared table, and Model 2's window must follow it.
    """
    original = sess.SESSION_INDEX
    moved = dict(original)
    moved["ny_am"] = replace(original["ny_am"], start=6 * 60, end=10 * 60)
    sess.SESSION_INDEX = moved
    try:
        reloaded = importlib.reload(m2)
        assert reloaded.MODEL_2_SESSION_INDEX["m2_ny_am"].start == 6 * 60
        assert reloaded.MODEL_2_SESSION_INDEX["m2_ny_am"].end == 10 * 60
    finally:
        sess.SESSION_INDEX = original
        importlib.reload(m2)

    # ...and the restore actually restored, so a failure above cannot leak a
    # six-o'clock NY AM into every test that runs after this one.
    assert m2.MODEL_2_SESSION_INDEX["m2_ny_am"].start == 7 * 60


#: Modules allowed to name trading hours. ``sessions.py`` owns the table;
#: ``time_utils.py`` converts clocks, and states the NY fallback offset used only
#: on a host with no IANA database.
_HOURS_OWNERS = {"sessions.py", "time_utils.py"}


def _trading_source(name: str) -> str:
    return (Path(sess.__file__).resolve().parent / name).read_text(encoding="utf-8")


def test_no_other_model_module_defines_trading_hours():
    """A future model must reuse the framework, not invent its own schedule.

    The guard is on window *construction* rather than on some naming convention:
    a trading hour can only enter the project by building a
    :class:`trading.sessions.Window` with a start and an end, and that now
    happens in exactly one file.
    """
    pattern = re.compile(r"(?<!\w)(?:_window|Window)\s*\(")
    offenders = sorted(
        path.name
        for path in (Path(sess.__file__).resolve().parent).glob("*.py")
        if path.name not in _HOURS_OWNERS and pattern.search(
            path.read_text(encoding="utf-8"))
    )

    assert offenders == [], f"windows built outside sessions.py: {offenders}"


def test_model_2_states_not_one_time():
    """No ``"HH:MM"`` literal survives in Model 2's session module.

    Belt and braces beside the re-import test: a table that has no time in it
    cannot drift from the shared one, whatever refactoring happens around it.
    """
    assert not re.search(r'"\d{1,2}:\d{2}"', _trading_source("model2_sessions.py"))


def test_the_strategy_asks_the_shared_table_whether_it_may_enter():
    """Model 1 does not carry its own hours either — it asks this module.

    Checked structurally because the alternative (driving a full ICT setup to an
    entry) tests the strategy, which has its own file; what is being verified
    here is only that its session decision comes from the shared gate.
    """
    source = _trading_source("strategy.py")

    assert "import sessions as sess" in source
    assert re.search(r"sess\.entry_permission\(", source)


# --------------------------------------------------------------------------- #
# 2. The New York clock, and DST
# --------------------------------------------------------------------------- #
def test_the_zone_is_resolved_from_the_iana_database():
    assert tu.NY_TZ_NAME == "America/New_York"
    assert tu.NY_TZ is not None, "no tz database on this host"


def test_the_ny_offset_follows_dst_rather_than_being_fixed():
    """The one assertion that would fail on a hard-coded UTC-4 or UTC-5.

    12:00 UTC is 07:00 in New York in January and 08:00 in July. A module-level
    offset constant gets exactly one of those right.
    """
    assert tu.utc_to_ny(datetime(2026, 1, 15, 12, 0)) == datetime(2026, 1, 15, 7, 0)
    assert tu.utc_to_ny(datetime(2026, 7, 15, 12, 0)) == datetime(2026, 7, 15, 8, 0)
    assert tu.ny_offset_hours(datetime(2026, 1, 15, 12, 0)) == -5
    assert tu.ny_offset_hours(datetime(2026, 7, 15, 12, 0)) == -4


@pytest.mark.parametrize("window", sess.CORE_SESSIONS, ids=lambda w: w.key)
def test_a_session_open_is_the_same_wall_clock_in_winter_and_summer(window):
    """The windows are wall-clock intervals, and the UTC hours move under them.

    Both halves matter: the first minute of the window is inside it on both
    dates — the schedule did not shift with the season — while the UTC instant
    that corresponds to is different, so the schedule is not secretly UTC either.
    """
    jan = datetime(2026, 1, 14, window.start // 60, window.start % 60)
    jul = datetime(2026, 7, 15, window.start // 60, window.start % 60)

    assert sess.session_activity(jan)[1] == window.key
    assert sess.session_activity(jul)[1] == window.key
    assert tu.ny_to_utc(jan).hour - tu.ny_to_utc(jul).hour == 1


def test_the_utc_hour_of_a_session_boundary_shifts_with_dst():
    """Stated directly, because it is the observable consequence of the above.

    NY AM opens at 07:00 in New York on both dates; that is 12:00 UTC in January
    and 11:00 UTC in July. An implementation that added a fixed offset to the
    wall clock would report the same UTC hour twice a year and be an hour wrong
    for half of it.
    """
    jan_open = tu.ny_to_utc(datetime(2026, 1, 14, 7, 0))
    jul_open = tu.ny_to_utc(datetime(2026, 7, 15, 7, 0))

    assert (jan_open.hour, jul_open.hour) == (12, 11)
    assert tu.utc_to_ny(jan_open) == datetime(2026, 1, 14, 7, 0)
    assert tu.utc_to_ny(jul_open) == datetime(2026, 7, 15, 7, 0)


def test_the_gate_is_awake_at_the_same_ny_minute_either_side_of_the_change():
    """Lunch is Lunch at 12:30 NY in January and in July, to the minute.

    A gate driven by a fixed UTC offset would be an hour out on one side of the
    transition — asleep through half the session, awake through the following
    gap — and would look entirely normal from the inside.
    """
    jan = sess.session_activity(datetime(2026, 1, 14, 12, 30))
    jul = sess.session_activity(datetime(2026, 7, 15, 12, 30))

    assert jan == jul == (True, "ny_lunch")
    # ...and the UTC instants are an hour apart, which is the whole point.
    assert tu.ny_to_utc(datetime(2026, 1, 14, 12, 30)).hour == 17
    assert tu.ny_to_utc(datetime(2026, 7, 15, 12, 30)).hour == 16


def test_model_2s_next_open_is_correct_across_a_dst_transition():
    """Both models' searches share the UTC minute walk, so both survive DST.

    US DST starts Sunday 2026-03-08. London opens at 01:00 NY on the Monday on
    both sides of it — 06:00 UTC in EST, 05:00 UTC in EDT.
    """
    before = m2.next_session_start_utc(datetime(2026, 2, 28, 12, 0))   # Saturday
    after = m2.next_session_start_utc(datetime(2026, 3, 7, 12, 0))     # Saturday

    assert before == datetime(2026, 3, 2, 6, 0)
    assert after == datetime(2026, 3, 9, 5, 0)
    assert tu.utc_to_ny(before) == datetime(2026, 3, 2, 1, 0)
    assert tu.utc_to_ny(after) == datetime(2026, 3, 9, 1, 0)


@pytest.mark.parametrize("module", ["sessions.py", "model2_sessions.py"])
def test_the_session_modules_do_no_utc_arithmetic(module):
    """No ``timedelta(hours=...)`` in either table.

    A session boundary is a minute of the NY day; the only legal arithmetic on it
    is adding *minutes* to a NY midnight, and the only legal step to UTC is
    :func:`trading.time_utils.ny_to_utc`. An ``hours=`` delta in these files
    would mean someone had reintroduced an offset by hand.
    """
    assert "timedelta(hours" not in _trading_source(module)


# --------------------------------------------------------------------------- #
# 3. Session times on a 12-hour clock
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key,expected", [
    ("asian_range", "8:00 PM – 11:59 PM"),
    ("london_open", "1:00 AM – 5:59 AM"),
    ("ny_am", "7:00 AM – 10:59 AM"),
    ("ny_lunch", "11:00 AM – 12:59 PM"),
    ("ny_pm", "1:00 PM – 4:59 PM"),
])
def test_the_window_ranges_are_written_the_way_the_operator_states_them(key,
                                                                        expected):
    assert sess.SESSION_INDEX[key].range_12h == expected


def test_model_2_windows_display_the_same_hours_as_the_shared_ones():
    """A Model 2 alert and a Model 1 alert quote identical hours for a session."""
    shared = {w.label: w.range_12h for w in sess.CORE_SESSIONS}
    assert {w.label: w.range_12h for w in m2.MODEL_2_SESSIONS} == shared


@pytest.mark.parametrize("minute,expected", [
    (0, "12:00 AM"),        # midnight
    (1, "12:01 AM"),
    (59, "12:59 AM"),
    (60, "1:00 AM"),
    (719, "11:59 AM"),      # last minute of the morning
    (720, "12:00 PM"),      # noon
    (779, "12:59 PM"),
    (780, "1:00 PM"),
    (1439, "11:59 PM"),     # last minute of the day
    (1440, "12:00 AM"),     # the exclusive end of a midnight-closing window
])
def test_minute_of_day_to_12h_edges(minute, expected):
    assert tu.minute_of_day_to_12h(minute) == expected


def test_the_last_minute_shown_is_the_last_minute_covered():
    """``end`` is exclusive, so the closing label is ``end - 1``.

    Reading the stored end as the closing *minute* would advertise every window
    one minute long — 11:00 AM for a window that closes at 11:00 AM, which it
    never covers.
    """
    ny_am = sess.SESSION_INDEX["ny_am"]

    assert ny_am.end == 11 * 60          # stored, exclusive
    assert ny_am.last_minute_12h == "10:59 AM"
    assert ny_am.contains(10 * 60 + 59)
    assert not ny_am.contains(11 * 60)


def test_no_session_range_is_written_as_a_24_hour_clock():
    """The stored bounds must not reach an operator's screen.

    Only the forms that cannot appear legitimately are checked: ``11:00`` is both
    a 24-hour bound and a legitimate 12-hour Lunch opening, so a blanket
    substring check across the whole set would be a false alarm waiting to
    happen. These four exist *only* on the 24-hour clock.
    """
    rendered = " | ".join(w.range_12h for w in sess.CORE_SESSIONS)

    for stored_only in ("24:00", "20:00", "17:00", "13:00"):
        assert stored_only not in rendered, stored_only
    for window in sess.CORE_SESSIONS:
        assert window.start_12h.endswith(("AM", "PM"))
        assert window.last_minute_12h.endswith(("AM", "PM"))


def test_every_surface_quotes_the_same_session_hours():
    """Log, alert and dashboard all embed the window's own 12-hour string.

    Three different places render session hours, and each of them could grow its
    own formatting. They must not: a trader reading "1:00 PM – 4:59 PM" on the
    dashboard and a different range in an alert is exactly the confusion the
    shared table is meant to remove.
    """
    from notifications.telegram import _session_with_hours

    window = sess.SESSION_INDEX["ny_pm"]
    ny = WED_JUL.replace(hour=14, minute=0)

    assert window.range_12h in _session_with_hours("ny_pm")
    assert window.range_12h in _session_with_hours("m2_ny_pm")
    assert window.range_12h in sess.activity_log_line(ny, *sess.session_activity(ny))
    assert window.range_12h in sess.describe_session_at(ny)


def test_format_ny_12h_renders_the_instant_on_the_operator_clock():
    """The helper the runner uses for "next open at ..."."""
    assert tu.format_ny_12h(datetime(2026, 1, 14, 7, 0)) == "7:00 AM"
    assert tu.format_ny_12h(datetime(2026, 1, 14, 13, 0)) == "1:00 PM"
    assert tu.format_ny_12h(datetime(2026, 1, 14, 0, 30)) == "12:30 AM"
