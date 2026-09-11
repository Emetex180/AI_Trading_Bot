"""Market data access: raw MT5 rates -> closed :class:`Candle` objects.

All timezone conversion from the broker clock to the project NY clock happens
here (through ``trading.bars`` / ``trading.time_utils``); nothing else may do it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .bars import BarSet, Candle
from .mt5_client import MT5Client, row_time_to_server_naive
from . import time_utils as tu


def row_to_candle(row: tuple, server_utc_offset_hours: float) -> Candle:
    """Convert one raw MT5 row into a Candle (NY time derived centrally)."""
    server_naive = row_time_to_server_naive(row[0])
    t_utc = tu.broker_to_utc(server_naive, server_utc_offset_hours)
    return Candle(
        t_utc=t_utc,
        t_ny=tu.utc_to_ny(t_utc),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=int(row[5]) if len(row) > 5 else 0,
    )


class MarketData:
    """Fetches closed M1 history and streams newly closed M1 candles."""

    def __init__(self, client: MT5Client, server_utc_offset_hours: float):
        self.client = client
        self.offset = server_utc_offset_hours

    # ------------------------------------------------------------------ #
    def fetch_m1_closed(self, symbol: str, count: int, drop_forming: bool = True) -> list[Candle]:
        """Return up to ``count`` *closed* M1 candles (ascending).

        One extra bar is requested and the newest row dropped when it is the
        still-forming bar, so only finished candles are returned.
        """
        extra = 1 if drop_forming else 0
        rows = self.client.copy_rates_from_pos(symbol, "M1", 0, count + extra)
        if drop_forming:
            rows = rows[:-1]  # newest row may be the forming bar
        return [row_to_candle(r, self.offset) for r in rows]

    def fetch_m1_range(self, symbol: str, start_utc: datetime, end_utc: datetime,
                       drop_forming: bool = True) -> list[Candle]:
        """Return *closed* M1 candles between two instants (UTC, ascending).

        The caller works in UTC, but ``MetaTrader5.copy_rates_range`` filters on
        the **broker server** clock, so the range is converted before the call.
        Candles come back through :func:`row_to_candle`, which converts the other
        way — so the returned ``t_utc`` is the real UTC instant either way.

        Unlike the "last N bars" fetch there is nothing to trim by default: a
        range ending in the past has no forming bar at all. The newest row is
        dropped only when it is genuinely still forming, which keeps a range
        ending *now* honest without silently shortening historical ones.
        """
        rows = self.client.copy_rates_range(
            symbol, "M1",
            tu.utc_to_broker(start_utc, self.offset),
            tu.utc_to_broker(end_utc, self.offset),
        )
        if drop_forming and rows:
            last_bar_open = row_time_to_server_naive(rows[-1][0])
            now_broker = tu.utc_to_broker(tu.now_utc(), self.offset)
            if last_bar_open + timedelta(minutes=1) > now_broker:
                rows = rows[:-1]
        return [row_to_candle(r, self.offset) for r in rows]

    def probe_m1_bounds(self, symbol: str) -> dict:
        """Describe the M1 history the broker actually holds for ``symbol``.

        This is what lets the dashboard offer real dates instead of asking for a
        blind count of bars. Returns ``n_bars == 0`` when the broker has nothing,
        which is also the honest answer for a symbol that is not visible.
        """
        bounds = {"symbol": symbol, "n_bars": 0,
                  "oldest_utc": None, "newest_utc": None}
        n_bars = self.client.history_bar_count(symbol, "M1")
        if n_bars <= 0:
            return bounds

        newest = self.client.copy_rates_from_pos(symbol, "M1", 0, 1)
        oldest = self.client.copy_rates_from_pos(symbol, "M1", n_bars - 1, 1)
        if oldest:
            bounds["oldest_utc"] = row_to_candle(oldest[0], self.offset).t_utc
        if newest:
            bounds["newest_utc"] = row_to_candle(newest[0], self.offset).t_utc
        bounds["n_bars"] = n_bars
        return bounds

    def symbol_exists(self, symbol: str) -> bool:
        info = self.client.symbol_info(symbol)
        return bool(info and info.get("visible", False) or info)

    def build_warmup_barset(self, symbol: str, count: int) -> BarSet:
        """Fetch history into a warm :class:`BarSet` for live/backtest use."""
        bars = self.fetch_m1_closed(symbol, count, drop_forming=True)
        bs = BarSet(server_offset_hours=self.offset)
        bs.add_many(bars)
        return bs

    # ------------------------------------------------------------------ #
    # Incremental new-closed-candle detection
    # ------------------------------------------------------------------ #
    def poll_closed_candles(self, symbol: str, lookback: int = 3) -> list[Candle]:
        """Return the latest *closed* M1 candles (ascending, newest last).

        The newest raw bar returned by the terminal is assumed to be the
        currently forming bar and is ignored; bars behind it are closed.
        """
        rows = self.client.copy_rates_from_pos(symbol, "M1", 0, lookback + 1)
        closed_rows = rows[:-1]
        return [row_to_candle(r, self.offset) for r in closed_rows]
