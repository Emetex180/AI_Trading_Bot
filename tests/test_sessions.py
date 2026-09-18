"""Session / Silver Bullet / macro window tests.

The core session model is the spec's master table, including its trade
permission per window (``yes`` / ``no`` / ``conditional``). The overlapping
windows are resolved by :func:`session_trade_mode`, so the overlap hours are
tested explicitly — that is where an incorrect precedence would silently let a
trade through the Asian range or NY lunch.
"""
from datetime import datetime, timedelta

import pytest

from trading import time_utils as tu

from trading.sessions import (
    CORE_SESSIONS,
    DEFAULT_ENTRY_SESSIONS,
    MACRO_WINDOWS,
    OBSERVED_WINDOWS,
    OUTSIDE_SESSION_REASON,
    SESSION_INDEX,
    SILVER_BULLET_WINDOWS,
    TRADE_CLOSED,
    TRADE_CONDITIONAL,
    TRADE_NO,
    TRADE_YES,
    WEEKEND_REASON,
    Window,
    active_core_sessions,
    active_macro_window_key,
    active_session_keys,
    active_silver_bullet_key,
    activity_log_line,
    entry_permission,
    get_trading_session,
    get_trading_session_key,
    get_trading_session_label,
    hm_to_minutes,
    is_trading_day,
    next_activity_start_utc,
    normalize_session_keys,
    parse_trading_days,
    primary_session,
    session_activity,
    session_trade_mode,
    tradeable_minute,
    wake_minute,
)


def hm(h, m=0):
    return h * 60 + m


# --------------------------------------------------------------------------- #
# The master session table
# --------------------------------------------------------------------------- #
def test_spec_windows_present():
    keys = {w.key for w in CORE_SESSIONS}
    assert keys == {"asian_range", "london_open", "ny_am", "ny_pm", "power_hour"}


@pytest.mark.parametrize("key,start,end,trade", [
    ("asian_range", hm(19), hm(24), TRADE_NO),
    ("london_open", hm(2), hm(5), TRADE_YES),
    ("ny_am", hm(7), hm(11), TRADE_YES),
    ("ny_pm", hm(13), hm(15), TRADE_YES),
    ("power_hour", hm(15), hm(16), TRADE_YES),
])
def test_window_table_matches_the_spec(key, start, end, trade):
    window = next(w for w in CORE_SESSIONS if w.key == key)
    assert (window.start, window.end, window.trade) == (start, end, trade)


def test_retired_window_keys_are_gone():
    """The old seven-window table must not linger as a second source of truth."""
    keys = {w.key for w in CORE_SESSIONS}
    assert not keys & {"ny_premarket", "ny_lunch", "london_close"}


def test_the_legacy_allowlist_still_resolves():
    """An existing VALID_ENTRY_SESSIONS must keep selecting something.

    The retired keys map onto their successors rather than vanishing, or the
    operator's configured allow-list would silently resolve to nothing and no
    session would ever be tradeable.
    """
    resolve = lambda keys: normalize_session_keys(keys)
    assert resolve(["london_open", "ny_premarket", "ny_am",
                    "london_close", "ny_pm"]) == ["london_open", "ny_am", "ny_pm"]
    assert resolve(["ny_premarket"]) == ["ny_am"]
    assert resolve(["ny_lunch"]) == []          # a block with no successor
    assert resolve(["power_hour"]) == ["power_hour"]


def test_no_macro_logic_is_scheduled():
    """The model has no macro/session-macro step: macros stay informational."""
    assert active_macro_window_key(hm(9, 0)) == "pre_ny_open"   # reported only
    assert all(w.trade == TRADE_YES for w in MACRO_WINDOWS)


def test_default_entry_sessions_exclude_the_no_trade_windows():
    assert "asian_range" not in DEFAULT_ENTRY_SESSIONS
    assert {"london_open", "ny_am", "ny_pm", "power_hour"} <= set(
        DEFAULT_ENTRY_SESSIONS)


# --------------------------------------------------------------------------- #
# Boundaries
# --------------------------------------------------------------------------- #
def test_asian_range_boundaries():
    # 19:00 is inclusive, 00:00 is exclusive.
    assert "asian_range" not in active_session_keys(hm(18, 59))
    assert "asian_range" in active_session_keys(hm(19, 0))
    assert "asian_range" in active_session_keys(hm(23, 59))
    assert "asian_range" not in active_session_keys(0)


def test_the_asian_window_ends_at_midnight_rather_than_wrapping():
    """``19:00 - 00:00`` is a same-day interval ending *at* the day boundary.

    The window must not be read as wrapping (which would put the Asian range on
    the far side of midnight, contradicting the other sessions' minutes). Its
    end is ``24:00`` — minute 1440 — so it is a plain half-open interval, and
    ``spans_midnight`` must say so.
    """
    asian = SESSION_INDEX["asian_range"]
    assert (asian.start, asian.end) == (hm(19), hm(24))
    assert asian.spans_midnight is False
    assert asian.end_hhmm == "24:00"
    # Midnight belongs to no session at all — it is the next day's minute zero.
    assert active_session_keys(hm(0, 0)) == []


def test_a_genuinely_wrapping_window_is_still_supported():
    """The other shape: ``23:00 - 02:00`` really does cross midnight."""
    wrapped = Window("night", "Night", hm(23), hm(2))
    assert wrapped.spans_midnight is True
    assert wrapped.contains(hm(23, 30))
    assert wrapped.contains(hm(0, 30))
    assert wrapped.contains(hm(1, 59))
    assert not wrapped.contains(hm(2, 0))
    assert not wrapped.contains(hm(12, 0))


def test_london_open_boundaries():
    assert "london_open" not in active_session_keys(hm(1, 59))
    assert "london_open" in active_session_keys(hm(2, 0))
    assert "london_open" in active_session_keys(hm(4, 59))
    assert "london_open" not in active_session_keys(hm(5, 0))


def test_ny_am_boundaries():
    assert "ny_am" not in active_session_keys(hm(6, 59))
    assert "ny_am" in active_session_keys(hm(7, 0))
    assert "ny_am" in active_session_keys(hm(10, 59))
    assert "ny_am" not in active_session_keys(hm(11, 0))


def test_ny_pm_and_power_hour_are_adjacent_not_overlapping():
    """13:00-15:00 then 15:00-16:00: Power Hour starts as NY PM closes."""
    assert active_session_keys(hm(13, 0)) == ["ny_pm"]
    assert active_session_keys(hm(14, 59)) == ["ny_pm"]
    assert active_session_keys(hm(15, 0)) == ["power_hour"]
    assert active_session_keys(hm(15, 59)) == ["power_hour"]


def test_window_contains_exclusive_end():
    w = Window("x", "X", hm(10), hm(11))
    assert w.contains(hm(10, 59))
    assert not w.contains(hm(11))


# --------------------------------------------------------------------------- #
# No two core sessions overlap
#
# The previous seven-window table overlapped on purpose and needed a documented
# precedence rule to resolve it. The operator's five-session table does not, and
# asserting that is what keeps the simpler invariant from silently regressing.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hour,minute", [
    (2, 0), (3, 0), (4, 59), (7, 0), (9, 30), (10, 59), (13, 0), (14, 0),
    (15, 0), (15, 59), (19, 0), (22, 0), (23, 59),
])
def test_no_two_core_sessions_overlap(hour, minute):
    assert len(active_core_sessions(hm(hour, minute))) <= 1


# --------------------------------------------------------------------------- #
# Trade modes across the day
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("h, m, mode", [
    (0, 30, TRADE_CLOSED),      # gap between Asian close and London
    (1, 30, TRADE_CLOSED),
    (3, 0, TRADE_YES),          # London
    (6, 0, TRADE_CLOSED),       # 05:00-07:00 gap
    (8, 0, TRADE_YES),          # NY AM (07:00-11:00)
    (10, 30, TRADE_YES),
    (12, 0, TRADE_CLOSED),      # 11:00-13:00 gap
    (14, 0, TRADE_YES),         # NY PM
    (15, 30, TRADE_YES),        # Power Hour
    (17, 0, TRADE_CLOSED),      # after Power Hour, before Asian
    (21, 0, TRADE_NO),          # Asian range
])
def test_trade_mode_at_representative_hours(h, m, mode):
    assert session_trade_mode(hm(h, m)) == mode


def test_the_midday_gap_is_closed_rather_than_a_no_trade_session():
    """11:00-13:00 is outside every window, not a defined block.

    Worth pinning because the two states are easy to conflate: a ``no`` window
    keeps the bot awake to watch, whereas a gap sleeps and reports
    ``outside_session``.
    """
    for minute in (hm(11, 0), hm(12, 0), hm(12, 59)):
        assert session_trade_mode(minute) == TRADE_CLOSED
        assert entry_permission(minute, conditional_ok=True) == (
            False, "outside_session")


def test_no_trade_sessions_block_regardless_of_conditional_ok():
    """A `no` session blocks an otherwise qualifying setup."""
    assert entry_permission(hm(21, 0), conditional_ok=True)[0] is False
    assert entry_permission(hm(22, 30), conditional_ok=True)[1] == \
        "session_not_tradable"


def test_closed_hours_block():
    allowed, reason = entry_permission(hm(6, 0))
    assert allowed is False
    assert reason == "outside_session"


# --------------------------------------------------------------------------- #
# Conditional sessions
#
# The operator's five-session table has no CONDITIONAL window: every tradeable
# session is an outright ``yes``. The mode is kept in the model (and resolved by
# session_trade_mode for any future table), but nothing may silently depend on a
# conditional window that no longer exists.
# --------------------------------------------------------------------------- #
def test_no_session_in_the_current_table_is_conditional():
    assert all(w.trade != TRADE_CONDITIONAL for w in CORE_SESSIONS)


def test_tradeable_sessions_ignore_conditional_ok():
    """A ``yes`` session must not require the conditional bar."""
    assert entry_permission(hm(3, 0), conditional_ok=False)[0] is True
    assert entry_permission(hm(8, 0), conditional_ok=False)[0] is True
    assert entry_permission(hm(14, 0), conditional_ok=False)[0] is True
    assert entry_permission(hm(15, 30), conditional_ok=False)[0] is True


# --------------------------------------------------------------------------- #
# Allow-list
# --------------------------------------------------------------------------- #
def test_allowlist_narrows_an_allowed_session():
    allowed, reason = entry_permission(hm(14, 0), allowed_sessions=["ny_am"])
    assert allowed is False
    assert reason == "session_not_in_allowlist"
    assert entry_permission(hm(14, 0), allowed_sessions=["ny_pm"])[0] is True


def test_allowlist_does_not_widen_a_blocked_session():
    allowed, reason = entry_permission(hm(21, 0), conditional_ok=True,
                                       allowed_sessions=["asian_range"])
    assert allowed is False
    assert reason == "session_not_tradable"


def test_allowlist_written_against_the_old_table_still_permits_trading():
    """``ny_premarket``/``london_close`` resolve to ``ny_am`` rather than nothing.

    Without the alias an existing ``.env`` would exclude every session and the
    bot would scan all day without ever entering.
    """
    legacy = ["london_open", "ny_premarket", "ny_am", "london_close", "ny_pm"]
    assert entry_permission(hm(8, 0), allowed_sessions=legacy)[0] is True
    assert entry_permission(hm(14, 0), allowed_sessions=legacy)[0] is True
    # ...and it still excludes what it never listed.
    assert entry_permission(hm(15, 30), allowed_sessions=legacy)[1] == \
        "session_not_in_allowlist"


# --------------------------------------------------------------------------- #
# Labelling
# --------------------------------------------------------------------------- #
def test_primary_session_picks_the_active_window():
    assert primary_session(hm(10, 30)).key == "ny_am"
    assert primary_session(hm(3, 0)).key == "london_open"
    assert primary_session(hm(14, 0)).key == "ny_pm"
    assert primary_session(hm(15, 30)).key == "power_hour"
    assert primary_session(hm(21, 0)).key == "asian_range"
    assert primary_session(hm(1, 0)) is None


def test_get_trading_session_is_the_public_entry_point():
    """The NY instant -> session question, named the way the spec asks for it."""
    assert get_trading_session_key(MON.replace(hour=10, minute=30)) == "ny_am"
    assert get_trading_session_label(MON.replace(hour=15, minute=30)) == "Power Hour"
    assert get_trading_session_label(MON.replace(hour=6, minute=0)) == \
        "Outside session"
    assert get_trading_session(MON.replace(hour=6, minute=0)) is None


def test_at_most_one_session_is_active_at_a_time():
    assert len(active_core_sessions(hm(10, 30))) == 1
    assert len(active_core_sessions(hm(3, 0))) == 1


# --------------------------------------------------------------------------- #
# Silver Bullet windows (informational)
# --------------------------------------------------------------------------- #
def test_silver_bullet_windows():
    assert active_silver_bullet_key(hm(3, 30)) == "london_sb"
    assert active_silver_bullet_key(hm(10, 30)) == "ny_am_sb"
    assert active_silver_bullet_key(hm(14, 30)) == "ny_pm_sb"
    assert active_silver_bullet_key(hm(8, 30)) is None


def test_silver_bullet_captured_at_bounds():
    assert active_silver_bullet_key(hm(3, 0)) == "london_sb"
    assert active_silver_bullet_key(hm(4, 0)) is None


def test_silver_bullet_definitions_unchanged():
    assert [w.key for w in SILVER_BULLET_WINDOWS] == ["london_sb", "ny_am_sb", "ny_pm_sb"]


# --------------------------------------------------------------------------- #
# Macro windows (informational)
# --------------------------------------------------------------------------- #
def test_macro_windows():
    assert active_macro_window_key(hm(9, 0)) == "pre_ny_open"
    assert active_macro_window_key(hm(10, 0)) == "ny_open"
    assert active_macro_window_key(hm(11, 0)) == "late_morning"
    assert active_macro_window_key(hm(12, 0)) == "pm_session"
    assert active_macro_window_key(hm(14, 0)) == "pm_continuation"
    assert active_macro_window_key(hm(16, 0)) == "pm_close"
    assert active_macro_window_key(hm(13, 0)) is None


# --------------------------------------------------------------------------- #
# Time parsing
# --------------------------------------------------------------------------- #
def test_hm_to_minutes():
    assert hm_to_minutes("09:30") == hm(9, 30)
    assert hm_to_minutes("00:00") == 0
    assert hm_to_minutes("24:00") == 24 * 60


# --------------------------------------------------------------------------- #
# The trading calendar
#
# A minute-of-day table cannot tell a Wednesday from a Saturday, so every window
# above would report "tradeable" over a weekend. These tests pin the calendar
# dimension that fixes it — the Saturday case is the regression that matters.
# --------------------------------------------------------------------------- #
#: Fixed reference dates (weekdays asserted in the tests, so a wrong assumption
#: fails loudly instead of silently testing the wrong day).
SAT = datetime(2026, 9, 12, 3, 0)     # Saturday 03:00 NY
SUN = datetime(2026, 9, 13, 22, 0)    # Sunday 22:00 NY (Asian range)
MON = datetime(2026, 9, 14)           # Monday
WED = datetime(2026, 9, 16)           # Wednesday

#: Every session the spec marks tradeable, pinned explicitly so the calendar
#: tests never depend on the developer's `.env` VALID_ENTRY_SESSIONS.
ALLOW_SESSIONS = ["london_open", "ny_am", "ny_pm", "power_hour"]


def test_parse_trading_days():
    assert parse_trading_days("mon,tue,wed,thu,fri") == frozenset({0, 1, 2, 3, 4})
    # Case and surrounding whitespace are tolerated...
    assert parse_trading_days(" MON , Fri ") == frozenset({0, 4})
    # ...as is an iterable rather than a string.
    assert parse_trading_days(["sat", "sun"]) == frozenset({5, 6})
    # A typo must not stop the bot starting: unknown names are dropped and an
    # empty result falls back to the Mon-Fri week.
    assert parse_trading_days("nonsense") == frozenset({0, 1, 2, 3, 4})
    assert parse_trading_days("") == frozenset({0, 1, 2, 3, 4})
    assert parse_trading_days(None) == frozenset({0, 1, 2, 3, 4})


def test_the_reference_dates_are_the_days_the_tests_claim():
    assert (SAT.weekday(), SUN.weekday()) == (5, 6)
    assert MON.weekday() == 0 and WED.weekday() == 2


def test_is_trading_day_defaults_to_the_mon_fri_week():
    assert is_trading_day(MON) and is_trading_day(WED)
    assert not is_trading_day(SAT) and not is_trading_day(SUN)
    # An explicit set overrides the default.
    assert is_trading_day(SAT, frozenset({5})) and not is_trading_day(MON, frozenset({5}))


def test_a_saturday_morning_is_not_tradeable_though_london_reports_open():
    """The whole reason the calendar exists.

    ``02:00-05:00`` is the London open, and the minute-of-day table says so on
    every day of the week — ``session_trade_mode`` cannot know it is Saturday.
    Left to the window table alone the bot would scan and trade a closed market.
    """
    assert session_trade_mode(hm(3, 0)) == TRADE_YES       # the window table
    assert session_activity(SAT) == (False, WEEKEND_REASON)  # the calendar
    assert session_activity(SUN) == (False, WEEKEND_REASON)
    # Sunday 22:00 is the Asian range on a Monday, but still the weekend.
    assert active_session_keys(hm(22, 0)) == ["asian_range"]
    assert session_activity(SUN) == (False, WEEKEND_REASON)


@pytest.mark.parametrize("hour,minute,expected", [
    (2, 0, "london_open"),      # open
    (2, 30, "london_open"),
    (4, 59, "london_open"),     # last minute before the 05:00 close
    (5, 0, None),               # the London window has closed
    (6, 0, None),               # nothing at all is open
    (7, 0, "ny_am"),            # NY AM opens
    (10, 59, "ny_am"),
    (11, 0, None),              # 11:00-13:00 is a genuine gap, not a block
    (12, 0, None),
    (13, 0, "ny_pm"),
    (14, 59, "ny_pm"),
    (15, 0, "power_hour"),      # Power Hour follows NY PM directly
    (15, 59, "power_hour"),
    (16, 0, None),              # Power Hour closed
    (19, 0, None),              # Asian range: a defined window, but a NO one
    (22, 0, None),
    (23, 59, None),
])
def test_tradeable_minute_boundaries(hour, minute, expected):
    assert tradeable_minute(hm(hour, minute)) == expected


def test_session_activity_is_active_on_a_weekday_inside_a_window():
    assert session_activity(MON.replace(hour=10, minute=0)) == (True, "ny_am")
    assert session_activity(WED.replace(hour=14, minute=0)) == (True, "ny_pm")
    # Asleep on a weekday, but for the other reason.
    assert session_activity(MON.replace(hour=6, minute=0)) == (
        False, OUTSIDE_SESSION_REASON)


def test_an_excluded_session_is_not_worth_waking_up_for():
    """The allow-list is the operator's, and the gate respects it.

    ``london_open`` is a tradeable window, but an operator who has left it out of
    ``VALID_ENTRY_SESSIONS`` has no use for a session that can never enter — so
    the bot should not wake for it either.
    """
    allow = ["ny_am", "ny_pm"]
    assert tradeable_minute(hm(3, 0)) == "london_open"
    assert tradeable_minute(hm(3, 0), allowed_sessions=allow) is None
    assert tradeable_minute(hm(10, 0), allowed_sessions=allow) == "ny_am"
    assert session_activity(MON.replace(hour=3, minute=0),
                            allowed_sessions=allow) == (False,
                                                        OUTSIDE_SESSION_REASON)
    # An empty allow-list means "no additional restriction", not "nothing".
    assert tradeable_minute(hm(3, 0), allowed_sessions=[]) == "london_open"


def test_next_activity_start_utc_skips_the_weekend():
    """From Saturday morning the next open is Monday's London session."""
    nxt = next_activity_start_utc(SAT)
    assert nxt == datetime(2026, 9, 14, 6, 0)      # 02:00 EDT
    assert tu.utc_to_ny(nxt) == datetime(2026, 9, 14, 2, 0)


def test_next_activity_start_utc_walks_the_weekday_gaps():
    """Asleep inside a trading day, the next open is that day's next window."""
    # 06:00 Monday -> 07:00 (NY AM), not Tuesday.
    nxt = next_activity_start_utc(MON.replace(hour=6, minute=0))
    assert tu.utc_to_ny(nxt) == datetime(2026, 9, 14, 7, 0)
    # 11:45 sits in the midday gap -> 13:00 (NY PM), the far side of the gap.
    nxt = next_activity_start_utc(MON.replace(hour=11, minute=45))
    assert tu.utc_to_ny(nxt) == datetime(2026, 9, 14, 13, 0)
    # 16:30, after NY PM closes -> the Asian range the same evening. It is an
    # observed window, so it counts: the bot wakes to watch a level it may not
    # trade, which is exactly the distinction wake_minute exists to draw.
    nxt = next_activity_start_utc(MON.replace(hour=16, minute=30))
    assert tu.utc_to_ny(nxt) == datetime(2026, 9, 14, 19, 0)
    # 00:30, after the Asian range closes -> the same morning's London open.
    nxt = next_activity_start_utc(MON.replace(hour=0, minute=30))
    assert tu.utc_to_ny(nxt) == datetime(2026, 9, 14, 2, 0)


def test_next_activity_start_utc_is_correct_across_a_dst_transition():
    """The same NY wall clock on either side of the spring-forward.

    US DST starts Sunday 2026-03-08. A week earlier the London open is 07:00 UTC
    (EST); after the change it is 06:00 UTC (EDT). Both are 02:00 on the NY clock
    — which is the assertion that actually matters, because the gate is defined
    in NY wall-clock terms. Wall-clock arithmetic on a naive NY datetime gets
    this wrong by an hour; stepping UTC and converting back cannot.
    """
    before = next_activity_start_utc(datetime(2026, 2, 28, 12, 0))   # Saturday
    after = next_activity_start_utc(datetime(2026, 3, 7, 12, 0))     # Saturday

    assert before == datetime(2026, 3, 2, 7, 0)    # Mon 02:00 EST
    assert after == datetime(2026, 3, 9, 6, 0)     # Mon 02:00 EDT
    assert tu.utc_to_ny(before) == datetime(2026, 3, 2, 2, 0)
    assert tu.utc_to_ny(after) == datetime(2026, 3, 9, 2, 0)


def test_next_activity_start_utc_gives_up_when_no_day_qualifies():
    """An empty day set is honoured literally — ``None`` means Mon-Fri, not this.

    Every day is refused, so the search exhausts its horizon and reports that
    there is no open to wait for rather than looping or guessing.
    """
    assert next_activity_start_utc(SAT, days=frozenset()) is None
    # A day set that *does* include Saturday makes the very next minute active,
    # which is the same search succeeding for the opposite reason.
    assert next_activity_start_utc(SAT, days=frozenset({5})) == datetime(
        2026, 9, 12, 7, 1)


# --------------------------------------------------------------------------- #
# The week as a whole
#
# Walking every minute of a simulated week is the only way to state the
# requirement the operator actually gave ("Monday to Friday, only during the
# session") as an assertion, rather than testing the boundaries one at a time
# and hoping the gaps between them add up.
# --------------------------------------------------------------------------- #
#: The spans the bot should be awake for on a weekday, as minutes of the NY day.
#: The last one is the Asian range: no entry can be taken in it, but the bot is
#: awake to observe the level the London open trades against, so it counts as
#: awake here.
EXPECTED_WEEKDAY_SPANS = [(2 * 60, 5 * 60),         # London
                          (7 * 60, 11 * 60),        # NY AM
                          (13 * 60, 16 * 60),       # NY PM + Power Hour (adjacent)
                          (19 * 60, 24 * 60)]       # Asian range (observed)
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _awake_spans(monday: datetime) -> dict[str, list[tuple[int, int]]]:
    """Contiguous awake spans per day of the week beginning ``monday``."""
    spans: dict[str, list[tuple[int, int]]] = {}
    for offset, day_name in enumerate(DAY_NAMES):
        date = monday + timedelta(days=offset)
        found: list[tuple[int, int]] = []
        start = None
        for minute in range(24 * 60):
            ny = date.replace(hour=minute // 60, minute=minute % 60)
            active, _ = session_activity(ny, allowed_sessions=ALLOW_SESSIONS)
            if active and start is None:
                start = minute
            elif not active and start is not None:
                found.append((start, minute))
                start = None
        if start is not None:
            found.append((start, 24 * 60))
        spans[day_name] = found
    return spans


WINTER_WEEK = datetime(2026, 1, 5)     # Mon 2026-01-05, EST
SUMMER_WEEK = datetime(2026, 7, 6)     # Mon 2026-07-06, EDT


def test_the_week_has_exactly_the_expected_awake_spans():
    spans = _awake_spans(WINTER_WEEK)
    for day in DAY_NAMES[:5]:
        assert spans[day] == EXPECTED_WEEKDAY_SPANS, day
    assert spans["Sat"] == [] and spans["Sun"] == []


def test_the_awake_spans_do_not_move_with_the_season():
    """The gate is defined on the NY wall clock, so DST must not shift it.

    Walking the same two weeks either side of the year is what catches a
    hard-coded UTC-4: the spans would come out an hour out in winter and this
    would be the test that noticed.
    """
    assert _awake_spans(WINTER_WEEK) == _awake_spans(SUMMER_WEEK)


# --------------------------------------------------------------------------- #
# Observed windows — awake, but not tradeable
#
# The Asian range is a hard no-trade block that the bot nonetheless stays awake
# for, because the level it builds is the liquidity the London open sweeps. The
# two questions ("may we enter?" and "should we be watching?") therefore have to
# be separate functions; collapsing them would either sleep through the range or
# treat it as an entry window.
# --------------------------------------------------------------------------- #
def test_an_observed_window_wakes_the_bot_without_trading_it():
    assert "asian_range" in OBSERVED_WINDOWS
    # Awake: there is something to watch.
    assert session_activity(MON.replace(hour=21, minute=0)) == (True,
                                                                "asian_range")
    # Not tradeable: the entry path refuses it, independently of the gate.
    assert tradeable_minute(hm(21, 0)) is None
    assert entry_permission(hm(21, 0), conditional_ok=True)[0] is False


def test_observed_windows_are_not_subject_to_the_entry_allowlist():
    """Excluding a session you cannot enter must not skip the level it builds.

    An operator trading only ``ny_am`` still wants the Asian range for it, so
    the allow-list filters entries, not observation.
    """
    allow = ["ny_am"]
    assert wake_minute(hm(21, 0), allowed_sessions=allow) == "asian_range"
    # The allow-list still filters what it is meant to filter.
    assert wake_minute(hm(3, 0), allowed_sessions=allow) is None


def test_observing_one_window_does_not_soften_another_windows_block():
    """Only listed windows are observed; the blocking rule is otherwise intact.

    Observing the Asian range must not turn "a no window blocks" into "a no
    window blocks unless we observe some other window entirely". The 11:00-13:00
    gap is a second, weaker check: being awake for one window must never extend
    the awake span into hours no window covers.
    """
    assert wake_minute(hm(21, 0)) == "asian_range"   # observed, so awake
    assert wake_minute(hm(12, 0)) is None            # a gap stays asleep
    assert wake_minute(hm(0, 30)) is None


@pytest.mark.parametrize("ny,active,reason,expected", [
    # Awake on a tradeable window.
    (MON.replace(hour=3, minute=0), True, "london_open",
     "[SESSION] London session active — Strategy scanner ON"),
    (MON.replace(hour=10, minute=0), True, "ny_am",
     "[SESSION] NY AM active — Strategy scanner ON"),
    (MON.replace(hour=14, minute=0), True, "ny_pm",
     "[SESSION] NY PM active — Strategy scanner ON"),
    (MON.replace(hour=15, minute=30), True, "power_hour",
     "[SESSION] Power Hour active — Strategy scanner ON"),
    # Awake on a window it only watches.
    (MON.replace(hour=21, minute=0), True, "asian_range",
     "[SESSION] Asian session active — Building liquidity"),
    # Asleep between windows.
    (MON.replace(hour=6, minute=0), False, OUTSIDE_SESSION_REASON,
     "[SESSION] Outside trading window — Scanner idle"),
    (MON.replace(hour=16, minute=30), False, OUTSIDE_SESSION_REASON,
     "[SESSION] Outside trading window — Scanner idle"),
    # The midday gap: nothing is open, so the line names no window at all.
    (MON.replace(hour=12, minute=0), False, OUTSIDE_SESSION_REASON,
     "[SESSION] Outside trading window — Scanner idle"),
    (SAT, False, WEEKEND_REASON, "[SESSION] Saturday — Weekend mode"),
    (SUN, False, WEEKEND_REASON, "[SESSION] Sunday — Weekend mode"),
])
def test_the_activity_log_line_matches_the_specified_wording(ny, active, reason,
                                                             expected):
    assert activity_log_line(ny, active, reason) == expected


def test_each_session_state_renders_a_distinct_log_line():
    """The line is the log's only record of a transition.

    The live loop suppresses repeats by comparing rendered lines, so two
    *different* states that rendered the same string would silently lose a
    transition. (Two minutes inside the *same* state rendering the same string
    is not that: there is no transition between them, and suppressing the
    repeat is the point.)
    """
    states = [                      # one representative minute per distinct state
        (0, "[SESSION] Outside trading window — Scanner idle"),
        (3, "[SESSION] London session active — Strategy scanner ON"),
        (10, "[SESSION] NY AM active — Strategy scanner ON"),
        (14, "[SESSION] NY PM active — Strategy scanner ON"),
        (15, "[SESSION] Power Hour active — Strategy scanner ON"),
        (21, "[SESSION] Asian session active — Building liquidity"),
    ]
    rendered: dict[str, int] = {}
    for hour, expected in states:
        ny = MON.replace(hour=hour)
        line = activity_log_line(ny, *session_activity(ny))
        assert line == expected, f"{hour}:00 rendered {line!r}"
        rendered[line] = hour
    assert len(rendered) == len(states), rendered

