"""Session / Silver Bullet / macro window tests."""
from trading.sessions import (
    MACRO_WINDOWS,
    SILVER_BULLET_WINDOWS,
    Window,
    active_core_sessions,
    active_macro_window,
    active_macro_window_key,
    active_session_keys,
    active_silver_bullet,
    active_silver_bullet_key,
    hm_to_minutes,
    primary_session,
)


def hm(h, m):
    return h * 60 + m


def test_spec_windows_present():
    keys = {w.key for w in active_core_sessions(0)} | {w.key for w in _all_core()}
    for expected in ["asian_range", "extension", "london_open", "ny_am",
                     "london_close", "ny_pm", "power_hour"]:
        assert expected in keys


def _all_core():
    # return the core session definitions through a representative active query set
    from trading.sessions import CORE_SESSIONS
    return CORE_SESSIONS


def test_boundaries():
    # 20:00 exactly belongs to Asian range (start inclusive).
    assert "asian_range" in active_session_keys(hm(20, 0))
    # 00:30 belongs to extension (00:00-02:00).
    assert active_session_keys(hm(0, 30)) == ["extension"]
    # 02:00 exactly -> London open (extension ended at 02:00, exclusive).
    assert "london_open" in active_session_keys(hm(2, 0))


def test_ny_am_and_overlaps():
    assert "ny_am" in active_session_keys(hm(8, 30))
    # 15:30 is both NY PM and Power Hour.
    keys = active_session_keys(hm(15, 30))
    assert {"ny_pm", "power_hour"}.issubset(set(keys))


def test_silver_bullet_windows():
    assert active_silver_bullet_key(hm(3, 30)) == "london_sb"
    assert active_silver_bullet_key(hm(10, 30)) == "ny_am_sb"
    assert active_silver_bullet_key(hm(14, 30)) == "ny_pm_sb"
    assert active_silver_bullet_key(hm(8, 30)) is None


def test_macro_windows():
    assert active_macro_window_key(hm(9, 0)) == "pre_ny_open"
    assert active_macro_window_key(hm(10, 0)) == "ny_open"
    assert active_macro_window_key(hm(11, 0)) == "late_morning"
    assert active_macro_window_key(hm(12, 0)) == "pm_session"
    assert active_macro_window_key(hm(14, 0)) == "pm_continuation"
    assert active_macro_window_key(hm(16, 0)) == "pm_close"
    assert active_macro_window_key(hm(13, 0)) is None


def test_primary_session_narrows():
    assert primary_session(hm(15, 30)).key == "power_hour"  # narrowest of ny_pm/ph
    assert primary_session(hm(8, 30)).key == "ny_am"
    assert primary_session(hm(0, 30)).key == "extension"


def test_window_contains_exclusive_end():
    w = Window("x", "X", hm(10, 0), hm(11, 0))
    assert w.contains(hm(10, 59))
    assert not w.contains(hm(11, 0))


def test_silver_bullet_captured_at_bounds():
    # 03:00 is inclusive, 04:00 exclusive.
    assert active_silver_bullet_key(hm(3, 0)) == "london_sb"
    assert active_silver_bullet_key(hm(4, 0)) is None
