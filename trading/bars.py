"""Candle model + deterministic OHLC aggregation (no lookahead).

A :class:`Candle` stores both the real-UTC instant (``t_utc``) and the project
NY-clock instant (``t_ny``, UTC-4). Higher timeframes are aggregated from closed
M1 candles only; a partially formed top bucket is never returned, which prevents
lookahead bias.

All aggregation is keyed on the UTC minute so results are independent of the
broker's display timezone and remain continuous across DST shifts of the
server clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import time_utils as tu

# Timeframe -> period in minutes (single source of truth).
TIMEFRAME_MINUTES: dict[str, int] = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}
TIMEFRAMES: tuple[str, ...] = ("M1", "M5", "M15", "H1")

# Common MT5 timeframe numeric codes (used by the MT5 client).
MT5_TIMEFRAME_CODES: dict[str, int] = {"M1": 1, "M5": 5, "M15": 15, "H1": 16385}


def epoch_minute(dt: datetime) -> int:
    """Whole minutes since epoch for a naive UTC datetime (bucket key)."""
    return int((dt - datetime(1970, 1, 1)).total_seconds()) // 60


@dataclass(slots=True)
class Candle:
    t_utc: datetime          # candle open time, real UTC (naive)
    t_ny: datetime           # candle open time on the NY clock (UTC-4)
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def time(self) -> datetime:
        """Alias — the NY time is the strategy's authoritative candle time."""
        return self.t_ny

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    def as_dict(self) -> dict:
        return {
            "t_utc": self.t_utc.isoformat(),
            "t_ny": self.t_ny.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def make_candle(t_utc: datetime, open_: float, high: float, low: float,
                close: float, volume: int = 0) -> Candle:
    """Build a candle, deriving the NY time centrally."""
    return Candle(t_utc=t_utc, t_ny=tu.utc_to_ny(t_utc), open=open_, high=high,
                  low=low, close=close, volume=volume)


def aggregate_candles(m1: list[Candle], period_minutes: int,
                      include_last_partial: bool = False) -> list[Candle]:
    """Aggregate closed M1 candles into ``period_minutes`` candles.

    Candles must be sorted ascending by ``t_utc``. A trailing (final) bucket is
    returned only when it is *complete* — that is, it holds exactly
    ``period_minutes`` M1 candles (each open minute present). When
    ``include_last_partial`` is True the trailing bucket is always appended,
    which callers should never do when stepping through live/backtest data.
    """
    if period_minutes < 1:
        raise ValueError("period_minutes must be >= 1")
    if period_minutes == 1:
        result = [c for c in m1]
        return result[:-1] if m1 and not include_last_partial else result

    buckets: list[Candle] = []
    bucket_start_key: int | None = None
    bucket_count = 0
    acc_open = acc_high = acc_low = acc_close = 0.0
    acc_vol = 0
    first_utc: datetime | None = None

    for c in m1:
        key = epoch_minute(c.t_utc) // period_minutes * period_minutes
        if bucket_start_key is None:
            bucket_start_key = key
            first_utc = c.t_utc
            acc_open = c.open
            acc_high = c.high
            acc_low = c.low
            acc_close = c.close
            acc_vol = c.volume
            bucket_count = 1
        elif key == bucket_start_key:
            acc_high = max(acc_high, c.high)
            acc_low = min(acc_low, c.low)
            acc_close = c.close
            acc_vol += c.volume
            bucket_count += 1
        else:
            # Close previous bucket at the instant the new bucket begins.
            buckets.append(Candle(
                t_utc=first_utc, t_ny=tu.utc_to_ny(first_utc),
                open=acc_open, high=acc_high, low=acc_low, close=acc_close,
                volume=acc_vol,
            ))
            bucket_start_key = key
            first_utc = c.t_utc
            acc_open = c.open
            acc_high = c.high
            acc_low = c.low
            acc_close = c.close
            acc_vol = c.volume
            bucket_count = 1

    # Append the trailing bucket when it is complete (or when explicitly asked).
    trailing_complete = bucket_start_key is not None and bucket_count >= period_minutes
    if bucket_start_key is not None and (include_last_partial or trailing_complete):
        buckets.append(Candle(
            t_utc=first_utc, t_ny=tu.utc_to_ny(first_utc),
            open=acc_open, high=acc_high, low=acc_low, close=acc_close,
            volume=acc_vol,
        ))
    return buckets


class BarSet:
    """A live, incrementally-fed store of closed M1 candles.

    Higher timeframes are lazily rebuilt from the closed M1 series whenever new
    M1 candles are appended (invalidation-based caching). Feeding it only closed
    candles means every returned higher-timeframe candle is fully closed.
    """

    def __init__(self, server_offset_hours: float | None = None):
        self._m1: list[Candle] = []
        self._offset = server_offset_hours
        self._cache: dict[int, list[Candle]] = {}

    def ingest_server_rows(self, rows) -> None:
        """Append raw MT5 rows (tuples: time,o,h,l,c,..) converting server->NY.

        ``rows`` is expected newest-first or oldest-first? We sort defensively.
        """
        converted = []
        for row in rows:
            server_naive = datetime.fromtimestamp(int(row[0]))
            t_utc = tu.broker_to_utc(server_naive, self._offset)
            c = Candle(t_utc=t_utc, t_ny=tu.utc_to_ny(t_utc),
                       open=float(row[1]), high=float(row[2]),
                       low=float(row[3]), close=float(row[4]),
                       volume=int(row[5]) if len(row) > 5 else 0)
            converted.append(c)
        self.add_many(converted)

    def add(self, candle: Candle) -> None:
        if self._m1 and candle.t_utc <= self._m1[-1].t_utc:
            # Idempotent re-feed of an already-seen closed candle.
            if candle.t_utc == self._m1[-1].t_utc:
                return
            raise ValueError("M1 candle out of order (not strictly increasing)")
        self._m1.append(candle)
        self._cache.clear()

    def add_many(self, candles: list[Candle]) -> None:
        candles = sorted(candles, key=lambda c: c.t_utc)
        for c in candles:
            self.add(c)

    @property
    def m1(self) -> list[Candle]:
        return list(self._m1)

    def last_closed(self) -> Candle | None:
        return self._m1[-1] if self._m1 else None

    def timeframe(self, key: str) -> list[Candle]:
        minutes = TIMEFRAME_MINUTES[key]
        if minutes not in self._cache:
            self._cache[minutes] = aggregate_candles(self._m1, minutes,
                                                     include_last_partial=False)
        return list(self._cache[minutes])

    def m5(self) -> list[Candle]:
        return self.timeframe("M5")

    def m15(self) -> list[Candle]:
        return self.timeframe("M15")

    def h1(self) -> list[Candle]:
        return self.timeframe("H1")

    def __len__(self) -> int:
        return len(self._m1)
