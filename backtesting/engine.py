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
dashboard can plot it, alongside per-direction and per-session breakdowns used
to compare assets against each other.

Session attribution reuses the live strategy's :mod:`trading.sessions` helpers
against the trade's NY-clock entry time, so a backtest breakdown and a live
signal agree about which session a setup belongs to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from trading import sessions as sess
from trading import time_utils as tu
from trading.asset_manager import Asset
from trading.bars import Candle
from trading.strategy import ICTStrategy

DEFAULT_MAX_HOLD_M1 = 60 * 12  # allow multi-hour holds by default

#: Bucket key for trades whose entry falls outside every named ICT window.
OUTSIDE_SESSION = "outside"


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
    # --- direction split ---------------------------------------------------- #
    n_long: int = 0
    n_short: int = 0
    long_r: float = 0.0
    short_r: float = 0.0
    long_wins: int = 0
    short_wins: int = 0
    # --- risk quality ------------------------------------------------------- #
    max_consecutive_losses: int = 0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    avg_bars_held: float = 0.0
    # --- where the edge actually lives -------------------------------------- #
    by_session: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_silver_bullet: dict[str, dict[str, Any]] = field(default_factory=dict)

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

    @property
    def expectancy(self) -> float:
        """Average R per closed trade.

        This is the cross-asset ranking metric: it is risk-normalised, so it
        stays comparable as lot sizing or risk percent changes, and it rewards
        consistency rather than one outsized winner.
        """
        return self.total_r / self.n_trades if self.n_trades else 0.0

    @property
    def avg_win_r(self) -> float:
        return self.gross_win_r / self.n_wins if self.n_wins else 0.0

    @property
    def avg_loss_r(self) -> float:
        return self.gross_loss_r / self.n_losses if self.n_losses else 0.0

    @property
    def payoff_ratio(self) -> float:
        """Average win divided by average loss."""
        if self.n_losses == 0:
            return float("inf") if self.gross_win_r > 0 else 0.0
        loss = abs(self.avg_loss_r)
        return (self.avg_win_r / loss) if loss else 0.0

    @property
    def long_win_rate(self) -> float:
        return self.long_wins / self.n_long if self.n_long else 0.0

    @property
    def short_win_rate(self) -> float:
        return self.short_wins / self.n_short if self.n_short else 0.0

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
            "expectancy": round(self.expectancy, 4),
            "avg_win_r": round(self.avg_win_r, 4),
            "avg_loss_r": round(self.avg_loss_r, 4),
            "payoff_ratio": round(self.payoff_ratio, 4),
            "max_drawdown_r": round(self.max_drawdown_r(), 4),
            "max_consecutive_losses": self.max_consecutive_losses,
            "best_trade_r": round(self.best_trade_r, 4),
            "worst_trade_r": round(self.worst_trade_r, 4),
            "avg_bars_held": round(self.avg_bars_held, 2),
            "n_long": self.n_long,
            "n_short": self.n_short,
            "long_r": round(self.long_r, 4),
            "short_r": round(self.short_r, 4),
            "long_win_rate": round(self.long_win_rate, 4),
            "short_win_rate": round(self.short_win_rate, 4),
            "by_session": self.by_session,
            "by_silver_bullet": self.by_silver_bullet,
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
# Breakdowns (pure + testable)
# --------------------------------------------------------------------------- #
def _bucket_key(entry_time_utc: datetime) -> str:
    """Primary ICT session key for a trade entry, on the project NY clock."""
    window = sess.primary_session(tu.minute_of_day(tu.utc_to_ny(entry_time_utc)))
    return window.key if window else OUTSIDE_SESSION


def _silver_bullet_key(entry_time_utc: datetime) -> str:
    """Silver Bullet window key for a trade entry, or ``outside``."""
    key = sess.active_silver_bullet_key(tu.minute_of_day(tu.utc_to_ny(entry_time_utc)))
    return key or OUTSIDE_SESSION


def _breakdown(trades: list["BacktestTrade"],
               key_fn: Callable[[datetime], str]) -> dict[str, dict[str, Any]]:
    """Group closed trades into per-bucket statistics, best total R first.

    Deterministic ordering (total R, then bucket name) so two runs over the same
    trades render identically.
    """
    grouped: dict[str, list[BacktestTrade]] = {}
    for t in trades:
        grouped.setdefault(key_fn(t.entry_time_utc), []).append(t)

    out: dict[str, dict[str, Any]] = {}
    for key, rows in grouped.items():
        wins = sum(1 for t in rows if t.outcome == "WIN")
        total_r = sum(t.pnl_r for t in rows)
        out[key] = {
            "n_trades": len(rows),
            "n_wins": wins,
            "win_rate": round(wins / len(rows), 4) if rows else 0.0,
            "total_r": round(total_r, 4),
            "expectancy": round(total_r / len(rows), 4) if rows else 0.0,
        }
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["total_r"], kv[0])))


def _max_consecutive_losses(closed_by_time: list["BacktestTrade"]) -> int:
    """Longest run of consecutive losses in entry order."""
    worst = run = 0
    for t in closed_by_time:
        if t.outcome == "LOSS":
            run += 1
            worst = max(worst, run)
        else:
            run = 0
    return worst


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
        closed_by_time = sorted(closed, key=lambda x: x.entry_time_utc)

        curve: list[tuple[str, float]] = []
        eq = 0.0
        for t in closed_by_time:
            eq += t.pnl_r
            curve.append((t.exit_time_utc.isoformat() if t.exit_time_utc else "", eq))

        long_trades = [t for t in closed if t.direction == "buy"]
        short_trades = [t for t in closed if t.direction == "sell"]

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
            n_long=len(long_trades),
            n_short=len(short_trades),
            long_r=sum(t.pnl_r for t in long_trades),
            short_r=sum(t.pnl_r for t in short_trades),
            long_wins=sum(1 for t in long_trades if t.outcome == "WIN"),
            short_wins=sum(1 for t in short_trades if t.outcome == "WIN"),
            max_consecutive_losses=_max_consecutive_losses(closed_by_time),
            best_trade_r=max((t.pnl_r for t in closed), default=0.0),
            worst_trade_r=min((t.pnl_r for t in closed), default=0.0),
            avg_bars_held=(sum(t.bars_held for t in closed) / len(closed)
                           if closed else 0.0),
            by_session=_breakdown(closed, _bucket_key),
            by_silver_bullet=_breakdown(closed, _silver_bullet_key),
        )
        return s
