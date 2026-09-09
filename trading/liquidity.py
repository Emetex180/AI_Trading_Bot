"""Deterministic liquidity detection.

Operates on *closed* candles only and derives:

* previous-day high/low (by NY calendar day),
* session extremes,
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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .sessions import Window


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

    kind: str                      # PDH|PDL|SESSION_HIGH|SESSION_LOW|SWING_HIGH|SWING_LOW|EQ_HIGH|EQ_LOW
    price: float
    time_ny: datetime

    @property
    def buy_side(self) -> bool:
        """True when resting liquidity sits *above* price (buy-side)."""
        return self.kind in {"PDH", "SESSION_HIGH", "SWING_HIGH", "EQ_HIGH"}

    @property
    def sell_side(self) -> bool:
        return not self.buy_side


@dataclass(frozen=True)
class SweepEvent:
    direction: str                 # "bullish" (sell-side swept) | "bearish" (buy-side swept)
    level: Level
    distance: float                # how far price traded beyond the level
    candle_index: int
    time_ny: datetime


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
    from datetime import timedelta

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


# --------------------------------------------------------------------------- #
# Level snapshot
# --------------------------------------------------------------------------- #
def build_level_snapshot(candles: list, as_of_date: str | None = None,
                         lookback_swings: int = 3,
                         swing_tolerance: float | None = None,
                         atr: float | None = None) -> list[Level]:
    """Assemble resting liquidity levels from the supplied (history) candles.

    PDH/PDL are computed relative to ``as_of_date`` (the NY date of the candle
    being tested for a sweep) using data strictly *before* that date. Pass
    ``as_of_date=None`` to use the last candle's own date.

    IMPORTANT: callers must pass a history slice that ends *before* the candle
    being tested — otherwise the sweep candle's own wick would appear as a
    swing / equal level (lookahead).
    """
    if not candles:
        return []
    ref_date = as_of_date or candles[-1].t_ny.strftime("%Y-%m-%d")
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
# Documented assumption (configurable, nothing silently invented): the engine
# prefers PDH/PDL, then session extremes, then equal highs/lows, and only falls
# back to a single swing when none of those were swept. Distance breaks ties.
PURGE_SIGNIFICANCE: dict[str, int] = {
    "PDH": 0, "PDL": 0,
    "SESSION_HIGH": 1, "SESSION_LOW": 1,
    "EQ_HIGH": 2, "EQ_LOW": 2,
    "SWING_HIGH": 3, "SWING_LOW": 3,
}


def _purge_rank(level: Level) -> int:
    return PURGE_SIGNIFICANCE.get(level.kind, 99)


def strongest_purge(events: list[SweepEvent], direction: str) -> SweepEvent | None:
    """The purge of a given direction that matters most to the trade.

    Selects the swept level of highest significance (PDH/PDL first, per
    :data:`PURGE_SIGNIFICANCE`); among equally significant levels, the one with
    the deepest overshoot wins.
    """
    matches = [e for e in events if e.direction == direction]
    if not matches:
        return None
    return min(matches, key=lambda e: (_purge_rank(e.level), -e.distance))


def nearest_target(entry_price: float, direction: str, levels: list[Level]) -> Level | None:
    """Nearest resting liquidity level beyond entry in the trade direction.

    For a BUY (``direction="buy"``) the target is the nearest buy-side level
    above the entry; for a SELL it is the nearest sell-side level below.
    """
    if direction == "buy":
        candidates = [l for l in levels if l.buy_side and l.price > entry_price]
        if not candidates:
            return None
        return min(candidates, key=lambda l: l.price - entry_price)
    candidates = [l for l in levels if l.sell_side and l.price < entry_price]
    if not candidates:
        return None
    return max(candidates, key=lambda l: entry_price - l.price)
