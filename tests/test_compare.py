"""Cross-asset comparison and backtest breakdown tests.

Pure functions plus the engine's own aggregation, so nothing here needs MT5, a
database or Flask. The point of these tests is the ranking rule: which asset a
backtest campaign says to trade, and that it never says so on no evidence.
"""
from datetime import datetime, timedelta

from backtesting.compare import batch_totals, rank_assets, tidy
from backtesting.engine import OUTSIDE_SESSION, BacktestRunner, BacktestTrade
from trading.asset_manager import Asset


def _runner():
    return BacktestRunner(Asset(name="TEST", broker_symbol="TEST", enabled=True))


def _trade(*, direction="buy", outcome="WIN", pnl_r=1.0, at_ny=None,
           bars_held=10, rr=2.0) -> BacktestTrade:
    """A BacktestTrade entered at ``at_ny`` on the project's NY (UTC-4) clock."""
    at_ny = at_ny or datetime(2026, 1, 6, 9, 0)
    entry_utc = at_ny + timedelta(hours=4)  # NY clock is UTC-4
    return BacktestTrade(
        asset="TEST", direction=direction, entry=100.0, sl=98.0, tp=105.0,
        exit_price=105.0,
        entry_time_utc=entry_utc,
        exit_time_utc=entry_utc + timedelta(minutes=30),
        outcome=outcome, pnl_r=pnl_r, rr=rr, bars_held=bars_held,
        reason={"WIN": "tp", "LOSS": "sl"}.get(outcome, "no_exit_in_window"),
    )


def _summary_dict(trades):
    return _runner()._summarize([], trades, None, None).to_dict()


# --------------------------------------------------------------------------- #
# Engine metrics
# --------------------------------------------------------------------------- #
def test_summarize_reports_expectancy_and_direction_split():
    trades = [
        _trade(direction="buy", outcome="WIN", pnl_r=2.0,
               at_ny=datetime(2026, 1, 6, 9, 0)),
        _trade(direction="buy", outcome="LOSS", pnl_r=-1.0,
               at_ny=datetime(2026, 1, 6, 9, 5)),
        _trade(direction="sell", outcome="LOSS", pnl_r=-1.0,
               at_ny=datetime(2026, 1, 6, 9, 10)),
        _trade(direction="sell", outcome="OPEN", pnl_r=0.0,
               at_ny=datetime(2026, 1, 6, 9, 15)),
    ]
    d = _summary_dict(trades)

    assert d["n_trades"] == 3 and d["n_open"] == 1   # OPEN is excluded
    assert d["total_r"] == 0.0
    assert d["expectancy"] == 0.0                    # 0R pooled over 3 closed
    assert d["avg_win_r"] == 2.0
    assert d["avg_loss_r"] == -1.0
    assert d["payoff_ratio"] == 2.0
    assert d["best_trade_r"] == 2.0 and d["worst_trade_r"] == -1.0
    assert d["n_long"] == 2 and d["n_short"] == 1
    assert d["long_r"] == 1.0 and d["short_r"] == -1.0
    assert d["long_win_rate"] == 0.5 and d["short_win_rate"] == 0.0
    assert d["avg_bars_held"] == 10.0


def test_payoff_ratio_is_infinite_without_losses():
    d = _summary_dict([_trade(outcome="WIN", pnl_r=2.0)])
    assert d["n_losses"] == 0
    assert d["avg_loss_r"] == 0.0
    assert d["profit_factor"] == float("inf")
    assert d["payoff_ratio"] == float("inf")


def test_max_consecutive_losses_counts_the_longest_run():
    outcomes = ["LOSS", "LOSS", "WIN", "LOSS", "LOSS", "LOSS"]
    trades = [_trade(outcome=o, pnl_r=(2.0 if o == "WIN" else -1.0),
                     at_ny=datetime(2026, 1, 6, 9, i))
              for i, o in enumerate(outcomes)]
    d = _summary_dict(trades)
    assert d["max_consecutive_losses"] == 3

    # A win resets the run.
    assert _summary_dict([_trade(outcome="LOSS"), _trade(outcome="WIN"),
                          _trade(outcome="LOSS")])["max_consecutive_losses"] == 1


def test_session_and_silver_bullet_breakdowns_use_the_ny_clock():
    trades = [
        _trade(outcome="WIN", pnl_r=2.0, at_ny=datetime(2026, 1, 6, 9, 0)),    # ny_am
        _trade(outcome="LOSS", pnl_r=-1.0, at_ny=datetime(2026, 1, 6, 9, 30)), # ny_am
        _trade(outcome="WIN", pnl_r=1.5, at_ny=datetime(2026, 1, 6, 14, 0)),   # ny_pm
    ]
    d = _summary_dict(trades)

    am = d["by_session"]["ny_am"]
    assert am["n_trades"] == 2 and am["n_wins"] == 1
    assert am["expectancy"] == 0.5           # (2.0 - 1.0) / 2

    pm = d["by_session"]["ny_pm"]
    assert pm["n_trades"] == 1 and pm["expectancy"] == 1.5

    # Silver Bullet membership is a subset of the session picture.
    assert d["by_silver_bullet"]["ny_pm_sb"]["n_trades"] == 1
    assert d["by_silver_bullet"][OUTSIDE_SESSION]["n_trades"] == 2


def test_breakdown_is_empty_without_trades():
    d = _summary_dict([])
    assert d["by_session"] == {} and d["by_silver_bullet"] == {}
    assert d["expectancy"] == 0.0 and d["win_rate"] == 0.0


# --------------------------------------------------------------------------- #
# Period breakdowns
# --------------------------------------------------------------------------- #
def test_period_buckets_use_the_ny_clock():
    """The bucket is the NY period the setup triggered in, not the UTC one.

    An entry at 03:00 UTC on 1 January is 23:00 on 31 December in NY, so reading
    the UTC clock would file it in the wrong month *and* the wrong year. The
    offset is exactly what breaks here, so it is what gets pinned.
    """
    d = _summary_dict([_trade(outcome="WIN", pnl_r=1.0,
                              at_ny=datetime(2025, 12, 31, 23, 0))])

    assert list(d["by_month"]) == ["2025-12"]
    assert list(d["by_hour"]) == ["23:00"]
    assert list(d["by_day_of_week"]) == ["Wed"]


def test_iso_week_can_disagree_with_the_month_at_a_year_boundary():
    """The two keys answer different questions and are allowed to differ.

    ISO weeks belong to the year holding their Thursday, so 31 December 2025
    (a Wednesday) sits in 2026-W01 while its calendar month is 2025-12.
    Deriving the week from the month would report the wrong one of these.
    """
    d = _summary_dict([_trade(outcome="WIN", pnl_r=1.0,
                              at_ny=datetime(2025, 12, 31, 23, 0))])

    assert list(d["by_month"]) == ["2025-12"]
    assert list(d["by_week"]) == ["2026-W01"]


def test_period_breakdown_reads_chronologically():
    """Periods are a time series, so they are ordered by date, not by R.

    Ranked the way sessions are, these three months would read March, January,
    February -- so a chronological result is a real statement about the ordering
    rule rather than an accident of the data.
    """
    trades = [
        _trade(outcome="WIN", pnl_r=1.0, at_ny=datetime(2026, 1, 5, 9, 0)),
        _trade(outcome="LOSS", pnl_r=-1.0, at_ny=datetime(2026, 2, 5, 9, 0)),
        _trade(outcome="WIN", pnl_r=3.0, at_ny=datetime(2026, 3, 5, 9, 0)),
    ]
    d = _summary_dict(trades)

    assert list(d["by_month"]) == ["2026-01", "2026-02", "2026-03"]
    assert [b["total_r"] for b in d["by_month"].values()] == [1.0, -1.0, 3.0]

    # The session breakdown keeps the opposite rule: where the edge lives is a
    # ranking, and best total R first is how it is read.
    assert list(_summary_dict([
        _trade(outcome="WIN", pnl_r=1.0, at_ny=datetime(2026, 1, 5, 9, 0)),
        _trade(outcome="WIN", pnl_r=5.0, at_ny=datetime(2026, 1, 5, 14, 0)),
    ])["by_session"]) == ["ny_pm", "ny_am"]


def test_weekdays_are_ordered_by_calendar_not_alphabetically():
    """Fri/Sat/Sun sort wrongly as text, so the weekday order is explicit."""
    trades = [
        _trade(outcome="WIN", pnl_r=1.0, at_ny=datetime(2026, 3, 6, 9, 0)),  # Fri
        _trade(outcome="WIN", pnl_r=1.0, at_ny=datetime(2026, 3, 2, 9, 0)),  # Mon
    ]
    assert list(_summary_dict(trades)["by_day_of_week"]) == ["Mon", "Fri"]


def test_period_breakdowns_exclude_open_trades():
    """An open trade has no outcome, so it cannot inform a period's edge."""
    d = _summary_dict([
        _trade(outcome="OPEN", pnl_r=0.0, at_ny=datetime(2026, 1, 5, 9, 0)),
    ])
    assert d["n_open"] == 1
    assert d["by_month"] == {} and d["by_hour"] == {}


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def _asset(asset, n_trades, expectancy, total_r):
    return {"asset": asset, "n_trades": n_trades, "expectancy": expectancy,
            "total_r": total_r}


def test_rank_assets_orders_by_expectancy_desc():
    ranked = rank_assets([_asset("C", 4, 0.5, 2.0),
                          _asset("A", 4, 2.0, 8.0),
                          _asset("B", 4, -1.0, -4.0)])
    assert [r["asset"] for r in ranked] == ["A", "C", "B"]
    assert [r["rank"] for r in ranked] == [1, 2, 3]
    assert ranked[0]["is_best"] is True
    assert not any(r["is_best"] for r in ranked[1:])


def test_rank_assets_puts_untraded_assets_last():
    """No trades is no evidence -- it must never outrank a tested asset."""
    ranked = rank_assets([_asset("NEVER", 0, 0.0, 0.0),
                          _asset("LOSSY", 3, -0.5, -1.5)])
    assert [r["asset"] for r in ranked] == ["LOSSY", "NEVER"]
    assert ranked[0]["is_best"] is True
    assert ranked[1]["tested"] is False


def test_rank_assets_marks_nothing_best_when_nothing_traded():
    ranked = rank_assets([_asset("QUIET", 0, 0.0, 0.0)])
    assert ranked[0]["is_best"] is False


def test_rank_assets_tolerates_summaries_written_before_the_new_metrics():
    """Old rows lack every extended key; they must still rank, not crash."""
    ranked = rank_assets([{"asset": "OLD", "n_trades": 2, "total_r": 3.0,
                           "win_rate": 0.5}])
    row = ranked[0]
    assert row["expectancy"] == 0.0
    assert row["profit_factor"] == 0.0
    assert row["max_consecutive_losses"] == 0
    assert row["by_session"] == {} and row["equity_curve"] == []
    assert row["tested"] is True


def test_tidy_fills_the_period_breakdowns_on_a_legacy_row():
    """Period breakdowns post-date the session ones, so a stored row can have
    the first pair and not the second. ``tidy`` is the seam that keeps the
    detail page from tripping over its own history."""
    row = tidy({"asset": "OLD", "n_trades": 1})
    for key in ("by_month", "by_week", "by_day_of_week", "by_hour"):
        assert row[key] == {}, f"{key} was not defaulted"
    assert row["n_bars"] == 0
    # Absent, not empty-string: the template renders these through the `ny`
    # filter, which maps None to "" and would print an empty cell otherwise.
    assert row["first_entry_utc"] is None and row["last_exit_utc"] is None


def test_rank_assets_of_nothing_is_empty():
    assert rank_assets([]) == []
    assert rank_assets(None) == []


# --------------------------------------------------------------------------- #
# Batch totals
# --------------------------------------------------------------------------- #
def test_batch_totals_pools_assets():
    totals = batch_totals([
        {"n_signals": 3, "n_trades": 2, "n_wins": 1, "n_losses": 1,
         "n_open": 1, "total_r": 4.0},
        {"n_signals": 2, "n_trades": 2, "n_wins": 2, "n_losses": 0,
         "n_open": 0, "total_r": 2.0},
    ])
    assert totals["n_assets"] == 2 and totals["n_assets_tested"] == 2
    assert totals["n_signals"] == 5 and totals["n_open"] == 1
    assert totals["n_trades"] == 4 and totals["n_wins"] == 3
    assert totals["total_r"] == 6.0
    assert totals["expectancy"] == 1.5
    assert totals["win_rate"] == 0.75


def test_batch_totals_with_no_trades_is_zero_not_a_crash():
    totals = batch_totals([{"n_trades": 0, "n_signals": 2}])
    assert totals["expectancy"] == 0.0
    assert totals["win_rate"] == 0.0
    assert totals["n_assets_tested"] == 0
    assert totals["n_assets"] == 1
