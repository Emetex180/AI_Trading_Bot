"""ICT sessions, Silver Bullet windows and macro windows.

All windows are expressed in the project's **NY (UTC-4)** clock and are resolved
against a *minute of the NY day* so the module has no timezone knowledge of its
own — conversion is centralized in :mod:`trading.time_utils`.

Windows
-------
Core ICT sessions (all are *analyzed*; which allow entries is a config list):

    asian_range   20:00 - 00:00
    extension     00:00 - 02:00
    london_open   02:00 - 05:00
    ny_am         07:00 - 10:00
    london_close  10:00 - 12:00
    ny_pm         13:30 - 16:00
    power_hour    15:00 - 16:00

Silver Bullet windows:

    london_sb     03:00 - 04:00
    ny_am_sb      10:00 - 11:00
    ny_pm_sb      14:00 - 15:00

Macro windows (each ~20 minutes):

    pre_ny_open       08:50 - 09:10
    ny_open           09:50 - 10:10
    late_morning      10:50 - 11:10
    pm_session        11:50 - 12:10
    pm_continuation   13:50 - 14:10
    pm_close          15:50 - 16:10
"""
from __future__ import annotations

from dataclasses import dataclass

# Seconds in a day as the max minute marker (exclusive end of 00:00-windows).
DAY_MINUTES = 24 * 60


def hm_to_minutes(hhmm: str) -> int:
    """Convert 'HH:MM' (or 'HH:MM:SS') into minutes since midnight."""
    parts = hhmm.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    s = int(parts[2]) if len(parts) > 2 else 0
    if h == 24 and m == 0 and s == 0:  # "24:00" == end of day boundary
        return DAY_MINUTES
    return h * 60 + m + s // 60


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    start: int  # minute of NY day, inclusive
    end: int    # minute of NY day, exclusive

    def contains(self, minute: int) -> bool:
        return self.start <= minute < self.end

    @property
    def start_hhmm(self) -> str:
        return f"{self.start // 60:02d}:{self.start % 60:02d}"

    @property
    def end_hhmm(self) -> str:
        return "24:00" if self.end == DAY_MINUTES else f"{self.end // 60:02d}:{self.end % 60:02d}"


def _window(key: str, label: str, start_hm: str, end_hm: str) -> Window:
    return Window(key=key, label=label, start=hm_to_minutes(start_hm), end=hm_to_minutes(end_hm))


# --------------------------------------------------------------------------- #
# Definitions (single source of truth)
# --------------------------------------------------------------------------- #
CORE_SESSIONS: tuple[Window, ...] = (
    _window("asian_range", "Asian Range", "20:00", "24:00"),
    _window("extension", "Extension", "00:00", "02:00"),
    _window("london_open", "London Open", "02:00", "05:00"),
    _window("ny_am", "New York AM", "07:00", "10:00"),
    _window("london_close", "London Close", "10:00", "12:00"),
    _window("ny_pm", "New York PM", "13:30", "16:00"),
    _window("power_hour", "Power Hour", "15:00", "16:00"),
)

SILVER_BULLET_WINDOWS: tuple[Window, ...] = (
    _window("london_sb", "London SB", "03:00", "04:00"),
    _window("ny_am_sb", "NY AM SB", "10:00", "11:00"),
    _window("ny_pm_sb", "NY PM SB", "14:00", "15:00"),
)

MACRO_WINDOWS: tuple[Window, ...] = (
    _window("pre_ny_open", "Pre-NY Open", "08:50", "09:10"),
    _window("ny_open", "NY Open", "09:50", "10:10"),
    _window("late_morning", "Late Morning", "10:50", "11:10"),
    _window("pm_session", "PM Session", "11:50", "12:10"),
    _window("pm_continuation", "PM Continuation", "13:50", "14:10"),
    _window("pm_close", "PM Close", "15:50", "16:10"),
)

SESSION_INDEX: dict[str, Window] = {w.key: w for w in CORE_SESSIONS}


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #
def active_core_sessions(minute: int) -> list[Window]:
    """All core ICT sessions active at a given NY minute (may overlap)."""
    return [w for w in CORE_SESSIONS if w.contains(minute)]


def active_session_keys(minute: int) -> list[str]:
    return [w.key for w in active_core_sessions(minute)]


def active_silver_bullet(minute: int) -> Window | None:
    """The Silver Bullet window containing the minute, if any."""
    for w in SILVER_BULLET_WINDOWS:
        if w.contains(minute):
            return w
    return None


def active_silver_bullet_key(minute: int) -> str | None:
    w = active_silver_bullet(minute)
    return w.key if w else None


def active_macro_window(minute: int) -> Window | None:
    """The macro window containing the minute, if any."""
    for w in MACRO_WINDOWS:
        if w.contains(minute):
            return w
    return None


def active_macro_window_key(minute: int) -> str | None:
    w = active_macro_window(minute)
    return w.key if w else None


def primary_session(minute: int) -> Window | None:
    """Most specific active core session for display (smallest window wins)."""
    matches = active_core_sessions(minute)
    if not matches:
        return None
    # Favour the narrowest window (ties broken by definition order).
    return min(matches, key=lambda w: (w.end - w.start, list(CORE_SESSIONS).index(w)))
