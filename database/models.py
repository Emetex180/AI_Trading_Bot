"""SQLAlchemy ORM models (SQLite now, PostgreSQL-ready).

Design notes
------------
* All datetimes are stored as *naive UTC* (matching the project convention).
  Column helpers default to UTC.
* ``JSON`` columns map to ``TEXT`` on SQLite and ``JSONB`` on PostgreSQL with
  the same code, so migrating only changes the connection URL.
* Every id is a plain ``Integer`` PK; a unique ``fingerprint`` is the logical
  key used to prevent duplicate signals/trades.
* Foreign keys are declared so later PostgreSQL DDL is clean.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from trading import time_utils as tu


def _utcnow() -> datetime:
    return tu.now_utc()


class Base(DeclarativeBase):
    pass


class Asset(Base):
    __tablename__ = "assets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    digits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow, onupdate=_utcnow)


class Signal(Base):
    """One validated/analysed ICT setup (fingerprint is the dedupe key)."""

    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("fingerprint", name="uq_signals_fingerprint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    sl: Mapped[float] = mapped_column(Float, nullable=False)
    tp: Mapped[float] = mapped_column(Float, nullable=False)
    entry_time_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    entry_time_ny: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    session_keys: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    session_primary: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    silver_bullet: Mapped[str | None] = mapped_column(String(32), nullable=True)
    macro: Mapped[str | None] = mapped_column(String(32), nullable=True)
    liquidity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    liquidity_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    purge_time_ny: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cisd_tf: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    cisd_confirm_time_ny: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fvg_direction: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    fvg_lower: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fvg_upper: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fvg_formation_time_ny: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    structure_extreme_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    alert_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    risk_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # AI overlay (advisory only).
    ai_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ai_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_strengths: Mapped[list] = mapped_column(JSON, nullable=True, default=list)
    ai_risks: Mapped[list] = mapped_column(JSON, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    executions: Mapped[list["Trade"]] = relationship(
        back_populates="signal", cascade="all, delete-orphan")


class Trade(Base):
    """Execution attempt for a signal (safe executor output)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    sl: Mapped[float] = mapped_column(Float, nullable=False)
    tp: Mapped[float] = mapped_column(Float, nullable=False)
    lots: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # SKIPPED/SENT/FAILED
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                       default=_utcnow)

    signal: Mapped["Signal"] = relationship(back_populates="executions")


class Backtest(Base):
    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    asset: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Groups the per-asset rows produced by ONE backtest request, so a campaign
    # over N assets can be ranked and compared instead of reading as N unrelated
    # runs. NULL on rows written before batching existed (and on any single-asset
    # run from an older build); those are surfaced as one-row batches.
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    start_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    trades: Mapped[list["BacktestTrade"]] = relationship(
        back_populates="backtest", cascade="all, delete-orphan")


class BacktestTrade(Base):
    """A simulated fill / closed trade produced by the backtester."""

    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[int] = mapped_column(ForeignKey("backtests.id"), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    sl: Mapped[float] = mapped_column(Float, nullable=False)
    tp: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    entry_time_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exit_time_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # WIN/LOSS/OPEN
    pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    bars_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    backtest: Mapped["Backtest"] = relationship(back_populates="trades")


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    event_time_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                     default=_utcnow)
