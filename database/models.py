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
    purge_grade: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    purge_time_ny: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cisd_tf: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    cisd_confirm_time_ny: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fvg_direction: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    fvg_lower: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fvg_upper: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fvg_formation_time_ny: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Stop anchor: the M5 candle that took the liquidity the stop sits behind.
    structure_extreme_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    structure_time_ny: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Take-profit target: the liquidity pull the trade is aimed at, which is a
    # different level from the one that was purged (``liquidity_type`` above).
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    target_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    target_grade: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    # Geometry and the transparent efficiency score.
    risk_points: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reward_points: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    efficiency_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Deterministic setup identity and the state the engine settled in.
    setup_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # Price precision of the instrument, so the dashboard can format the levels
    # in an alert without re-reading the asset registry.
    digits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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


# --------------------------------------------------------------------------- #
# Web platform: accounts, access and broker links
#
# Added for the client-facing platform. Purely additive — no existing table is
# altered, and every table below is created by the same ``create_all`` in
# ``repository.ensure_schema`` that already builds the rest of the schema, so an
# existing production database simply gains four new (empty) tables.
# --------------------------------------------------------------------------- #
#: Roles. ``admin`` sees the whole platform; ``client`` sees only the analysis.
ROLE_ADMIN = "admin"
ROLE_CLIENT = "client"
ROLES = (ROLE_ADMIN, ROLE_CLIENT)

#: Account lifecycle. A suspended account keeps its history but cannot log in.
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"

#: Subscription state shown on the admin's client list. ``expired`` and ``none``
#: are recorded states, not computed ones — nothing here is inferred.
SUB_NONE = "none"
SUB_TRIAL = "trial"
SUB_ACTIVE = "active"
SUB_EXPIRED = "expired"
SUBSCRIPTION_STATES = (SUB_NONE, SUB_TRIAL, SUB_ACTIVE, SUB_EXPIRED)


class User(Base):
    """A person who can log in: platform admin or client.

    Only the *hash* is stored — ``password_hash`` is produced by
    ``werkzeug.security.generate_password_hash`` and is never reversible. There
    is no column anywhere in this schema holding a plaintext password.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_CLIENT)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=STATUS_ACTIVE)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Username of the admin who created the account ("" for the bootstrap admin).
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    profile: Mapped["ClientProfile | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False)
    broker_accounts: Mapped[list["BrokerAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_active(self) -> bool:
        """Whether the account may log in at all.

        Named ``is_active`` to match the flag Flask-Login expects, though this
        app does its own session handling — see :mod:`app.auth`.
        """
        return self.status == STATUS_ACTIVE


class ClientProfile(Base):
    """Client-facing metadata that has nothing to do with trading.

    Separate from :class:`User` because it is optional and client-specific: an
    admin account has no subscription, and a row is only written when a client
    is created.
    """

    __tablename__ = "client_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         unique=True)
    subscription_status: Mapped[str] = mapped_column(String(16), nullable=False,
                                                     default=SUB_NONE)
    subscription_plan: Mapped[str] = mapped_column(String(64), nullable=False,
                                                   default="")
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                                    nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)

    user: Mapped["User"] = relationship(back_populates="profile")


class BrokerAccount(Base):
    """A client's broker account *link*.

    Deliberately holds no money. Balance, equity and margin live in
    :class:`BrokerAccountSnapshot` and are written **only** by a provider that
    actually read them from a broker (see :mod:`trading.broker_accounts`). Until
    such a provider is configured, a row here describes a link that is recorded
    but not connected, and the UI says exactly that rather than showing a zero.
    """

    __tablename__ = "broker_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="mt5")
    login: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    server: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)

    user: Mapped["User"] = relationship(back_populates="broker_accounts")
    snapshots: Mapped[list["BrokerAccountSnapshot"]] = relationship(
        back_populates="account", cascade="all, delete-orphan")


class BrokerAccountSnapshot(Base):
    """One reading of a client's broker account, with the moment it was taken.

    A snapshot rather than a live value, for the same reason the operator's own
    account is snapshotted (see ``runner.AccountState``): a figure whose age is
    not displayed cannot be told apart from a current one.
    """

    __tablename__ = "broker_account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id"), nullable=False, index=True)
    balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_free: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Which provider produced this reading, so a figure is always attributable.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    fetched_at_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                     default=_utcnow)

    account: Mapped["BrokerAccount"] = relationship(back_populates="snapshots")


class EngineState(Base):
    """The live scanner's own state, published so another process can read it.

    A singleton row (``id == 1``) that the process running the engine overwrites
    on every poll. It exists because the dashboard and the engine are
    deliberately separate processes (see ``deploy/install-service.ps1``: MT5's
    Python API binds a whole process to one terminal), and ``runner.LiveState``
    is in-memory only. Without this row the web process could never see a price,
    a setup state or even whether an engine was running — every one of those
    lived inside the scanner's address space.

    ``heartbeat_utc`` and ``lease_seconds`` together answer "is the engine
    alive?". The live loop legitimately sleeps — a poll interval while awake,
    up to ``SESSION_SLEEP_CAP_SECONDS`` while correctly idle outside a session —
    so a fixed freshness threshold would report the bot dead every time it did
    the right thing. The writer therefore records the wait budget it is about to
    sleep for, and a reader treats the engine as alive while
    ``now - heartbeat_utc <= lease_seconds``.

    Every other column mirrors a :class:`runner.LiveState` field, and is cleared
    or marked stopped when the session ends, so a finished session can never
    read as a live one.
    """

    __tablename__ = "engine_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: Random per-process token. Distinguishes "this engine" from "an engine
    #: in another process" when deciding whether a start would be a duplicate.
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")
    #: Whether the loop is *awake* (inside a tradeable window). A running but
    #: asleep engine is still alive — see the class docstring.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    activity: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    assets: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: asset -> {"buy": state, "sell": state}: the setup state machine.
    setups: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: asset -> last closed M1 close: the price the *strategy* last acted on.
    prices: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: asset -> ISO-8601 UTC close time of the candle ``prices`` came from, so a
    #: figure is never shown without its age.
    price_times: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: asset -> {"bid", "ask", "spread", "spread_points", "time_utc"}: the live
    #: quote read from the same terminal session the engine already holds.
    quotes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_candle_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_open_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signals_session: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    heartbeat_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                    default=_utcnow)
    #: Seconds the writer may legitimately sleep before its next heartbeat.
    lease_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
