"""Event-driven backtester over the same ICT strategy modules.

The backtester is a **closed-candle replay**: it feeds the exact same
:class:`trading.strategy.ICTStrategy` instance the live scanner uses, so the
detection logic under test is identical to production (no duplicated signals,
no lookahead). Every approved signal emitted by the engine is then simulated
forward over the already-closed M1 candles that follow it.

Simulation model
----------------
* A BUY is stopped out the first bar whose ``low <= sl`` and wins the first bar
  whose ``high >= tp``. When both would trigger inside the same bar the SL is
  assumed to fill first (conservative). SELL is the mirror.
* A trade still open when the backtest window ends is counted as ``OPEN``
  (excluded from the win rate; shown separately).
* ``max_hold_m1`` caps how many M1 bars an entry may remain open; exceeding it
  closes the trade at the last close and marks it ``OPEN``/expired.

Statistics are reported in **R multiples** (one unit of risk per trade), which
is broker- and lot-size-neutral. An R-based equity curve is returned so a
dashboard can plot it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from trading.asset_manager import Asset
from trading.bars import Candle
from trading.strategy import ICTStrategy

DEFAULT_MAX_HOLD_M1 = 60 * 12  # allow multi-hour holds by default


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass
class BacktestSummary:
    asset: str
    start_utc: datetime | None
    end_utc: datetime | None
    params: dict[str, Any]
    n_signals: int = 0
    n_trades: int = 0          # closed (win+loss)
    n_open: int = 0
    n_wins: int = 0
    n_losses: int = 0
    total_r: float = 0.0
    gross_win_r: float = 0.0
    gross_loss_r: float = 0.0
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_trades if self.n_trades else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss_r == 0:
            return float("inf") if self.gross_win_r > 0 else 0.0
        return self.gross_win_r / abs(self.gross_loss_r)

    @property
    def avg_r(self) -> float:
        return self.total_r / self.n_trades if self.n_trades else 0.0

    def max_drawdown_r(self) -> float:
        peak = 0.0
        mdd = 0.0
        for _t, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                mdd = max(mdd, peak - eq)
        return mdd

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "n_signals": self.n_signals,
            "n_trades": self.n_trades,
            "n_open": self.n_open,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "total_r": round(self.total_r, 4),
            "avg_r": round(self.avg_r, 4),
            "max_drawdown_r": round(self.max_drawdown_r(), 4),
            "equity_curve": [[t, round(eq, 4)] for t, eq in self.equity_curve],
        }


@dataclass
class BacktestTrade:
    asset: str
    direction: str
    entry: float
    sl: float
    tp: float
    exit_price: float
    entry_time_utc: datetime
    exit_time_utc: datetime | None
    outcome: str            # WIN | LOSS | OPEN
    pnl_r: float
    rr: float
    bars_held: int
    reason: str

    def as_db_dict(self) -> dict:
        return {
            "asset": self.asset,
            "direction": self.direction,
            "entry": self.entry,
            "sl": self.sl,
            "tp": self.tp,
            "exit_price": self.exit_price,
            "entry_time_utc": self.entry_time_utc,
            "exit_time_utc": self.exit_time_utc,
            "outcome": self.outcome,
            "pnl": self.pnl_r,
            "rr": self.rr,
            "bars_held": self.bars_held,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Trade simulation (pure + testable)
# --------------------------------------------------------------------------- #
def simulate(signal, following: list[Candle], max_hold_m1: int) -> BacktestTrade:
    """Simulate one signal against the closed candles that follow its entry bar.

    ``following`` are candles strictly *after* the signal's own (closed) entry
    candle, oldest first. Returns a closed or open :class:`BacktestTrade`.
    """
    direction = signal.direction
    entry_time = signal.entry_time_utc
    held = 0
    exit_price = signal.entry
    exit_time: datetime | None = None
    outcome = "OPEN"
    reason = "window_end"

    for c in following:
        if held >= max_hold_m1:
            break
        held += 1
        if direction == "buy":
            if c.low <= signal.sl:          # SL fills before any TP in-bar
                exit_price, exit_time, outcome, reason = signal.sl, c.t_utc, "LOSS", "sl"
                break
            if c.high >= signal.tp:
                exit_price, exit_time, outcome, reason = signal.tp, c.t_utc, "WIN", "tp"
                break
        else:  # sell
            if c.high >= signal.sl:
                exit_price, exit_time, outcome, reason = signal.sl, c.t_utc, "LOSS", "sl"
                break
            if c.low <= signal.tp:
                exit_price, exit_time, outcome, reason = signal.tp, c.t_utc, "WIN", "tp"
                break

    if outcome == "OPEN":
        # Expire at last seen close so the trade is priced, but count as OPEN.
        exit_price = following[-1].close if following else signal.entry
        exit_time = following[-1].t_utc if following else entry_time
        reason = "max_hold" if held >= max_hold_m1 and following else "no_exit_in_window"

    rr = signal.rr
    if outcome == "WIN":
        pnl_r = rr
    elif outcome == "LOSS":
        pnl_r = -1.0
    else:
        pnl_r = 0.0  # OPEN trades add no closed PnL
    return BacktestTrade(
        asset=signal.asset,
        direction=direction,
        entry=signal.entry,
        sl=signal.sl,
        tp=signal.tp,
        exit_price=exit_price,
        entry_time_utc=entry_time,
        exit_time_utc=exit_time,
        outcome=outcome,
        pnl_r=pnl_r,
        rr=rr,
        bars_held=held,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class BacktestRunner:
    """Replay M1 closed candles through a real ICTStrategy and price exits."""

    def __init__(self, asset: Asset, *, max_hold_m1: int = DEFAULT_MAX_HOLD_M1,
                 extra_params: dict[str, Any] | None = None):
        self.asset = asset
        self.max_hold_m1 = max_hold_m1
        self.extra_params = dict(extra_params or {})
        overrides = dict(asset.overrides)
        overrides.update(self.extra_params)
        self.strategy = ICTStrategy(Asset(
            name=asset.name, broker_symbol=asset.broker_symbol,
            enabled=True, digits=asset.digits, overrides=overrides))

    # ------------------------------------------------------------------ #
    def run(self, m1_candles: list[Candle], name: str = "") -> tuple[BacktestSummary,
                                                                     list[BacktestTrade]]:
        """Feed closed M1 candles oldest-first; return (summary, trades)."""
        candles = sorted(m1_candles, key=lambda c: c.t_utc)
        start_utc = candles[0].t_utc if candles else None
        end_utc = candles[-1].t_utc if candles else None

        signals: list = []
        by_time: dict[datetime, int] = {}
        for idx, c in enumerate(candles):
            by_time[c.t_utc] = idx
            signals.extend(self.strategy.feed(c))

        trades: list[BacktestTrade] = []
        for sig in signals:
            sig_idx = by_time.get(sig.entry_time_utc)
            if sig_idx is None:
                continue
            following = candles[sig_idx + 1:]
            trades.append(simulate(sig, following, self.max_hold_m1))

        summary = self._summarize(signals, trades, start_utc, end_utc)
        summary.params = {
            "max_hold_m1": self.max_hold_m1,
            "asset_overrides": dict(self.asset.overrides),
            "extra": self.extra_params,
        }
        return summary, trades

    # ------------------------------------------------------------------ #
    def _summarize(self, signals: list, trades: list[BacktestTrade],
                   start_utc: datetime | None, end_utc: datetime | None) -> BacktestSummary:
        closed = [t for t in trades if t.outcome in ("WIN", "LOSS")]
        wins = [t for t in closed if t.outcome == "WIN"]
        losses = [t for t in closed if t.outcome == "LOSS"]
        open_trades = [t for t in trades if t.outcome == "OPEN"]

        curve: list[tuple[str, float]] = []
        eq = 0.0
        for t in sorted(closed, key=lambda x: x.entry_time_utc):
            eq += t.pnl_r
            curve.append((t.exit_time_utc.isoformat() if t.exit_time_utc else "", eq))

        s = BacktestSummary(
            asset=self.asset.name,
            start_utc=start_utc,
            end_utc=end_utc,
            params={},
            n_signals=len(signals),
            n_trades=len(closed),
            n_open=len(open_trades),
            n_wins=len(wins),
            n_losses=len(losses),
            total_r=sum(t.pnl_r for t in closed),
            gross_win_r=sum(t.pnl_r for t in wins),
            gross_loss_r=sum(t.pnl_r for t in losses),
            equity_curve=curve,
        )
        return s
