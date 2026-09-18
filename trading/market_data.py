"""Market data access: raw MT5 rates -> closed :class:`Candle` objects.

All timezone conversion from the broker clock to the project NY clock happens
here (through ``trading.bars`` / ``trading.time_utils``); nothing else may do it.

The broker offset is passed as ``None`` by default, which means "ask
:mod:`trading.time_utils` for the offset currently in force" — that module holds
the value discovered from the live terminal. Passing an explicit number pins it
(tests, replays of a known broker).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .bars import BarSet, Candle
from .mt5_client import MT5Client, row_time_to_server_naive
from . import sessions as sess
from . import time_utils as tu

__all__ = ["MarketData", "row_to_candle", "describe_candle_time"]


def row_to_candle(row: tuple, server_utc_offset_hours: float | None) -> Candle:
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


def describe_candle_time(row: tuple, candle: Candle,
                         server_utc_offset_hours: float | None) -> str:
    """The MT5 -> UTC -> NY -> session trace for one raw row.

    The session named here is the one ``trading.strategy`` will actually gate on,
    so this line is evidence about the decision rather than a parallel opinion:
    every value comes from the same conversion the pipeline used.
    """
    broker_naive = row_time_to_server_naive(row[0])
    minute = tu.minute_of_day(candle.t_ny)
    window = sess.primary_session(minute)
    label = window.label if window else "Outside session"
    return tu.format_time_chain(broker_naive, candle.t_utc, candle.t_ny,
                                session=label,
                                offset_hours=server_utc_offset_hours)


class MarketData:
    """Fetches closed M1 history and streams newly closed M1 candles."""

    def __init__(self, client: MT5Client,
                 server_utc_offset_hours: float | None = None,
                 on_debug=None):
        self.client = client
        #: ``None`` -> resolve through :mod:`trading.time_utils` on every call,
        #: which is how the discovered broker offset reaches the conversions.
        self.offset = server_utc_offset_hours
        self._on_debug = on_debug
        #: Throttle for the time trace: one per (symbol, NY minute).
        self._last_debug_key: tuple[str, str] | None = None

    def set_debug_sink(self, sink) -> None:
        """Attach the console sink the time trace is written to.

        A setter rather than a constructor argument so the runner can keep
        building this through its two-argument ``market_factory`` (which tests
        replace with fakes) and hand over its own emit function afterwards.
        """
        self._on_debug = sink

    def _debug_candle_time(self, symbol: str, row: tuple, candle: Candle) -> None:
        """Emit the conversion trace for a candle, at most once per NY minute."""
        if self._on_debug is None or not tu.time_debug_enabled():
            return
        key = (symbol, candle.t_ny.strftime("%Y-%m-%d %H:%M"))
        if key == self._last_debug_key:
            return
        self._last_debug_key = key
        try:
            self._on_debug(f"[scan] {symbol}\n"
                           + describe_candle_time(row, candle, self.offset))
        except Exception:  # a broken console must never stop a session
            pass

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
        """True when the terminal offers ``symbol`` and it is now fetchable.

        MT5 returns no history for a symbol that is not selected in Market Watch,
        and brokers do not pre-select every instrument they offer — so this
        selects the symbol first. Without that, any newly added asset (an index,
        a cross, crypto) would appear "missing" and be skipped.
        """
        ensure = getattr(self.client, "ensure_symbol", None)
        if callable(ensure):
            return bool(ensure(symbol))
        info = self.client.symbol_info(symbol)
        return bool(info) and bool(info.get("visible", False))

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
        candles = [row_to_candle(r, self.offset) for r in closed_rows]
        if closed_rows and candles:
            self._debug_candle_time(symbol, closed_rows[-1], candles[-1])
        return candles
