"""Database engine, sessions and repository functions.

The repository is deliberately thin: it persists domain objects (Signal,
ExecutionResult, backtest output) to the schema in :mod:`database.models` and
queries them back for the dashboard. It contains **no strategy logic** and
**no MT5/AI/telegram code**, so it can be tested with an in-memory SQLite DB.

``get_engine`` / ``get_session`` build on the settings ``db_url`` (SQLite by
default; setting ``DATABASE_URL`` migrates to PostgreSQL with no code change).
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterator

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from config import Settings, get_settings

from . import models as m
from .models import Base

# Re-export models for callers that prefer ``database.models``.
__all__ = ["Base", "models", "get_engine", "get_session", "init_db",
           "Repository"]


def _make_engine(db_url: str):
    kwargs: dict = {"future": True}
    # SQLite needs check_same_thread=False for Flask's default threads.
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(db_url, **kwargs)


def get_engine(settings: Settings | None = None):
    cfg = settings or get_settings()
    return _make_engine(cfg.db_url)


def init_db(settings: Settings | None = None) -> None:
    """Create all tables (idempotent). Call once at startup."""
    Base.metadata.create_all(get_engine(settings))


def get_session(settings: Settings | None = None) -> Session:
    engine = get_engine(settings)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return maker()


class Repository:
    """Persistence facade used by the scanner, backtester and Flask app."""

    def __init__(self, settings: Settings | None = None, session: Session | None = None):
        self.settings = settings or get_settings()
        self._own_session = session is None
        self.session = session or get_session(settings)

    def close(self) -> None:
        if self._own_session:
            self.session.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Signals
    # ------------------------------------------------------------------ #
    def fingerprint_exists(self, fingerprint: str) -> bool:
        row = self.session.execute(
            select(m.Signal.id).where(m.Signal.fingerprint == fingerprint)
        ).first()
        return row is not None

    def save_signal(self, sig) -> m.Signal:
        """Insert a domain Signal as a row; dedupe on fingerprint (idempotent)."""
        existing = self.session.execute(
            select(m.Signal).where(m.Signal.fingerprint == sig.fingerprint())
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = m.Signal(
            fingerprint=sig.fingerprint(),
            asset=sig.asset,
            direction=sig.direction,
            entry=sig.entry,
            sl=sig.sl,
            tp=sig.tp,
            entry_time_utc=sig.entry_time_utc,
            entry_time_ny=sig.entry_time_ny,
            session_keys=sig.session_keys,
            session_primary=sig.session_primary,
            silver_bullet=sig.silver_bullet,
            macro=sig.macro,
            liquidity_type=sig.liquidity_type,
            liquidity_price=sig.liquidity_price,
            purge_time_ny=sig.purge_time_ny,
            cisd_tf=sig.cisd_tf,
            cisd_confirm_time_ny=sig.cisd_confirm_time_ny,
            fvg_direction=sig.fvg_direction,
            fvg_lower=sig.fvg_lower,
            fvg_upper=sig.fvg_upper,
            fvg_formation_time_ny=sig.fvg_formation_time_ny,
            structure_extreme_price=sig.structure_extreme_price,
            rr=sig.rr,
            status=sig.status,
            reason=sig.reason,
            alert_only=sig.alert_only,
            risk_approved=sig.risk_approved,
            ai_status=sig.ai_status,
            ai_score=sig.ai_score,
            ai_decision=sig.ai_decision,
            ai_reasoning=sig.ai_reasoning,
            ai_confidence=sig.ai_confidence,
            ai_strengths=sig.ai_strengths,
            ai_risks=sig.ai_risks,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def update_signal_ai(self, signal_id: int, sig) -> m.Signal | None:
        """Persist a signal's AI overlay after analysis (deduped save first)."""
        row = self.session.get(m.Signal, signal_id)
        if row is None:
            return None
        row.ai_status = sig.ai_status
        row.ai_score = sig.ai_score
        row.ai_decision = sig.ai_decision
        row.ai_reasoning = sig.ai_reasoning
        row.ai_confidence = sig.ai_confidence
        row.ai_strengths = list(sig.ai_strengths or [])
        row.ai_risks = list(sig.ai_risks or [])
        self.session.commit()
        return row

    def recent_signals(self, limit: int = 100) -> list[m.Signal]:
        rows = self.session.execute(
            select(m.Signal).order_by(m.Signal.id.desc()).limit(limit)
        ).scalars().all()
        return list(rows)

    def get_signal(self, signal_id: int) -> m.Signal | None:
        return self.session.get(m.Signal, signal_id)

    def find_signals(self, *, status: str | None = None, asset: str | None = None,
                     limit: int = 500) -> list[m.Signal]:
        """Filtered listing (newest first) for the dashboard signals page."""
        stmt = select(m.Signal)
        if status:
            stmt = stmt.where(m.Signal.status == status)
        if asset:
            stmt = stmt.where(m.Signal.asset == asset)
        stmt = stmt.order_by(m.Signal.id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def count_signals(self, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(m.Signal)
        if status is not None:
            stmt = stmt.where(m.Signal.status == status)
        return int(self.session.execute(stmt).scalar_one())

    def signals_since(self, after_utc: datetime, limit: int = 500) -> list[m.Signal]:
        rows = self.session.execute(
            select(m.Signal).where(m.Signal.entry_time_utc >= after_utc)
            .order_by(m.Signal.entry_time_utc.asc()).limit(limit)
        ).scalars().all()
        return list(rows)

    # ------------------------------------------------------------------ #
    # Trades (execution attempts)
    # ------------------------------------------------------------------ #
    def save_trade(self, result, signal_id: int | None = None) -> m.Trade:
        row = m.Trade(
            signal_id=signal_id,
            fingerprint=result.signal_fingerprint,
            asset=result.asset,
            symbol=result.symbol,
            direction=result.direction,
            entry=result.entry,
            sl=result.sl,
            tp=result.tp,
            lots=result.lots,
            status=result.status,
            reason=result.reason,
            requested_at_utc=result.requested_at_utc,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def recent_trades(self, limit: int = 100) -> list[m.Trade]:
        rows = self.session.execute(
            select(m.Trade).order_by(m.Trade.id.desc()).limit(limit)
        ).scalars().all()
        return list(rows)

    def trades_for_fingerprint(self, fingerprint: str, limit: int = 50) -> list[m.Trade]:
        """Execution attempts recorded against a given signal fingerprint."""
        rows = self.session.execute(
            select(m.Trade).where(m.Trade.fingerprint == fingerprint)
            .order_by(m.Trade.id.desc()).limit(limit)
        ).scalars().all()
        return list(rows)

    def count_trades(self, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(m.Trade)
        if status is not None:
            stmt = stmt.where(m.Trade.status == status)
        return int(self.session.execute(stmt).scalar_one())

    def count_backtests(self) -> int:
        return int(self.session.execute(
            select(func.count()).select_from(m.Backtest)).scalar_one())

    # ------------------------------------------------------------------ #
    # Backtests
    # ------------------------------------------------------------------ #
    def save_backtest(self, *, name: str, asset: str, symbol: str | None,
                      start_utc: datetime | None, end_utc: datetime | None,
                      params: dict, summary: dict,
                      trades: list[dict]) -> m.Backtest:
        bt = m.Backtest(
            name=name, asset=asset, symbol=symbol,
            start_utc=start_utc, end_utc=end_utc,
            params_json=params, summary_json=summary,
        )
        self.session.add(bt)
        self.session.flush()  # obtain bt.id
        for t in trades:
            self.session.add(m.BacktestTrade(backtest_id=bt.id, **t))
        self.session.commit()
        return bt

    def recent_backtests(self, limit: int = 50) -> list[m.Backtest]:
        rows = self.session.execute(
            select(m.Backtest).order_by(m.Backtest.id.desc()).limit(limit)
        ).scalars().all()
        return list(rows)

    def get_backtest(self, backtest_id: int) -> m.Backtest | None:
        return self.session.get(m.Backtest, backtest_id)

    def backtest_trades(self, backtest_id: int) -> list[m.BacktestTrade]:
        rows = self.session.execute(
            select(m.BacktestTrade).where(m.BacktestTrade.backtest_id == backtest_id)
            .order_by(m.BacktestTrade.entry_time_utc.asc())
        ).scalars().all()
        return list(rows)

    # ------------------------------------------------------------------ #
    # System events
    # ------------------------------------------------------------------ #
    def log_event(self, level: str, source: str, message: str) -> None:
        self.session.add(m.SystemEvent(level=level, source=source, message=message))
        self.session.commit()

    def recent_events(self, limit: int = 100) -> list[m.SystemEvent]:
        rows = self.session.execute(
            select(m.SystemEvent).order_by(m.SystemEvent.id.desc()).limit(limit)
        ).scalars().all()
        return list(rows)

    # ------------------------------------------------------------------ #
    # Assets
    # ------------------------------------------------------------------ #
    def upsert_asset(self, name: str, broker_symbol: str, enabled: bool,
                     digits: int | None = None, overrides: dict | None = None) -> m.Asset:
        row = self.session.get(m.Asset, name)
        if row is None:
            row = m.Asset(name=name, broker_symbol=broker_symbol, enabled=enabled,
                          digits=digits, overrides=overrides or {})
            self.session.add(row)
        else:
            row.broker_symbol = broker_symbol
            row.enabled = enabled
            row.digits = digits
            row.overrides = overrides or {}
        self.session.commit()
        return row

    def list_assets(self) -> list[m.Asset]:
        rows = self.session.execute(select(m.Asset).order_by(m.Asset.name)).scalars().all()
        return list(rows)


def iter_session(settings: Settings | None = None) -> Iterator[Session]:
    """Context-managed session for scripts/CLI."""
    s = get_session(settings)
    try:
        yield s
    finally:
        s.close()
