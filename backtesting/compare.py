"""Cross-asset comparison of backtest results.

One backtest request over N assets produces N :class:`database.models.Backtest`
rows sharing a single ``batch_id``. This module turns those per-asset summaries
into a *ranked* comparison, so the question the dashboard has to answer —
"which asset does this strategy actually work on?" — has one answer rather than
a list to read from memory.

Deliberately pure: no database, no MT5, no Flask. The repository stores and the
templates render; the ranking rule lives here, where it can be unit-tested.
"""
from __future__ import annotations

from typing import Any

#: Metric used to rank assets against each other. Average R per closed trade:
#: risk-normalised, so it stays comparable as lot sizing or risk percent
#: changes, and it rewards consistency over a single outsized winner.
RANK_METRIC = "expectancy"

_FLOAT_KEYS = (
    "expectancy", "total_r", "win_rate", "profit_factor", "max_drawdown_r",
    "avg_win_r", "avg_loss_r", "payoff_ratio", "best_trade_r", "worst_trade_r",
    "long_win_rate", "short_win_rate", "long_r", "short_r", "avg_bars_held",
)
_INT_KEYS = (
    "n_signals", "n_trades", "n_open", "n_wins", "n_losses", "n_long",
    "n_short", "max_consecutive_losses", "n_bars",
)

#: Breakdown maps, all with the same bucket -> stats shape. Period keys are read
#: by the same code paths as session keys, so they must survive ``tidy`` the same
#: way whether or not the row that produced them knew about them.
_BREAKDOWN_KEYS = ("by_session", "by_silver_bullet", "by_month", "by_week",
                   "by_day_of_week", "by_hour")


def _num(summary: dict, key: str, default: float = 0.0) -> float:
    """Read a numeric metric, tolerating absent or malformed values."""
    value = (summary or {}).get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def tidy(summary: dict | None) -> dict:
    """A summary with every metric this module reads present and numeric.

    Rows written before the extended metrics existed lack these keys entirely,
    so nothing here may assume they are present.
    """
    out: dict[str, Any] = dict(summary or {})
    for key in _FLOAT_KEYS:
        out[key] = _num(summary, key)
    for key in _INT_KEYS:
        out[key] = int(_num(summary, key))
    for key in _BREAKDOWN_KEYS:
        out.setdefault(key, {})
    out.setdefault("equity_curve", [])
    # ISO strings of the first entry and last exit, or None when the run never
    # traded. Deliberately not parsed: they are rendered, never ranked on.
    out.setdefault("first_entry_utc", None)
    out.setdefault("last_exit_utc", None)
    return out


def rank_assets(summaries: list[dict] | None) -> list[dict]:
    """Rank per-asset summaries best-first by :data:`RANK_METRIC`.

    An asset with no closed trades sorts below every asset that did trade.
    Ranking it on expectancy would place it level with a tested asset whose
    expectancy happens to be 0.0 — but "never traded" is no evidence, not
    neutral evidence, and it should not read as a recommendation.
    """
    ranked = [tidy(s) for s in (summaries or [])]
    ranked.sort(
        key=lambda s: (s["n_trades"] > 0, s[RANK_METRIC], s["total_r"]),
        reverse=True,
    )
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position
        row["tested"] = row["n_trades"] > 0
        row["is_best"] = position == 1 and row["tested"]
    return ranked


def batch_totals(summaries: list[dict] | None) -> dict[str, Any]:
    """Pooled totals across a batch.

    Indicative only: these rows are separate instruments replayed over the same
    window, not a portfolio, so the pooled figure double-counts concurrent risk
    and must not be read as an account-level result.
    """
    rows = [tidy(s) for s in (summaries or [])]
    n_trades = sum(r["n_trades"] for r in rows)
    n_wins = sum(r["n_wins"] for r in rows)
    total_r = sum(r["total_r"] for r in rows)
    return {
        "n_assets": len(rows),
        "n_assets_tested": sum(1 for r in rows if r["n_trades"] > 0),
        "n_signals": sum(r["n_signals"] for r in rows),
        "n_trades": n_trades,
        "n_open": sum(r["n_open"] for r in rows),
        "n_wins": n_wins,
        "n_losses": sum(r["n_losses"] for r in rows),
        "win_rate": round(n_wins / n_trades, 4) if n_trades else 0.0,
        "total_r": round(total_r, 4),
        "expectancy": round(total_r / n_trades, 4) if n_trades else 0.0,
    }
