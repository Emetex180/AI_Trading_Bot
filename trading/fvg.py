"""Three-candle Fair Value Gap (FVG) detection on M1.

A bullish FVG leaves a gap between the first candle's high and the third
candle's low when the middle candle displaces upward:

    bullish:  c[k+2].low  >  c[k].high      lower = c[k].high, upper = c[k+2].low

A bearish FVG is the mirror:

    bearish:  c[k+2].high <  c[k].low       upper = c[k].low,  lower = c[k+2].high

Only closed candles are used, and FVGs are only reported for fully-formed
patterns, so there is no lookahead.
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


def classify(candle, fvg: FVG) -> str:
    """Classify a closed candle relative to a formed FVG.

    Returns one of:
      * "invalidated"  – closed through the far side of the gap (bearish/bullish)
      * "retraced"     – traded into the gap but did not destroy it
      * "untouched"    – did not reach the gap
    """
    if fvg.direction == "bullish":
        if candle.close < fvg.lower:
            return "invalidated"
        if candle.low <= fvg.upper and candle.low >= fvg.lower:
            return "retraced"
        return "untouched"
    # bearish
    if candle.close > fvg.upper:
        return "invalidated"
    if candle.high >= fvg.lower and candle.high <= fvg.upper:
        return "retraced"
    return "untouched"
