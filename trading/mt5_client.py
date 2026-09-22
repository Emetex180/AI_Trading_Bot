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
from datetime import timedelta
from typing import Any

from config import Settings

from . import time_utils as tu

try:  # pragma: no cover - importable only where the package is installed
    import MetaTrader5 as _mt5  # type: ignore
except Exception:  # pragma: no cover - defensive
    _mt5 = None


class MT5Error(RuntimeError):
    """Raised when an MT5 operation cannot be performed."""


#: Brokers put their server clock on quarter-hour boundaries, so a discovered
#: offset is snapped to this grid — that is what makes agreement between two
#: independent symbols meaningful.
OFFSET_QUANTUM_SECONDS = 900

#: How far a broker clock may plausibly sit from UTC (UTC-12 .. UTC+14).
MAX_SANE_OFFSET_HOURS = 14.0

#: A clock reading this far off the snap grid is stale rather than offset.
OFFSET_TOLERANCE_SECONDS = 120

#: The newest M1 bar must have opened within this long for the market to count
#: as live — the check that stops a weekend-close clock reading from being
#: mistaken for a broker offset.
MAX_BAR_AGE_SECONDS = 600


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

    def terminal_trade_allowed(self) -> bool | None:
        """Whether the terminal's own Algo Trading button is on.

        Deliberately *not* ``AccountInfo.trade_allowed``: that one reports the
        account's permission to trade, which stays ``True`` even when the
        toolbar toggle is off. This is the toolbar toggle, and while it is off
        every ``order_send`` is rejected no matter what ``AUTO_TRADING`` says —
        which is exactly the state that looks like "auto-trading is on but
        nothing happens".

        ``None`` means "could not ask" (no terminal), which the dashboard must
        render as unknown rather than as a reassuring ``False`` or ``True``.
        """
        if not self.is_connected():
            return None
        info = _mt5.terminal_info()
        if info is None:
            return None
        return bool(info.trade_allowed)

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

    @staticmethod
    def _info_dict(info: Any) -> dict[str, Any]:
        """Normalise one MT5 ``SymbolInfo`` object into the dict the bot passes around.

        The contract fields (``trade_contract_size``, ``volume_*``, ``tick_*``)
        are what make position sizing correct per asset class — see
        :mod:`trading.symbol_spec`. Shared by :meth:`symbol_info` and
        :meth:`symbol_catalog` so the two can never report different contract
        numbers for the same instrument; ``SymbolSpec.from_mt5_info`` reads either.
        """
        return {
            "name": info.name,
            "digits": info.digits,
            "point": info.point,
            "trade_mode": info.trade_mode,
            "visible": bool(info.visible),
            # Contract specification (per-asset-class sizing).
            "trade_contract_size": getattr(info, "trade_contract_size", 0.0),
            "volume_min": getattr(info, "volume_min", 0.0),
            "volume_step": getattr(info, "volume_step", 0.0),
            "volume_max": getattr(info, "volume_max", 0.0),
            "tick_size": getattr(info, "trade_tick_size", 0.0),
            "tick_value": getattr(info, "trade_tick_value", 0.0),
            "filling_mode": getattr(info, "filling_mode", None),
            "currency_profit": getattr(info, "currency_profit", ""),
        }

    def symbol_info(self, symbol: str) -> dict[str, Any] | None:
        """Everything the rest of the bot needs to know about one symbol."""
        if not self.is_connected():
            return None
        info = _mt5.symbol_info(symbol)
        if info is None:
            return None
        return self._info_dict(info)

    def symbol_names(self) -> list[str]:
        """Every symbol name the terminal offers (used for broker-symbol lookup).

        Returns ``[]`` rather than raising so an unavailable symbol list degrades
        to "could not resolve" instead of taking down a scanning session.
        """
        if not self.is_connected():
            return []
        symbols = _mt5.symbols_get()
        if symbols is None:
            return []
        return [s.name for s in symbols]

    def symbol_catalog(self) -> list[dict[str, Any]]:
        """Every symbol the terminal offers, each with its contract fields.

        ``symbols_get()`` returns *populated* info objects, so the contract
        numbers arrive in the same single call that lists the names. Reading
        :meth:`symbol_info` once per symbol would be thousands of terminal round
        trips over a broker's full catalogue, which is why the dashboard browser
        reads this instead.

        Returns ``[]`` rather than raising when the terminal is unavailable —
        the same degrade-don't-fail contract as :meth:`symbol_names`, so the
        dashboard shows an empty list instead of an error page.
        """
        if not self.is_connected():
            return []
        symbols = _mt5.symbols_get()
        if symbols is None:
            return []
        return [self._info_dict(s) for s in symbols]

    def ensure_symbol(self, symbol: str) -> bool:
        """Make ``symbol`` visible in Market Watch; return whether it now is.

        MT5 returns no history for a symbol that is not selected, so this is
        required before the first fetch of any newly added asset. Selecting is
        idempotent and only affects the terminal's symbol list — never orders.
        """
        if not self.is_connected():
            return False
        info = _mt5.symbol_info(symbol)
        if info is None:
            seen = _mt5.symbols_get(symbol)
            if not seen:
                return False
            info = seen[0]
        if getattr(info, "visible", False):
            return True
        return bool(_mt5.symbol_select(symbol, True))

    # ------------------------------------------------------------------ #
    # Broker clock discovery
    # ------------------------------------------------------------------ #
    def _tick_time_epoch(self, symbol: str) -> int | None:
        """The broker-clock epoch of ``symbol``'s last tick, or ``None``."""
        tick = _mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        stamp = int(getattr(tick, "time", 0) or 0)
        return stamp or None

    def _newest_m1_open_epoch(self, symbol: str) -> int | None:
        """The broker-clock epoch of the newest M1 bar's open, or ``None``."""
        rows = self._copy_rates_from_pos_raw(symbol, "M1", 0, 1)
        return int(rows[0][0]) if rows else None

    def symbol_tick(self, symbol: str) -> dict[str, Any] | None:
        """The last quote for ``symbol``, or ``None`` if there is not one.

        Read-only and additive: this is the same ``symbol_info_tick`` call
        :meth:`_tick_time_epoch` already makes for the broker-clock probe,
        surfaced with the bid/ask so the dashboard can show a real quote rather
        than only the last price the strategy acted on.

        ``spread`` is the ask-minus-bid in *price* units, which is what a
        reader compares against the digits they can see; ``spread_points`` is
        the broker's own ``symbol_info().spread`` in points, kept alongside it
        because that is the number a broker's own UI quotes.

        ``time_utc`` is **real UTC**. A tick's ``time`` is stamped on the
        broker's *server* clock, exactly like a ``copy_rates`` row, so it is
        converted the same way the candle pipeline converts one — broker wall
        clock, then off the discovered server offset. Reading the epoch as
        though it were already UTC puts every displayed quote out by the
        broker's own offset, which is hours.

        Returns ``None`` rather than raising when the terminal is closed or the
        symbol is not quoting, so a market table degrades to "no quote read"
        instead of failing a poll.
        """
        if not self.is_connected():
            return None
        try:
            tick = _mt5.symbol_info_tick(symbol)
        except Exception:
            return None
        if tick is None:
            return None

        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        epoch = int(getattr(tick, "time", 0) or 0)
        info = _mt5.symbol_info(symbol)
        points = getattr(info, "spread", None) if info is not None else None
        # Every numeric field is tested with ``is not None`` rather than for
        # truth: a legitimate zero — a zero-spread instrument, or the 0 a
        # broker reports in ``spread`` — is a real reading, and a truthiness
        # test would silently turn it into "unknown".
        return {
            "bid": float(bid) if bid is not None else None,
            "ask": float(ask) if ask is not None else None,
            # Only a *negative* difference is impossible: it means the quote is
            # crossed, so it is reported as unknown rather than as a real
            # (and impossible) number. A zero is a real spread on a zero-spread
            # account and is reported as one.
            "spread": (round(float(ask) - float(bid), 8)
                       if bid is not None and ask is not None and ask >= bid
                       else None),
            "spread_points": int(points) if points is not None else None,
            "digits": getattr(info, "digits", None) if info is not None else None,
            # The broker -> UTC step, using the offset the runner discovered
            # from this terminal (``time_utils.server_utc_offset_hours``). The
            # same conversion ``market_data.row_to_candle`` applies to a candle,
            # so a quote and the candle it sits beside cannot disagree about
            # what time it is.
            "time_utc": (tu.broker_to_utc(tu.utc_epoch_to_naive(epoch))
                         if epoch else None),
        }

    def _probe_symbols(self, preferred: list[str] | None, limit: int) -> list[str]:
        """Symbols to read the broker clock from, best first.

        Prefers the caller's own traded symbols (they are selected in Market
        Watch, and 24/5 instruments are the ones most likely to be quoting), then
        falls back to a few continuously-quoted majors.
        """
        out: list[str] = [s for s in (preferred or []) if s]
        available = set(self.symbol_names())
        for candidate in ("EURUSD", "USDCAD", "XAUUSD", "US100", "GBPUSD"):
            if candidate in available and candidate not in out:
                out.append(candidate)
        return out[:limit]

    def discover_server_utc_offset_hours(
            self, preferred_symbols: list[str] | None = None,
            limit: int = 5) -> tuple[float | None, str]:
        """Derive the broker's offset ahead of UTC from the terminal itself.

        Returns ``(offset_hours, detail)``; ``offset_hours`` is ``None`` when the
        offset could not be *verified*, and ``detail`` says why — callers must
        treat that as "unknown", never as zero.

        How it works: ``symbol_info_tick().time`` is the broker's own wall clock
        at the last quote, and the host knows real UTC, so the difference **is**
        the offset — no table of broker names, and no assumption that any given
        broker is UTC+2 or UTC+3. Observations are snapped to the quarter-hour
        grid brokers actually use, and only accepted when two or more symbols
        agree exactly.

        There is one trap, and it is the reason for the second half of this
        method: when the market is **closed** the last tick is not "now", it is
        the *close* instant, so the difference is the offset minus however long
        the market has been shut and looks like a plausible offset in its own
        right. The candidate is therefore confirmed against the newest M1 bar:
        if applying it does not place that bar within the last few minutes, the
        reading is stale and is rejected rather than guessed at.
        """
        if not self.is_connected():
            return None, "terminal not connected"

        real_now = tu.now_utc()
        votes: dict[float, int] = {}
        for symbol in self._probe_symbols(preferred_symbols, limit):
            epoch = self._tick_time_epoch(symbol)
            if epoch is None:
                continue
            broker_now = tu.utc_epoch_to_naive(epoch)
            raw = (broker_now - real_now).total_seconds()
            snapped = round(raw / OFFSET_QUANTUM_SECONDS) * OFFSET_QUANTUM_SECONDS
            if abs(snapped) > MAX_SANE_OFFSET_HOURS * 3600:
                continue
            # Only accept a reading that was already within the quantum grid;
            # a large residual means the clock is stale, not merely offset.
            if abs(raw - snapped) > OFFSET_TOLERANCE_SECONDS:
                continue
            hours = snapped / 3600.0
            votes[hours] = votes.get(hours, 0) + 1

        if not votes:
            return None, "no live tick from any probe symbol"

        offset_hours, agree = max(votes.items(), key=lambda kv: kv[1])
        if agree < 2:
            return None, (f"only one symbol reported the broker clock "
                          f"(UTC{offset_hours:+.2f}); need agreement to trust it")

        # Confirm against the newest bar, which cannot corroborate a stale read.
        confirmed, why = self._confirm_offset(offset_hours, preferred_symbols)
        if not confirmed:
            return None, why
        return offset_hours, f"discovered from {agree} symbol(s) via {why}"

    def _confirm_offset(self, offset_hours: float,
                        preferred_symbols: list[str] | None) -> tuple[bool, str]:
        """Is ``offset_hours`` consistent with a bar that opened *just now*?"""
        for symbol in self._probe_symbols(preferred_symbols, 3):
            epoch = self._newest_m1_open_epoch(symbol)
            if epoch is None:
                continue
            broker_open = tu.utc_epoch_to_naive(epoch)
            age = (tu.now_utc() - (broker_open - timedelta(hours=offset_hours))
                   ).total_seconds()
            if 0 <= age <= MAX_BAR_AGE_SECONDS:
                return True, f"newest {symbol} M1 bar opened {age:.0f}s ago"
        return False, ("broker clock reading is stale (the market looks closed), "
                       "so the offset cannot be verified right now")

    def discover_and_publish_server_offset(
            self, preferred_symbols: list[str] | None = None) -> tuple[float | None, str]:
        """Discover the broker offset and publish it process-wide.

        The single entry point the runner calls once per terminal session, so
        every later :func:`trading.time_utils.broker_to_utc` reads the verified
        number without needing a handle on this client. An explicit
        ``MT5_SERVER_UTC_OFFSET`` still wins, and a mismatch is reported loudly
        rather than obeyed in silence.
        """
        offset, detail = self.discover_server_utc_offset_hours(preferred_symbols)
        configured = tu.configured_server_utc_offset()
        if offset is not None:
            tu.set_server_utc_offset(offset, "discovered")
        if configured is not None and offset is not None and abs(configured - offset) > 1e-9:
            return offset, (f"{detail} — WARNING: disagrees with the pinned "
                            f"MT5_SERVER_UTC_OFFSET={configured:+.2f}, which wins")
        return offset, detail

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
    """Convert an MT5 row timestamp to a naive server-clock datetime.

    Delegates to :func:`trading.time_utils.utc_epoch_to_naive`, which is the one
    place that knows MT5 epochs are the broker's wall clock. Reading them with a
    bare ``datetime.fromtimestamp`` here would interpret them in the *host
    machine's* timezone instead — see that function's docstring.
    """
    from . import time_utils as tu

    return tu.utc_epoch_to_naive(row_time_epoch)
