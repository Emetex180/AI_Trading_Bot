"""MetaTrader 5 client wrapper.

This is the *only* module that talks to the ``MetaTrader5`` package. It keeps the
interface small so the rest of the codebase can be tested without a live
terminal. Importing this module does **not** require a running MetaTrader 5
terminal; only :meth:`MT5Client.connect` does.

The existing working MT5 python setup (already installed on this machine) is
used as-is — nothing about the package is patched or replaced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import Settings

try:  # pragma: no cover - importable only where the package is installed
    import MetaTrader5 as _mt5  # type: ignore
except Exception:  # pragma: no cover - defensive
    _mt5 = None


class MT5Error(RuntimeError):
    """Raised when an MT5 operation cannot be performed."""


@dataclass
class AccountSummary:
    login: int | None
    server: str | None
    name: str | None
    currency: str | None
    balance: float
    equity: float
    leverage: int | None
    margin_free: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class MT5Client:
    """Thin, safe wrapper around the MetaTrader5 python package."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._connected = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return _mt5 is not None

    def connect(self) -> bool:
        if not self.available:
            raise MT5Error("MetaTrader5 python package is not installed/importable")
        if self._connected and self._is_connected():
            return True

        kwargs: dict[str, Any] = {"path": self.settings.mt5_terminal_path} if self.settings.mt5_terminal_path else {}
        if self.settings.mt5_login:
            kwargs["login"] = int(self.settings.mt5_login)
            kwargs["password"] = self.settings.mt5_password or ""
            if self.settings.mt5_server:
                kwargs["server"] = self.settings.mt5_server

        ok = _mt5.initialize(**kwargs)
        if not ok:
            last = _mt5.last_error()
            raise MT5Error(f"MT5 initialize failed: {last}")
        self._connected = True
        return True

    def _is_connected(self) -> bool:
        info = _mt5.terminal_info()
        return bool(info is not None and info.connected)

    def is_connected(self) -> bool:
        try:
            return self.available and self._is_connected()
        except Exception:
            return False

    def disconnect(self) -> None:
        if _mt5 is not None:
            _mt5.shutdown()
        self._connected = False

    # ------------------------------------------------------------------ #
    # Account / symbol info
    # ------------------------------------------------------------------ #
    def account_info(self) -> AccountSummary | None:
        if not self.is_connected():
            return None
        acc = _mt5.account_info()
        if acc is None:
            return None
        return AccountSummary(
            login=acc.login,
            server=acc.server,
            name=acc.name,
            currency=acc.currency,
            balance=acc.balance,
            equity=acc.equity,
            leverage=acc.leverage,
            margin_free=acc.margin_free,
        )

    def symbol_info(self, symbol: str) -> dict[str, Any] | None:
        if not self.is_connected():
            return None
        info = _mt5.symbol_info(symbol)
        if info is None:
            return None
        return {
            "name": info.name,
            "digits": info.digits,
            "point": info.point,
            "trade_mode": info.trade_mode,
            "visible": bool(info.visible),
        }

    # ------------------------------------------------------------------ #
    # Market data (server-clock times; conversion happens in market_data)
    # ------------------------------------------------------------------ #
    def _timeframe_code(self, timeframe: str) -> int:
        from .bars import MT5_TIMEFRAME_CODES

        try:
            return MT5_TIMEFRAME_CODES[timeframe]
        except KeyError as exc:
            raise MT5Error(f"Unsupported timeframe {timeframe!r}") from exc

    def _copy_rates_from_pos_raw(self, symbol: str, timeframe: str, pos: int,
                                 count: int) -> list[tuple]:
        """``copy_rates_from_pos`` that returns ``[]`` instead of raising.

        Probing for the edges of history asks for positions that legitimately do
        not exist, so "no rows here" must be distinguishable from a failure.
        """
        if not self.is_connected():
            raise MT5Error("Not connected to MT5")
        rates = _mt5.copy_rates_from_pos(symbol, self._timeframe_code(timeframe), pos, count)
        if rates is None:
            return []
        return [tuple(r) for r in rates]

    def copy_rates_from_pos(self, symbol: str, timeframe: str, pos: int, count: int) -> list[tuple]:
        """Return raw MT5 rate rows (chronological ascending) for ``symbol``.

        Rows are tuples of ``(time, open, high, low, close, tick_volume, spread, real_volume)``
        where ``time`` is the server-clock epoch. May include the currently
        forming bar at the newest position — callers decide what is 'closed'.
        """
        rates = self._copy_rates_from_pos_raw(symbol, timeframe, pos, count)
        if not rates:
            raise MT5Error(f"copy_rates_from_pos failed for {symbol} {timeframe}: {_mt5.last_error()}")
        return rates

    def copy_rates_range(self, symbol: str, timeframe: str, date_from, date_to) -> list[tuple]:
        """Return raw MT5 rate rows between two **server-clock** datetimes.

        ``MetaTrader5.copy_rates_range`` filters bars against the *broker server*
        clock, not UTC — callers must convert before calling (see
        :meth:`trading.market_data.MarketData.fetch_m1_range`).

        An empty list is a legitimate answer ("the broker holds no bars in this
        window") and is returned as-is; only a hard failure raises.
        """
        if not self.is_connected():
            raise MT5Error("Not connected to MT5")
        rates = _mt5.copy_rates_range(symbol, self._timeframe_code(timeframe),
                                      date_from, date_to)
        if rates is None:
            raise MT5Error(
                f"copy_rates_range failed for {symbol} {timeframe} "
                f"{date_from}..{date_to}: {_mt5.last_error()}")
        return [tuple(r) for r in rates]

    def history_bar_count(self, symbol: str, timeframe: str,
                          ceiling: int = 5_000_000) -> int:
        """How many bars of ``timeframe`` the broker holds for ``symbol``.

        MT5 offers no "how much history do you have" call, but
        ``copy_rates_from_pos`` indexes backwards from the newest bar, so the
        count is the largest ``pos`` that still returns a row. Found by
        exponential probing followed by a bisection — about ``2*log2(N)``
        one-bar requests, which is cheap next to paging the history itself.
        """
        if not self._copy_rates_from_pos_raw(symbol, timeframe, 0, 1):
            return 0

        # Index 0 is known to exist, so the count is at least 1. Bisect between
        # the largest index known to exist (`lo`) and one known not to (`hi`).
        lo, hi = 0, 1
        while hi < ceiling and self._copy_rates_from_pos_raw(symbol, timeframe, hi, 1):
            lo, hi = hi, min(hi * 2, ceiling)

        # Invariant: `lo` exists, `hi` does not (or `hi` hit the ceiling).
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self._copy_rates_from_pos_raw(symbol, timeframe, mid, 1):
                lo = mid
            else:
                hi = mid
        return lo + 1


def row_time_to_server_naive(row_time_epoch: int) -> Any:
    """Convert an MT5 row timestamp to a naive server-clock datetime."""
    from datetime import datetime

    return datetime.fromtimestamp(int(row_time_epoch))
