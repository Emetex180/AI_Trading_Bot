"""Minimal deterministic indicators shared by the strategy modules.

Only what the strategy actually uses is implemented (no unrelated indicators).
All functions operate on *closed* candles only and never peek ahead.
"""
from __future__ import annotations


def atr(candles: list, period: int = 14) -> float:
    """Average True Range over the last ``period`` closed candles.

    True range uses the previous close as reference (Wilder-style without
    smoothing). Falls back to the average candle range when history is short.
    """
    if not candles:
        return 0.0
    n = len(candles)
    window = candles[-period:] if period > 0 else candles
    if len(window) < 2:
        return sum(c.range for c in candles) / max(1, len(candles))

    trs = []
    for i in range(1, len(window)):
        prev_close = window[i - 1].close
        c = window[i]
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


def swing_tolerance(atr_value: float, multiple: float = 0.2) -> float:
    """Tolerance used when clustering equal highs/lows from an ATR value."""
    return atr_value * multiple
