"""Deterministic liquidity detection.

Operates on *closed* candles only and derives:

* previous-day high/low (by NY calendar day),
* most recently completed session extremes (Asian, London, and the generic
  "last completed session"),
* previous-hour high/low,
* swing highs/lows (pivot points),
* equal highs/lows (swing clusters within a configurable tolerance),
* liquidity purges / sweeps (a close that *reclaims* a swept level).

No third-party indicators are used. Every function is pure and unit-testable;
timeframe conversion is not needed here because candles already carry NY time.

Convention
----------
A **sell-side** level (PDL, session low, swing low, equal lows) sits below price;
a sweep *below* it followed by a reclaiming close is a bullish (BUY) premise.
A **buy-side** level sits above price; a sweep *above* it with a reclaiming close
is a bearish (SELL) premise.

Liquidity priority
------------------
Every level carries a :attr:`Level.grade` from :data:`LIQUIDITY_GRADES`, the
strategy's single priority table:

    VERY_HIGH    previous-day high/low
    HIGH         equal highs/lows, session highs/lows, Asian high/low
    MEDIUM_HIGH  London high/low
    MEDIUM       previous-hour high/low, recent swing high/low

The grade ranks a sweep's significance (which level a purge is *anchored* on)
and acts as the quality filter/tie-break when a take-profit target is chosen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as _time, timedelta

from .sessions import CORE_SESSIONS, SESSION_INDEX, Window

# --------------------------------------------------------------------------- #
# Liquidity priority
# --------------------------------------------------------------------------- #
VERY_HIGH = "VERY_HIGH"
HIGH = "HIGH"
MEDIUM_HIGH = "MEDIUM_HIGH"
MEDIUM = "MEDIUM"

#: Priority of each level kind. Lower rank number = stronger liquidity.
GRADE_RANK: dict[str, int] = {
    VERY_HIGH: 4,
    HIGH: 3,
    MEDIUM_HIGH: 2,
    MEDIUM: 1,
}

LIQUIDITY_GRADES: dict[str, str] = {
    "PDH": VERY_HIGH, "PDL": VERY_HIGH,
    "EQ_HIGH": HIGH, "EQ_LOW": HIGH,
    "SESSION_HIGH": HIGH, "SESSION_LOW": HIGH,
    "ASIAN_HIGH": HIGH, "ASIAN_LOW": HIGH,
    "LONDON_HIGH": MEDIUM_HIGH, "LONDON_LOW": MEDIUM_HIGH,
    "PREV_HOUR_HIGH": MEDIUM, "PREV_HOUR_LOW": MEDIUM,
    "SWING_HIGH": MEDIUM, "SWING_LOW": MEDIUM,
}

_UNKNOWN_GRADE = MEDIUM

_BUY_SIDE_KINDS = frozenset({
    "PDH", "EQ_HIGH", "SESSION_HIGH", "ASIAN_HIGH", "LONDON_HIGH",
    "PREV_HOUR_HIGH", "SWING_HIGH",
})


def grade_rank(grade: str | None) -> int:
    """Numeric rank for a grade name; 0 when unknown/None."""
    return GRADE_RANK.get((grade or "").upper(), 0)


def grade_at_least(grade: str | None, minimum: str | None) -> bool:
    """True when ``grade`` is at least ``minimum`` (an unknown grade never is)."""
    if not minimum:
        return True
    return grade_rank(grade) >= grade_rank(minimum)


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Swing:
    kind: str                      # "H" high pivot, "L" low pivot
    price: float
    index: int                     # index within the supplied candle list
    time_ny: datetime

    @property
    def is_high(self) -> bool:
        return self.kind == "H"


@dataclass(frozen=True)
class EqualLevel:
    kind: str                      # "EQ_HIGH" | "EQ_LOW"
    price: float
    touches: int
    last_index: int
    time_ny: datetime


@dataclass(frozen=True)
class Level:
    """A resting liquidity reference (target or sweep level)."""

    kind: str    # PDH|PDL|SESSION_HIGH|SESSION_LOW|ASIAN_HIGH|ASIAN_LOW|
                 # LONDON_HIGH|LONDON_LOW|PREV_HOUR_HIGH|PREV_HOUR_LOW|
                 # SWING_HIGH|SWING_LOW|EQ_HIGH|EQ_LOW
    price: float
    time_ny: datetime

    @property
    def buy_side(self) -> bool:
        """True when resting liquidity sits *above* price (buy-side)."""
        return self.kind in _BUY_SIDE_KINDS

    @property
    def sell_side(self) -> bool:
        return not self.buy_side

    @property
    def grade(self) -> str:
        """Priority of this level (see :data:`LIQUIDITY_GRADES`)."""
        return LIQUIDITY_GRADES.get(self.kind, _UNKNOWN_GRADE)

    @property
    def label(self) -> str:
        """Human-readable name for alerts ("Asian High", "Previous Day Low")."""
        return LEVEL_LABELS.get(self.kind, self.kind.replace("_", " ").title())


#: Display names for alert text.
LEVEL_LABELS: dict[str, str] = {
    "PDH": "Previous Day High", "PDL": "Previous Day Low",
    "EQ_HIGH": "Equal Highs", "EQ_LOW": "Equal Lows",
    "SESSION_HIGH": "Session High", "SESSION_LOW": "Session Low",
    "ASIAN_HIGH": "Asian High", "ASIAN_LOW": "Asian Low",
    "LONDON_HIGH": "London High", "LONDON_LOW": "London Low",
    "PREV_HOUR_HIGH": "Previous Hour High", "PREV_HOUR_LOW": "Previous Hour Low",
    "SWING_HIGH": "Recent Swing High", "SWING_LOW": "Recent Swing Low",
}


@dataclass(frozen=True)
class SweepEvent:
    direction: str                 # "bullish" (sell-side swept) | "bearish" (buy-side swept)
    level: Level
    distance: float                # how far price traded beyond the level
    candle_index: int
    time_ny: datetime


# --------------------------------------------------------------------------- #
# Swing / equal-level detection
# --------------------------------------------------------------------------- #
def _strict_high(candles, i: int, lookback: int) -> bool:
    lo = max(0, i - lookback)
    hi = min(len(candles), i + lookback + 1)
    center = candles[i].high
    for j in range(lo, hi):
        if j == i:
            continue
        if candles[j].high >= center:
            return False
    return True


def _strict_low(candles, i: int, lookback: int) -> bool:
    lo = max(0, i - lookback)
    hi = min(len(candles), i + lookback + 1)
    center = candles[i].low
    for j in range(lo, hi):
        if j == i:
            continue
        if candles[j].low <= center:
            return False
    return True


def find_swings(candles: list, lookback: int = 3) -> list[Swing]:
    """Return confirmed pivot highs/lows.

    A pivot requires ``lookback`` candles on each side — so only candles that
    already have their right-hand context are ever classified (no lookahead).
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    swings: list[Swing] = []
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        if _strict_high(candles, i, lookback):
            swings.append(Swing("H", c.high, i, c.t_ny))
        elif _strict_low(candles, i, lookback):
            swings.append(Swing("L", c.low, i, c.t_ny))
    return swings


def equal_levels(swings: list[Swing], tolerance: float) -> list[EqualLevel]:
    """Cluster consecutive same-kind swings whose prices are within ``tolerance``.

    A cluster needs at least two touches to count as an equal-high / equal-low.
    """
    clusters: list[EqualLevel] = []
    i = 0
    while i < len(swings):
        kind = swings[i].kind
        j = i
        group = [swings[i]]
        while j + 1 < len(swings) and swings[j + 1].kind == kind \
                and abs(swings[j + 1].price - group[0].price) <= tolerance:
            j += 1
            group.append(swings[j])
        if len(group) >= 2:
            price = max(s.price for s in group) if kind == "H" else min(s.price for s in group)
            last = group[-1]
            clusters.append(EqualLevel("EQ_HIGH" if kind == "H" else "EQ_LOW",
                                       price, len(group), last.index, last.time_ny))
        i = j + 1
    return clusters


# --------------------------------------------------------------------------- #
# Day / session references
# --------------------------------------------------------------------------- #
def day_extremes(candles: list, ny_date: str) -> tuple[float, float] | None:
    """(high, low) over closed candles belonging to one NY calendar day."""
    highs = [c.high for c in candles if c.t_ny.strftime("%Y-%m-%d") == ny_date]
    if not highs:
        return None
    lows = [c.low for c in candles if c.t_ny.strftime("%Y-%m-%d") == ny_date]
    return max(highs), min(lows)


def previous_day_high_low(candles: list, ny_date: str, max_lookback_days: int = 5) -> tuple[float, float] | None:
    """(high, low) of the most recent prior NY trading day that has data."""
    day = datetime.strptime(ny_date, "%Y-%m-%d")
    for _ in range(max_lookback_days):
        day -= timedelta(days=1)
        ext = day_extremes(candles, day.strftime("%Y-%m-%d"))
        if ext is not None:
            return ext
    return None


def session_extremes(candles: list, window: Window, ny_date: str,
                     upto_index: int | None = None) -> tuple[float, float] | None:
    """(high, low) of closed candles within one session window on a NY day.

    ``upto_index`` optionally restricts analysis to candles before that index
    (exclusive) to avoid peeking at candles that had not yet closed.
    """
    end = len(candles) if upto_index is None else upto_index
    highs: list[float] = []
    lows: list[float] = []
    for i in range(end):
        c = candles[i]
        if c.t_ny.strftime("%Y-%m-%d") != ny_date:
            continue
        minute = c.t_ny.hour * 60 + c.t_ny.minute
        if window.contains(minute):
            highs.append(c.high)
            lows.append(c.low)
    if not highs:
        return None
    return max(highs), min(lows)


def _session_close_ny(day: date, window: Window) -> datetime:
    """The NY instant a session window closes on a given NY date."""
    return datetime.combine(day, _time(0, 0)) + timedelta(minutes=window.end)


def last_completed_session(candles: list, window: Window, ref_ny: datetime,
                           lookback_days: int = 3
                           ) -> tuple[str, tuple[float, float]] | None:
    """Extremes of the most recent *completed* instance of ``window``.

    A session only counts once its closing instant is at or before ``ref_ny``,
    so the Asian range taken at 09:00 NY is *yesterday's* (20:00-24:00), and a
    London range read at 09:00 NY is that same morning's (02:00-05:00). Returns
    ``(ny_date, (high, low))`` or ``None``.
    """
    for offset in range(0, lookback_days + 1):
        day = (ref_ny - timedelta(days=offset)).date()
        if _session_close_ny(day, window) > ref_ny:
            continue
        ext = session_extremes(candles, window, day.strftime("%Y-%m-%d"))
        if ext is not None:
            return day.strftime("%Y-%m-%d"), ext
    return None


def last_completed_any_session(candles: list, ref_ny: datetime,
                               lookback_days: int = 3
                               ) -> tuple[Window, tuple[float, float]] | None:
    """Extremes of whichever core session closed most recently before ``ref_ny``."""
    candidates: list[tuple[datetime, date, Window]] = []
    for offset in range(0, lookback_days + 1):
        day = (ref_ny - timedelta(days=offset)).date()
        for window in CORE_SESSIONS:
            close_dt = _session_close_ny(day, window)
            if close_dt <= ref_ny:
                candidates.append((close_dt, day, window))
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _close_dt, day, window in candidates:
        ext = session_extremes(candles, window, day.strftime("%Y-%m-%d"))
        if ext is not None:
            return window, ext
    return None


# --------------------------------------------------------------------------- #
# Level snapshot
# --------------------------------------------------------------------------- #
def build_level_snapshot(candles: list, as_of_date: str | None = None,
                         lookback_swings: int = 3,
                         swing_tolerance: float | None = None,
                         atr: float | None = None,
                         as_of_ny: datetime | None = None,
                         session_levels: bool = True,
                         prev_hour: bool = True,
                         session_lookback_days: int = 3) -> list[Level]:
    """Assemble resting liquidity levels from the supplied (history) candles.

    PDH/PDL are computed relative to ``as_of_date`` (the NY date of the candle
    being tested for a sweep) using data strictly *before* that date. Pass
    ``as_of_date=None`` to use the last candle's own date. ``as_of_ny`` is the
    NY instant "now" for the session-level lookbacks and defaults to the last
    candle's own time.

    IMPORTANT: callers must pass a history slice that ends *before* the candle
    being tested — otherwise the sweep candle's own wick would appear as a
    swing / equal level (lookahead).
    """
    if not candles:
        return []
    ref_date = as_of_date or candles[-1].t_ny.strftime("%Y-%m-%d")
    ref_ny = as_of_ny or candles[-1].t_ny
    tolerance = swing_tolerance
    if tolerance is None and atr is not None:
        tolerance = atr * 0.2
    if tolerance is None:
        tolerance = 0.0

    levels: list[Level] = []

    # Previous NY day high/low (relative to the reference day).
    pd = previous_day_high_low(candles, ref_date)
    if pd is not None:
        pdh, pdl = pd
        levels.append(Level("PDH", pdh, candles[0].t_ny))
        levels.append(Level("PDL", pdl, candles[0].t_ny))

    # Session extremes: the generic "last completed session", plus the named
    # Asian and London ranges the spec calls out explicitly.
    if session_levels:
        generic = last_completed_any_session(candles, ref_ny,
                                             lookback_days=session_lookback_days)
        if generic is not None:
            window, (hi, lo) = generic
            if window.trade != "no" or window.key == "asian_range":
                levels.append(Level("SESSION_HIGH", hi, candles[-1].t_ny))
                levels.append(Level("SESSION_LOW", lo, candles[-1].t_ny))

        named = (("asian_range", "ASIAN_HIGH", "ASIAN_LOW"),
                 ("london_open", "LONDON_HIGH", "LONDON_LOW"))
        for key, high_kind, low_kind in named:
            window = SESSION_INDEX.get(key)
            if window is None:  # pragma: no cover - guarded by the table above
                continue
            found = last_completed_session(candles, window, ref_ny,
                                           lookback_days=session_lookback_days)
            if found is None:
                continue
            _day, (hi, lo) = found
            levels.append(Level(high_kind, hi, candles[-1].t_ny))
            levels.append(Level(low_kind, lo, candles[-1].t_ny))

    # Previous-hour high/low: the bar immediately before the reference candle.
    if prev_hour:
        prev = candles[-1]
        levels.append(Level("PREV_HOUR_HIGH", prev.high, prev.t_ny))
        levels.append(Level("PREV_HOUR_LOW", prev.low, prev.t_ny))

    # Swings + equal highs/lows.
    swings = find_swings(candles, lookback=lookback_swings)
    for s in swings:
        kind = "SWING_HIGH" if s.is_high else "SWING_LOW"
        levels.append(Level(kind, s.price, s.time_ny))
    if tolerance > 0:
        for eq in equal_levels(swings, tolerance):
            levels.append(Level(eq.kind, eq.price, eq.time_ny))
    return levels


# --------------------------------------------------------------------------- #
# Sweep detection
# --------------------------------------------------------------------------- #
def detect_sweeps(candle, levels: list[Level], tolerance: float = 0.0) -> list[SweepEvent]:
    """Detect liquidity purges for one closed candle against known levels.

    A purge requires the candle to trade *beyond* the level and *close back on
    the near side* (a reclaim). ``tolerance`` lets you require a minimum
    overshoot distance. Returns both bullish and bearish events when present.
    """
    events: list[SweepEvent] = []
    for lvl in levels:
        if lvl.sell_side and candle.low < lvl.price and candle.close > lvl.price:
            distance = lvl.price - candle.low
            if distance >= tolerance:
                events.append(SweepEvent("bullish", lvl, distance, 0, candle.t_ny))
        elif lvl.buy_side and candle.high > lvl.price and candle.close < lvl.price:
            distance = candle.high - lvl.price
            if distance >= tolerance:
                events.append(SweepEvent("bearish", lvl, distance, 0, candle.t_ny))
    return events


# Significance ranking used when a single purge candle sweeps a *cascade* of
# resting sell-side levels (e.g. an overnight swing low taken en route to the
# previous-day low). ICT anchors a purge on the most significant liquidity that
# was actually swept; minor swings only become the reference when nothing more
# significant is swept. Lower number = more significant.
#
# Derived from the spec's liquidity priority table (`LIQUIDITY_GRADES`): the
# grade of the swept level *is* its significance, and distance only breaks ties
# between equally graded levels.
PURGE_SIGNIFICANCE: dict[str, int] = {
    kind: 4 - GRADE_RANK[grade] for kind, grade in LIQUIDITY_GRADES.items()
}


def _purge_rank(level: Level) -> int:
    return PURGE_SIGNIFICANCE.get(level.kind, 99)


def strongest_purge(events: list[SweepEvent], direction: str) -> SweepEvent | None:
    """The purge of a given direction that matters most to the trade.

    Selects the swept level of highest significance (PDH/PDL first, per
    :data:`PURGE_SIGNIFICANCE`, which mirrors the liquidity priority table);
    among equally significant levels, the one with the deepest overshoot wins.
    """
    matches = [e for e in events if e.direction == direction]
    if not matches:
        return None
    return min(matches, key=lambda e: (_purge_rank(e.level), -e.distance))


# --------------------------------------------------------------------------- #
# Target selection
# --------------------------------------------------------------------------- #
def nearest_target(entry_price: float, direction: str, levels: list[Level],
                   min_grade: str | None = None) -> Level | None:
    """Nearest resting liquidity level beyond entry in the trade direction.

    For a BUY (``direction="buy"``) the target is the nearest buy-side level
    above the entry; for a SELL it is the nearest sell-side level below.
    ``min_grade`` optionally filters out levels weaker than that grade; ties on
    distance are broken in favour of the stronger grade.
    """
    if min_grade:
        levels = [l for l in levels if grade_at_least(l.grade, min_grade)]
    if direction == "buy":
        candidates = [l for l in levels if l.buy_side and l.price > entry_price]
        if not candidates:
            return None
        return min(candidates, key=lambda l: (l.price - entry_price, -grade_rank(l.grade)))
    candidates = [l for l in levels if l.sell_side and l.price < entry_price]
    if not candidates:
        return None
    return max(candidates, key=lambda l: (l.price - entry_price, -grade_rank(l.grade)))


def select_tp_target(entry_price: float, direction: str, levels: list[Level],
                     *, offset: float = 0.0, min_grade: str | None = None
                     ) -> tuple[Level, float] | None:
    """The take-profit target and the price to place it at.

    The target is the nearest *valid* liquidity pull in the trade direction
    (see :func:`nearest_target`). ``offset`` pulls the order slightly *before*
    the level — a resting level is where the liquidity sits, so the fill is
    sought on the approach, not on the touch. Returns ``(level, tp_price)``, or
    ``None`` when nothing valid exists or the offset would push the target to or
    behind the entry.
    """
    target = nearest_target(entry_price, direction, levels, min_grade=min_grade)
    if target is None:
        return None
    tp = target.price - offset if direction == "buy" else target.price + offset
    if direction == "buy" and tp <= entry_price:
        return None
    if direction == "sell" and tp >= entry_price:
        return None
    return target, tp
