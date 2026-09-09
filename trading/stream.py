"""Incremental multi-timeframe stream: closed M1 in, closed M5/M15/H1 out.

Each :meth:`add` accepts one *closed* M1 candle and finalizes any higher
timeframe bucket whose last constituent minute is now behind it. Buckets are
keyed on the UTC minute so results are timezone-independent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .bars import Candle, TIMEFRAME_MINUTES, epoch_minute
from . import time_utils as tu


def _bucket_start_utc(m1_candle: Candle, period: int) -> datetime:
    key = epoch_minute(m1_candle.t_utc) // period * period
    # UTC epoch-seconds -> naive-UTC datetime (matches the naive-UTC convention
    # used across the project; avoids datetime.utcfromtimestamp deprecation).
    return datetime(1970, 1, 1) + timedelta(minutes=key)


@dataclass
class StepUpdate:
    """Which closed candles were finalized by feeding one closed M1 candle."""

    m1: Candle
    m5: Candle | None = None
    m15: Candle | None = None
    h1: Candle | None = None

    def get(self, tf: str) -> Candle | None:
        return {"M1": self.m1, "M5": self.m5, "M15": self.m15, "H1": self.h1}.get(tf)


class _BucketBuilder:
    def __init__(self, period: int):
        self.period = period
        self.closed: list[Candle] = []
        self._open_utc: datetime | None = None
        self._o = self._h = self._l = self._c = 0.0
        self._vol = 0
        self._count = 0

    def on_m1(self, c: Candle) -> Candle | None:
        """Accumulate one M1; return the bucket it closes (or None)."""
        key = _bucket_start_utc(c, self.period)
        new_bucket = key != self._open_utc
        finalized: Candle | None = None
        if new_bucket and self._open_utc is not None:
            finalized = Candle(t_utc=self._open_utc, t_ny=tu.utc_to_ny(self._open_utc),
                               open=self._o, high=self._h, low=self._l,
                               close=self._c, volume=self._vol)
            self.closed.append(finalized)

        if new_bucket or self._open_utc is None:
            self._open_utc = key
            self._o = c.open
            self._h = c.high
            self._l = c.low
            self._c = c.close
            self._vol = c.volume
        else:
            self._h = max(self._h, c.high)
            self._l = min(self._l, c.low)
            self._c = c.close
            self._vol += c.volume
        return finalized


class BarsStream:
    """Closed M1 candles plus incrementally maintained M5/M15/H1 series."""

    def __init__(self, server_offset_hours: float | None = None):
        self.m1: list[Candle] = []
        self._offset = server_offset_hours
        self._builders = {tf: _BucketBuilder(TIMEFRAME_MINUTES[tf])
                          for tf in ("M5", "M15", "H1")}

    def add(self, candle: Candle) -> StepUpdate:
        """Append a *closed* M1 candle and finalize any closing buckets."""
        if self.m1 and candle.t_utc < self.m1[-1].t_utc:
            raise ValueError("M1 candle out of order")
        if self.m1 and candle.t_utc == self.m1[-1].t_utc:
            # Idempotent re-feed (broker may return the same closed bar twice).
            return StepUpdate(m1=self.m1[-1])
        self.m1.append(candle)
        return StepUpdate(
            m1=candle,
            m5=self._builders["M5"].on_m1(candle),
            m15=self._builders["M15"].on_m1(candle),
            h1=self._builders["H1"].on_m1(candle),
        )

    def add_many(self, candles: list[Candle]) -> None:
        for c in sorted(candles, key=lambda x: x.t_utc):
            self.add(c)

    def tf(self, key: str) -> list[Candle]:
        if key == "M1":
            return self.m1
        return self._builders[key].closed

    def m5(self) -> list[Candle]:
        return self.tf("M5")

    def m15(self) -> list[Candle]:
        return self.tf("M15")

    def h1(self) -> list[Candle]:
        return self.tf("H1")

    def last_m1(self) -> Candle | None:
        return self.m1[-1] if self.m1 else None
