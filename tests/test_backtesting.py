"""Backtesting engine tests.

Unit tests exercise the pure forward-simulator (SL/TP/WIN/LOSS/OPEN + the
within-bar "SL fills first" rule); end-to-end tests replay the exact synthetic
scenario used by the strategy suite through a real :class:`BacktestRunner` and
drive the resulting entry to its take-profit / stop-loss with continuation
candles.
"""
from datetime import datetime
from math import isinf

from backtesting.engine import BacktestRunner, simulate
from trading.asset_manager import Asset
from trading.signal_engine import Signal

from conftest import c, series
from test_strategy import build_scenario


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _signal(direction="buy", entry=100.0, sl=98.0, tp=105.0, rr=2.5) -> Signal:
    return Signal(
        asset="TEST", direction=direction, entry=entry, sl=sl, tp=tp,
        entry_time_utc=datetime(2026, 1, 6, 14, 0),
        entry_time_ny=datetime(2026, 1, 6, 10, 0),
        session_keys=["ny_am"], session_primary="ny_am",
        liquidity_type="PDL", liquidity_price=100.0,
        cisd_tf="M15", fvg_direction="bullish", rr=rr,
        status="APPROVED", risk_approved=True,
    )


def _after(dt: datetime, rows):
    """Consecutive M1 candles beginning one minute after ``dt`` (NY label)."""
    from datetime import timedelta
    start = dt + timedelta(minutes=1)
    return series(start, rows)


def _runner():
    return BacktestRunner(Asset(name="TEST", broker_symbol="TEST", enabled=True,
                                overrides={"min_history_h1": "5"}))


# --------------------------------------------------------------------------- #
# Pure forward simulation
# --------------------------------------------------------------------------- #
def test_simulate_buy_win():
    sig = _signal(entry=100.0, sl=98.0, tp=105.0, rr=2.5)
    following = _after(sig.entry_time_ny, [(100.5, 100.6, 99.0, 100.55),
                                           (100.55, 100.7, 100.4, 100.6),
                                           (100.6, 105.2, 100.5, 105.1)])
    t = simulate(sig, following, max_hold_m1=720)
    assert t.outcome == "WIN" and t.reason == "tp"
    assert t.exit_price == 105.0 and t.pnl_r == 2.5 and t.bars_held == 3


def test_simulate_buy_loss():
    sig = _signal(entry=100.0, sl=98.0, tp=105.0)
    following = _after(sig.entry_time_ny, [(99.5, 100.5, 97.5, 98.0)])
    t = simulate(sig, following, max_hold_m1=720)
    assert t.outcome == "LOSS" and t.reason == "sl"
    assert t.exit_price == 98.0 and t.pnl_r == -1.0


def test_simulate_buy_within_bar_sl_fills_first():
    """Same bar touches both SL and TP => conservative SL fill for a buy."""
    sig = _signal(entry=100.0, sl=98.0, tp=105.0)
    following = _after(sig.entry_time_ny, [(99.0, 106.0, 97.0, 105.0)])
    t = simulate(sig, following, max_hold_m1=720)
    assert t.outcome == "LOSS" and t.reason == "sl" and t.pnl_r == -1.0


def test_simulate_sell_win_and_loss():
    sig_sell_win = _signal(direction="sell", entry=100.0, sl=102.0, tp=95.0, rr=2.5)
    t = simulate(sig_sell_win, _after(sig_sell_win.entry_time_ny,
                                      [(99.0, 100.5, 94.5, 95.0)]), 720)
    assert t.outcome == "WIN" and t.reason == "tp" and t.pnl_r == 2.5

    sig_sell_loss = _signal(direction="sell", entry=100.0, sl=102.0, tp=95.0)
    t = simulate(sig_sell_loss, _after(sig_sell_loss.entry_time_ny,
                                       [(100.5, 102.5, 100.0, 102.0)]), 720)
    assert t.outcome == "LOSS" and t.reason == "sl" and t.pnl_r == -1.0


def test_simulate_open_when_no_exit_data():
    sig = _signal(entry=100.0, sl=98.0, tp=105.0)
    t = simulate(sig, [], max_hold_m1=720)
    assert t.outcome == "OPEN" and t.reason == "no_exit_in_window"
    assert t.pnl_r == 0.0 and t.exit_price == sig.entry


def test_simulate_max_hold_expiry():
    sig = _signal(entry=100.0, sl=98.0, tp=105.0)
    following = _after(sig.entry_time_ny, [(100.2, 100.4, 100.1, 100.3),
                                           (100.3, 100.5, 100.2, 100.4)])
    t = simulate(sig, following, max_hold_m1=1)
    assert t.outcome == "OPEN" and t.reason == "max_hold"
    assert t.pnl_r == 0.0 and t.bars_held == 1


# --------------------------------------------------------------------------- #
# End-to-end replay through a real strategy
# --------------------------------------------------------------------------- #
def test_runner_reproduces_live_pipeline():
    """The exact strategy scenario yields one OPEN buy (no exit candles yet)."""
    warm, feed = build_scenario()
    summary, trades = _runner().run(warm + feed)

    assert summary.n_signals == 1
    assert summary.n_trades == 0 and summary.n_open == 1
    assert summary.n_signals == summary.n_trades + summary.n_open
    t = trades[0]
    assert t.direction == "buy" and t.outcome == "OPEN"
    assert t.reason == "no_exit_in_window"
    assert t.entry > t.sl and t.tp > t.entry
    assert t.rr >= 1.5
    assert summary.equity_curve == []
    assert summary.to_dict()["n_open"] == 1


def test_runner_buy_rallies_to_tp():
    warm, feed = build_scenario()
    base = 101.7
    rows, prev = [], base
    for _ in range(45):                      # ~+0.35/bar, crosses TP well above
        o, prev = prev, prev + 0.35
        rows.append((o, o + 0.4, o - 0.1, prev))
    continuation = _after(feed[-1].t_ny, rows)

    summary, trades = _runner().run(warm + feed + continuation)

    assert summary.n_signals == 1 and summary.n_trades == 1 and summary.n_open == 0
    assert summary.n_wins == 1 and summary.n_losses == 0
    assert summary.win_rate == 1.0
    assert summary.total_r > 0
    assert isinf(summary.profit_factor)      # no losses => infinite PF
    assert len(summary.equity_curve) == 1 and summary.max_drawdown_r() == 0.0

    win = trades[0]
    assert win.direction == "buy" and win.outcome == "WIN" and win.reason == "tp"
    assert win.exit_price >= win.tp and win.pnl_r == win.rr


def test_runner_buy_dumps_to_sl():
    warm, feed = build_scenario()
    entry_close = feed[-1].close
    continuation = _after(feed[-1].t_ny, [(entry_close, entry_close + 0.2,
                                           97.0, 97.5)])  # deep dump => SL

    summary, trades = _runner().run(warm + feed + continuation)

    assert summary.n_signals == 1 and summary.n_trades == 1 and summary.n_open == 0
    assert summary.n_losses == 1 and summary.n_wins == 0
    assert summary.total_r == -1.0
    t = trades[0]
    assert t.outcome == "LOSS" and t.reason == "sl" and t.exit_price == t.sl


def test_backtest_persists_via_repository():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import Base
    from database.repository import Repository

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    repo = Repository(session=sessionmaker(bind=engine, expire_on_commit=False,
                                           future=True)())

    warm, feed = build_scenario()
    summary, trades = _runner().run(warm + feed)
    bt = repo.save_backtest(
        name="scenario", asset="TEST", symbol="TEST",
        start_utc=summary.start_utc, end_utc=summary.end_utc,
        params=summary.params, summary=summary.to_dict(),
        trades=[t.as_db_dict() for t in trades])
    rows = repo.backtest_trades(bt.id)
    assert len(rows) == 1 and rows[0].outcome == "OPEN"
    assert repo.recent_backtests()[0].summary_json["n_open"] == 1


def test_summary_exposes_the_comparison_metrics_end_to_end():
    """A real replay carries every key the cross-asset ranking page reads.

    The unit-level behaviour of each metric is pinned in ``test_compare.py``;
    this guards the contract end-to-end, through a real strategy run, so a
    metric that silently stops being populated cannot pass the suite.
    """
    warm, feed = build_scenario()
    rows, prev = [], 101.7
    for _ in range(45):
        o, prev = prev, prev + 0.35
        rows.append((o, o + 0.4, o - 0.1, prev))
    summary, _ = _runner().run(warm + feed + _after(feed[-1].t_ny, rows))
    d = summary.to_dict()

    for key in ("expectancy", "avg_win_r", "avg_loss_r", "payoff_ratio",
                "n_long", "n_short", "long_r", "short_r", "long_win_rate",
                "short_win_rate", "max_consecutive_losses", "best_trade_r",
                "worst_trade_r", "avg_bars_held", "by_session",
                "by_silver_bullet"):
        assert key in d, f"summary_json is missing {key}"

    assert d["expectancy"] == d["avg_r"] == d["total_r"]  # one closed winner
    assert d["n_long"] == 1 and d["n_short"] == 0
    assert d["long_r"] == d["total_r"] and d["short_r"] == 0.0
    assert d["max_consecutive_losses"] == 0
    assert d["worst_trade_r"] > 0
    assert d["avg_bars_held"] > 0
    # The scenario enters inside the NY morning, so the trade is attributed to
    # a real ICT session rather than falling through to "outside".
    assert sum(b["n_trades"] for b in d["by_session"].values()) == 1
    assert "outside" not in d["by_session"]


def test_simulate_buy_loss_on_mirror_geometry():
    """Direct simulate call already covers SL; guard sell-direction too."""
    sig = _signal(direction="sell", entry=100.0, sl=102.0, tp=95.0, rr=2.5)
    # Bar that never reaches either SL/TP then a TP hit later.
    following = _after(sig.entry_time_ny, [(100.3, 100.6, 100.1, 100.4),
                                           (100.4, 100.5, 94.8, 95.0)])
    t = simulate(sig, following, max_hold_m1=720)
    assert t.outcome == "WIN" and t.bars_held == 2
