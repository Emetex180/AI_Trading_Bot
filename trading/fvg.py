"""Three-candle Fair Value Gap (FVG) detection on M1.

A bullish FVG leaves a gap between the first candle's high and the third
candle's low when the middle candle displaces upward:

    bullish:  c[k+2].low  >  c[k].high      lower = c[k].high, upper = c[k+2].low

A bearish FVG is the mirror:

    bearish:  c[k+2].high <  c[k].low       upper = c[k].low,  lower = c[k+2].high

Only closed candles are used, and FVGs are only reported for fully-formed
patterns, so there is no lookahead.

Retracement vs. entry
---------------------
Two separate facts, deliberately kept as two functions:

* :func:`entered` — price traded *into* the gap (a wick is enough). This is the
  retracement event.
* :func:`closes_inside` — the candle *closed* within the gap. This is the entry
  trigger.

A retracement is not an entry. The gap being touched says the level was
respected; only a close back inside the zone ties the fill to that gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FVG:
    direction: str        # "bullish" | "bearish"
    upper: float
    lower: float
    formation_time_utc: datetime   # open time of the third candle of the pattern
    formation_time_ny: datetime

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def depth(self) -> float:
        return self.upper - self.lower

    @property
    def midpoint(self) -> float:
        return (self.lower + self.upper) / 2.0


def fvg_at_three(c0, c1, c2, direction: str | None = None) -> FVG | None:
    """Detect an FVG formed by three consecutive candles (c2 is the newest)."""
    if direction in (None, "bullish"):
        if c2.low > c0.high:
            return FVG(direction="bullish", upper=c2.low, lower=c0.high,
                       formation_time_utc=c2.t_utc, formation_time_ny=c2.t_ny)
    if direction in (None, "bearish"):
        if c2.high < c0.low:
            return FVG(direction="bearish", upper=c0.low, lower=c2.high,
                       formation_time_utc=c2.t_utc, formation_time_ny=c2.t_ny)
    return None


def fvg_on_tail(candles: list, direction: str | None = None,
                after_time_utc: datetime | None = None,
                before_time_utc: datetime | None = None) -> FVG | None:
    """Detect the most recent FVG using the last three *closed* candles.

    ``after_time_utc`` / ``before_time_utc`` optionally restrict the pattern's
    formation time so callers can require the FVG to form after a CISD
    confirmation and before the current candle.
    """
    if len(candles) < 3:
        return None
    c0, c1, c2 = candles[-3], candles[-2], candles[-1]
    if after_time_utc is not None and c2.t_utc < after_time_utc:
        return None
    if before_time_utc is not None and c2.t_utc > before_time_utc:
        return None
    return fvg_at_three(c0, c1, c2, direction=direction)


def entered(candle, fvg: FVG) -> bool:
    """Did this closed candle's range actually trade *into* the gap?

    True whenever the candle's high/low band overlaps ``[fvg.lower, fvg.upper]``,
    however deep the excursion went. This is the **retracement** fact on its own
    — it says price reached the gap, and nothing more. Deliberately independent
    of where the candle closed, so it can never be conflated with the entry
    trigger (see :func:`closes_inside`).
    """
    return candle.low <= fvg.upper and candle.high >= fvg.lower


def closes_inside(candle, fvg: FVG) -> bool:
    """Did this closed candle *close* within the gap?

    The entry-trigger half of the model. A wick into the gap is a retracement;
    only a close back inside the zone ties the fill to that gap. A close beyond
    the far boundary is exactly the "reaction close outside the FVG" that must
    not be reported as an FVG entry.
    """
    return fvg.contains(candle.close)


def classify(candle, fvg: FVG) -> str:
    """Classify a closed candle relative to a formed FVG.

    Returns one of:
      * "invalidated"  – closed through the far side of the gap (bearish/bullish)
      * "retraced"     – traded into the gap but did not destroy it
      * "untouched"    – did not reach the gap

    "retraced" is the overlap test in :func:`entered`, so a candle that pierces
    straight through the gap and closes back inside it still counts as a
    retracement rather than reading as "untouched" — the excursion did happen,
    and only a *close* beyond the far boundary destroys the gap.
    """
    if fvg.direction == "bullish":
        if candle.close < fvg.lower:
            return "invalidated"
        return "retraced" if entered(candle, fvg) else "untouched"
    # bearish
    if candle.close > fvg.upper:
        return "invalidated"
    return "retraced" if entered(candle, fvg) else "untouched"
