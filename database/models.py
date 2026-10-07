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
    # Which model produced this signal — MODEL_1 (the ICT sequence) or MODEL_2
    # (the session-liquidity purge model). Defaults to MODEL_1 so every row that
    # already exists keeps its meaning when the column is added.
    # ``server_default`` as well as ``default``: the Python-side default only
    # applies to ORM inserts, so a database built by ``create_all`` would carry
    # ``NOT NULL`` with no SQL default at all and reject any insert that does not
    # name the column — which is what a legacy row or a hand-written INSERT does.
    # Emitting it in the DDL also makes a fresh database identical to one the
    # additive migration upgraded, which is the property the migration promises.
    model: Mapped[str] = mapped_column(String(16), nullable=False,
                                       default="MODEL_1", server_default="MODEL_1",
                                       index=True)
    # The producing model's own provenance payload, as a JSON string. Model 2
    # records the purged session level, its source session and date, and the
    # purge candle's extremes there. Text rather than the JSON type so SQLite and
    # PostgreSQL store it identically and the additive ALTER TABLE needs no
    # dialect-specific type.
    model_meta: Mapped[str] = mapped_column(Text, nullable=False, default="{}",
                                            server_default="{}")
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
#:
#: ``ROLE_USER`` is an *alias*, not a fourth role: the 3rader sign-up flow talks
#: about "users", but the column has stored ``"client"`` since the platform
#: began and every existing row, query and test uses that spelling. Keeping one
#: stored value under two readable names avoids a data migration that would buy
#: nothing.
ROLE_ADMIN = "admin"
ROLE_CLIENT = "client"
ROLE_USER = ROLE_CLIENT
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
    #: Profile fields a 3rader account holder maintains themselves. Empty string
    #: rather than NULL throughout, matching ``email``/``display_name`` above, so
    #: templates never have to distinguish "absent" from "blank".
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    country: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Filename only — never a path, and never a client-supplied one. The file
    #: lives in the avatar directory (see ``app.profile``) and is served through
    #: an authenticated route, so a crafted value cannot escape that directory.
    avatar_path: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: Username of the admin who created the account ("" for the bootstrap admin).
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: When the password last changed. The signed-in session records this value,
    #: and the loader drops any session whose recorded copy no longer matches —
    #: so a password reset (or an admin resetting it) ends every other session
    #: instead of leaving an attacker's cookie working after the victim has
    #: changed their credentials. NULL for accounts that predate the column;
    #: NULL compares equal to a session that recorded nothing, so existing
    #: sessions are unaffected.
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                                 nullable=True)
    #: Soft deletion. An admin "deleting" a user sets this instead of removing the
    #: row, so the payments and subscriptions the account accrued — which are
    #: financial records — are never orphaned or destroyed. Every user lookup that
    #: authenticates filters on it (see ``Repository.get_user``).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    profile: Mapped["ClientProfile | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False)
    broker_accounts: Mapped[list["BrokerAccount"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    #: Payment history is *not* cascade-deleted with the account: these are
    #: financial records, and an admin removing a user soft-deletes the row
    #: (``deleted_at``) precisely so this history survives. The relationship is
    #: therefore read-only and carries no delete-orphan.
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="user", viewonly=True)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def full_name(self) -> str:
        """The name to show a person, falling back to the login name.

        ``display_name`` is optional at creation (the admin form has always
        allowed it to be blank), so anything rendering a person must cope with
        it being empty rather than printing an empty cell.
        """
        return self.display_name or self.username

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


# --------------------------------------------------------------------------- #
# 3rader: plans, subscriptions, payments and notifications
#
# Purely additive, like the block above: ``create_all`` builds these five tables
# on the next start and an existing production database simply gains them empty.
# The only change to an existing table is five new ``users`` columns, back-filled
# by ``_ADDED_COLUMNS`` in ``repository``.
#
# Money is stored in **integer minor units** (cents for USD) and never as a
# float. A payment amount is compared against the plan price to authorise a
# subscription, and binary floating point cannot represent 0.1 exactly — a
# comparison that is off by one ulp is the difference between honouring a
# payment and rejecting a real customer.
# --------------------------------------------------------------------------- #
#: Payment lifecycle. ``SUCCESSFUL`` is only ever written by the server after a
#: provider verification — never by a browser.
PAY_PENDING = "pending"
PAY_SUCCESSFUL = "successful"
PAY_FAILED = "failed"
PAY_CANCELLED = "cancelled"
PAYMENT_STATES = (PAY_PENDING, PAY_SUCCESSFUL, PAY_FAILED, PAY_CANCELLED)

#: Subscription lifecycle, distinct from the coarse ``SUB_*`` states the admin
#: list has always shown on ``ClientProfile``. ``pending`` means a payment was
#: started and not yet confirmed; the subscription is inactive until it clears.
SUB_PENDING = "pending"
SUB_CANCELLED = "cancelled"

#: Notification kinds. Stored, not inferred, so the bell can group and the
#: email layer can pick a template by name.
NOTIFY_REGISTRATION = "registration"
NOTIFY_PAYMENT_SUCCESS = "payment_successful"
NOTIFY_PAYMENT_FAILED = "payment_failed"
NOTIFY_SUBSCRIPTION_ACTIVATED = "subscription_activated"
NOTIFY_SUBSCRIPTION_CHANGED = "subscription_changed"
NOTIFICATION_KINDS = (
    NOTIFY_REGISTRATION, NOTIFY_PAYMENT_SUCCESS, NOTIFY_PAYMENT_FAILED,
    NOTIFY_SUBSCRIPTION_ACTIVATED, NOTIFY_SUBSCRIPTION_CHANGED,
)


class Plan(Base):
    """A purchasable subscription tier.

    Seeded from :mod:`app.plans`, which is the canonical definition, and then
    *owned by the database*: ``sync_plans`` fills in only keys it has never seen
    and never overwrites a row, so an operator who edits a price later is not
    reverted on the next restart. Nothing outside :mod:`app.plans` and the admin
    editor should read this table directly — routes ask the access layer.

    ``level`` is the only field access decisions use. It orders the tiers, so
    "at least Premium" is ``plan.level >= plans.get("premium").level`` rather
    than a chain of string comparisons that would need editing whenever a tier
    is inserted between two others.
    """

    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("key", name="uq_plans_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Stable identifier used in URLs, access checks and payment metadata:
    #: ``basic`` / ``premium`` / ``vip``. Never shown to a customer.
    key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Minor units (cents). See the module note above on why this is an integer.
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    features: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Ordering rank for "at least this tier" checks. Not a price comparison.
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Whether the plan may be bought right now. A retired plan keeps its
    #: subscriptions and its history; it just stops appearing on /pricing.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Marks the tier the pricing page presents as the default choice.
    highlight: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow, onupdate=_utcnow)


class Payment(Base):
    """One attempt to buy a subscription, and its outcome.

    The row is created *before* the customer leaves for the provider, in
    ``pending``, so the return leg and the webhook both have something to
    reconcile against. Only a server-side provider verification moves it to
    ``successful``.

    Idempotency rests on two unique constraints rather than on application
    logic: ``reference`` is ours and is presented to the provider, and
    ``provider_tx_id`` is theirs. A webhook delivered twice — which providers
    do, deliberately — collides on the second write and is recognised as a
    replay instead of activating a second subscription.
    """

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_payments_reference"),
        UniqueConstraint("provider_tx_id", name="uq_payments_provider_tx_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"),
                                                nullable=True, index=True)
    #: Which plan was being bought, kept as the key as well as the FK so a
    #: payment's intent survives even if the plan row is later retired.
    plan_key: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: Merchant reference we generate and hand to the provider.
    reference: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False,
                                          default="flutterwave")
    #: The provider's own transaction id, written only after verification.
    provider_tx_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=PAY_PENDING, index=True)
    #: True only when the server has confirmed the charge with the provider.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The provider's verification payload, kept for reconciliation and disputes.
    #: Deliberately the *response body* — no card data and no secret key reaches
    #: this column, and nothing here is rendered to a customer.
    verification_json: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                    default=dict)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checkout_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow, onupdate=_utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="payments")
    plan: Mapped["Plan | None"] = relationship()


class Subscription(Base):
    """A user's entitlement to a plan over a period.

    Rows accumulate: an upgrade writes a new subscription and expires the old
    one rather than mutating it, so "what was this account entitled to in
    March?" stays answerable. The single *current* row is the one whose status
    is ``active`` and whose ``expires_at`` has not passed; :func:`Repository
    .active_subscription` is the only place that rule is implemented.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), nullable=False,
                                         index=True)
    plan_key: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=SUB_PENDING, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: The payment that bought this period. Unique so one payment can never
    #: activate two subscriptions, however many times a webhook arrives.
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"),
                                                   nullable=True, unique=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship()
    plan: Mapped["Plan"] = relationship()


class Notification(Base):
    """An in-app notification for one user.

    Stored rather than derived so it can be marked read and so a delivery that
    happened by email is still visible in the app. Email is sent through
    :mod:`notifications.email`; this row is the in-app half of the same event.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Optional in-app destination, e.g. ``/subscription``. A path we wrote, not
    #: user input, so it cannot become an open redirect.
    link: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow, index=True)

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class PasswordReset(Base):
    """A single-use, expiring password-reset grant.

    Only the SHA-256 of the token is stored, so a leaked database snapshot does
    not hand over working reset links — the same reason only password *hashes*
    are kept. ``used_at`` makes it single-use: the row is claimed with a
    conditional UPDATE, so two simultaneous submissions of the same link cannot
    both succeed.
    """

    __tablename__ = "password_resets"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_password_resets_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False,
                                         index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)
