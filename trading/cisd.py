"""CISD — Confirmation/continuation detector after a liquidity purge.

This module is deliberately **standalone and testable** with synthetic candles.

Timeframe selection
-------------------
* purge at/after the NY hour in ``cisd_threshold_hour_ny`` (default 09:00) → **M5**
* purge before that hour                                          → **M15**

Documented assumption (make it configurable; nothing is silently invented)
---------------------------------------------------------------------------
"Confirmed CISD" is not given an exact canonical definition in the spec, so the
module ships with a deterministic, configurable definition:

    A *bullish CISD confirmation* is a closed candle on the chosen CISD
    timeframe, opening at/after the purge premise candle, that:
      1. closes bullish (close > open), and
      2. closes back **above** the swept sell-side liquidity level (reclaim), and
      3. optionally shows a minimum bullish displacement (body size vs. range).

    A *bearish CISD confirmation* is the exact mirror: a closed candle opening
    at/after the premise that closes bearish and closes back **below** the swept
    buy-side level.

The behaviour is selected with ``mode`` (only ``"displacement_reclaim"`` is
implemented; adding another mode keeps the interface stable).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Threshold used if the caller does not provide one.
DEFAULT_THRESHOLD_HOUR_NY = 9


@dataclass(frozen=True)
class CISDParams:
    mode: str = "displacement_reclaim"
    # Minimum body size (close-open) as a fraction of the candle range required
    # for a displacement confirmation. 0.0 disables the displacement gate.
    displacement_frac: float = 0.0
    # How many CISD-timeframe candles after the purge are searched.
    max_candles: int = 8


def choose_timeframe(purge_time_ny: datetime, threshold_hour_ny: int = DEFAULT_THRESHOLD_HOUR_NY) -> str:
    """M5 when purge occurred at/after the threshold hour, else M15."""
    if purge_time_ny.hour >= threshold_hour_ny:
        return "M5"
    return "M15"


def confirm(candles: list, level_price: float, direction: str,
            after_time_utc: datetime | None = None,
            params: CISDParams | None = None) -> object:
    """Return the first confirming candle (or None).

    ``candles`` is the *closed* series on the CISD timeframe (oldest first).
    ``level_price`` is the swept liquidity level. ``direction`` is the intended
    trade direction: ``"buy"`` confirms a sell-side sweep was reclaimed;
    ``"sell"`` confirms a buy-side sweep was reclaimed.
    """
    params = params or CISDParams()
    if params.mode != "displacement_reclaim":
        raise ValueError(f"Unsupported CISD mode: {params.mode}")
    if direction not in {"buy", "sell"}:
        raise ValueError(f"direction must be 'buy' or 'sell', got {direction!r}")

    window = list(candles)
    if after_time_utc is not None:
        window = [c for c in window if c.t_utc >= after_time_utc]
    if params.max_candles > 0:
        window = window[-params.max_candles:]

    for c in window:
        if direction == "buy":
            if c.is_bullish and c.low < level_price and c.close > level_price:
                if _displacement_ok(c, params.displacement_frac):
                    return c
        else:  # sell
            if c.is_bearish and c.high > level_price and c.close < level_price:
                if _displacement_ok(c, params.displacement_frac):
                    return c
    return None


def _displacement_ok(candle, frac: float) -> bool:
    if frac <= 0.0:
        return True
    rng = candle.high - candle.low
    if rng <= 0:
        return candle.close != candle.open
    return abs(candle.close - candle.open) / rng >= frac
