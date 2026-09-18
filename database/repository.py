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

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from config import Settings, get_settings

from . import models as m
from .models import Base

# Re-export models for callers that prefer ``database.models``.
__all__ = ["Base", "models", "get_engine", "get_session", "init_db",
           "ensure_schema", "Repository"]

# How long SQLite waits for a competing writer before raising "database is
# locked". A live scanner thread writes signals while Flask request threads
# read, so contention is expected rather than exceptional.
_SQLITE_BUSY_TIMEOUT_MS = 10_000


def _make_engine(db_url: str):
    kwargs: dict = {"future": True}
    # SQLite needs check_same_thread=False for Flask's default threads.
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(db_url, **kwargs)

    if db_url.startswith("sqlite"):
        # WAL lets the scanner write while request threads read instead of
        # serialising on a global lock; the busy timeout turns the remaining
        # contention into a short wait rather than an immediate error.
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            finally:
                cursor.close()

    return engine


def get_engine(settings: Settings | None = None):
    cfg = settings or get_settings()
    return _make_engine(cfg.db_url)


# Columns added to a table *after* a database was first created. ``create_all``
# only ever CREATEs a missing table — it never ALTERs one that already exists —
# so an added column must be back-filled explicitly or every query touching that
# table fails with "no such column".
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (table, column, DDL type)
    ("backtests", "batch_id", "VARCHAR(36)"),
    # ICT model: purge grade, the SL anchor's own time, the take-profit target
    # (a different level from the purged one), the geometry/score, and the
    # deterministic setup identity. Additive only — existing rows read as the
    # declared defaults.
    ("signals", "purge_grade", "VARCHAR(16)"),
    ("signals", "structure_time_ny", "DATETIME"),
    ("signals", "target_kind", "VARCHAR(32)"),
    ("signals", "target_price", "FLOAT"),
    ("signals", "target_grade", "VARCHAR(16)"),
    ("signals", "risk_points", "FLOAT"),
    ("signals", "reward_points", "FLOAT"),
    ("signals", "efficiency_score", "FLOAT"),
    ("signals", "setup_id", "VARCHAR(64)"),
    ("signals", "state", "VARCHAR(32)"),
    ("signals", "digits", "INTEGER"),
)

# Indexes to (re)assert on every startup. Safe because ``IF NOT EXISTS`` is
# understood by both SQLite and PostgreSQL.
_ENSURED_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_backtests_batch_id", "backtests", "batch_id"),
)


def _ensure_schema(engine) -> None:
    """Apply additive migrations to an existing database (idempotent).

    Only ever adds: a missing column or a missing index. Never drops, renames or
    retypes anything, so running it against a populated database is safe.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, column, ddl_type in _ADDED_COLUMNS:
        if table not in existing_tables:
            continue  # create_all just made it, so the column is already there
        if column in {c["name"] for c in inspector.get_columns(table)}:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))

    for index, table, column in _ENSURED_INDEXES:
        if table not in existing_tables:
            continue
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})"))


def init_db(settings: Settings | None = None) -> None:
    """Create all tables and apply additive migrations (idempotent)."""
    ensure_schema(get_engine(settings))


def ensure_schema(engine) -> None:
    """Make a database match the models: create missing tables, add columns.

    The single entry point for schema setup, used by both :func:`init_db` (the
    CLI and the job runner) and the dashboard app. ``create_all`` alone is not
    enough for the dashboard: it only ever CREATEs a missing *table*, never
    ALTERs an existing one, so an app that started first against a database
    created by an older build would be missing every column added since — and
    the first signal insert would fail with "no such column".
    """
    Base.metadata.create_all(engine)
    _ensure_schema(engine)


def get_session(settings: Settings | None = None) -> Session:
    engine = get_engine(settings)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return maker()


# Columns the domain ``Signal`` fills, derived from the model itself so a new
# column needs no second edit here. ``_signal_values`` fails loudly if the
# dataclass cannot supply one, which is the whole point: a field added to
# ``Signal`` but never persisted used to be a silent data loss, not an error.
_SIGNAL_DB_ONLY = frozenset({"id", "created_at", "fingerprint"})
_SIGNAL_FIELDS: tuple[str, ...] = tuple(
    c.name for c in m.Signal.__table__.columns if c.name not in _SIGNAL_DB_ONLY
)

# The AI overlay is written twice (initial save, then after analysis), so its
# field list lives in one place too.
_AI_FIELDS: tuple[str, ...] = (
    "ai_status", "ai_score", "ai_decision", "ai_reasoning", "ai_confidence",
    "ai_strengths", "ai_risks",
)


def _signal_values(sig) -> dict:
    """Column values for a domain ``Signal``.

    ``fingerprint`` is a method on the dataclass and a column on the row, so it
    is computed rather than read. Every other field is copied straight across.
    """
    missing = [name for name in _SIGNAL_FIELDS if not hasattr(sig, name)]
    if missing:
        raise TypeError(
            f"{type(sig).__name__} has no field(s) {missing} required by the "
            "signals table — add them, or drop the column.")
    values = {name: getattr(sig, name) for name in _SIGNAL_FIELDS}
    values["fingerprint"] = sig.fingerprint()
    return values


def _batch_entry(batch_id: str, rows: list) -> dict:
    """Structural description of one backtest batch.

    Deliberately carries no scores: ranking lives in :mod:`backtesting.compare`
    so this module stays free of strategy/analysis logic.
    """
    ordered = sorted(rows, key=lambda r: r.id)
    starts = [r.start_utc for r in ordered if r.start_utc is not None]
    ends = [r.end_utc for r in ordered if r.end_utc is not None]
    created = [r.created_at for r in ordered if r.created_at is not None]
    return {
        "batch_id": batch_id,
        "rows": ordered,
        "n_assets": len(ordered),
        "assets": [r.asset for r in ordered],
        "created_at": max(created) if created else None,
        "start_utc": min(starts) if starts else None,
        "end_utc": max(ends) if ends else None,
    }


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
        row = m.Signal(**_signal_values(sig))
        self.session.add(row)
        self.session.commit()
        return row

    def update_signal_ai(self, signal_id: int, sig) -> m.Signal | None:
        """Persist a signal's AI overlay after analysis (deduped save first)."""
        row = self.session.get(m.Signal, signal_id)
        if row is None:
            return None
        for name in _AI_FIELDS:
            value = getattr(sig, name)
            # The JSON columns must hold a plain list, never None or a tuple.
            if name in ("ai_strengths", "ai_risks"):
                value = list(value or [])
            setattr(row, name, value)
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
                     direction: str | None = None, session: str | None = None,
                     date_from: datetime | None = None,
                     date_to: datetime | None = None,
                     limit: int = 500) -> list[m.Signal]:
        """Filtered listing (newest first) for the dashboard signals page.

        Every filter past ``status``/``asset`` is optional and defaulted, so the
        original two-argument call sites (the admin signals page, the tests)
        behave exactly as before.

        ``session`` matches ``session_primary`` — the single session the setup is
        *filed under*, which is what the UI displays and lets a reader filter by.
        ``session_keys`` is the full overlap list and would match several rows
        per setup, so it is deliberately not what this filters on.

        ``date_from``/``date_to`` are naive-UTC instants compared against
        ``entry_time_utc`` (inclusive at both ends). Conversion from a NY date
        picker happens in the caller, through ``trading.time_utils``.
        """
        stmt = select(m.Signal)
        if status:
            stmt = stmt.where(m.Signal.status == status)
        if asset:
            stmt = stmt.where(m.Signal.asset == asset)
        if direction:
            stmt = stmt.where(m.Signal.direction == direction)
        if session:
            stmt = stmt.where(m.Signal.session_primary == session)
        if date_from is not None:
            stmt = stmt.where(m.Signal.entry_time_utc >= date_from)
        if date_to is not None:
            stmt = stmt.where(m.Signal.entry_time_utc <= date_to)
        stmt = stmt.order_by(m.Signal.id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def distinct_sessions(self) -> list[str]:
        """Session keys actually present in the signal history, for a filter list.

        Read from the data rather than from ``trading.sessions`` so the filter
        offers only values that can return rows — a dropdown listing sessions
        that produce nothing reads as a broken filter.
        """
        rows = self.session.execute(
            select(m.Signal.session_primary).where(m.Signal.session_primary != "")
            .distinct().order_by(m.Signal.session_primary)
        ).scalars().all()
        return [r for r in rows if r]

    def active_setups(self, limit: int = 50) -> list[m.Signal]:
        """Setups the engine has confirmed and that are still the latest word.

        "Active" means APPROVED — the deterministic rules and the risk manager
        both passed, so it is a setup the platform is standing behind. Pending
        and rejected rows are history, not active setups.
        """
        return self.find_signals(status="APPROVED", limit=limit)

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

    def signals_after_id(self, after_id: int, limit: int = 50) -> list[m.Signal]:
        """Signals with a higher id than ``after_id`` (dashboard live feed)."""
        rows = self.session.execute(
            select(m.Signal).where(m.Signal.id > after_id)
            .order_by(m.Signal.id.asc()).limit(limit)
        ).scalars().all()
        return list(rows)

    def max_signal_id(self) -> int:
        """Highest signal id, or 0 when the table is empty."""
        return int(self.session.execute(
            select(func.max(m.Signal.id))).scalar() or 0)

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
                      trades: list[dict],
                      batch_id: str | None = None) -> m.Backtest:
        bt = m.Backtest(
            name=name, asset=asset, symbol=symbol, batch_id=batch_id,
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

    def backtest_batch(self, batch_id: str) -> list[m.Backtest]:
        """Every per-asset row produced by one batch request, in run order."""
        rows = self.session.execute(
            select(m.Backtest).where(m.Backtest.batch_id == batch_id)
            .order_by(m.Backtest.id.asc())
        ).scalars().all()
        return list(rows)

    def recent_batches(self, limit: int = 20, scan: int = 500) -> list[dict]:
        """Backtest campaigns, newest first.

        Returns structural rows only — ``batch_id``, the rows, the window — and
        leaves ranking to :mod:`backtesting.compare`, so this module stays a
        persistence facade with no scoring logic.

        A row with ``batch_id IS NULL`` predates batching (or came from a
        single-asset run on an older build). Each one is surfaced as its own
        one-asset batch rather than dropped, so no history disappears.
        """
        rows = self.session.execute(
            select(m.Backtest).order_by(m.Backtest.id.desc()).limit(scan)
        ).scalars().all()

        batches: list[dict] = []
        grouped: dict[str, dict] = {}
        for row in rows:
            if row.batch_id is None:
                batches.append(_batch_entry(f"single:{row.id}", [row]))
                continue
            entry = grouped.get(row.batch_id)
            if entry is None:
                entry = {"batch_id": row.batch_id, "rows": []}
                grouped[row.batch_id] = entry
                batches.append(entry)
            entry["rows"].append(row)

        out = [_batch_entry(b["batch_id"], b["rows"]) for b in batches]
        return out[:limit]

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

    def recent_events_by_source(self, source: str, limit: int = 100,
                                sources: tuple[str, ...] | None = None
                                ) -> list[m.SystemEvent]:
        """Newest events from one source, or any of several.

        Filtering in SQL rather than in the caller because the event log is the
        busiest table in the schema — every poll writes session lines, so a
        client asking for just the strategy's decision lines would otherwise
        pull hundreds of unrelated rows across the wire to discard them.

        ``sources`` wins when supplied; ``source`` is the single-source shorthand.
        """
        wanted = list(sources) if sources else ([source] if source else [])
        stmt = select(m.SystemEvent)
        if wanted:
            stmt = stmt.where(m.SystemEvent.source.in_(wanted))
        rows = self.session.execute(
            stmt.order_by(m.SystemEvent.id.desc()).limit(limit)
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

    # ------------------------------------------------------------------ #
    # Platform accounts (admin / client)
    #
    # The repository stays the only thing that touches the ORM; the auth layer
    # never builds a query of its own. Password hashing happens in
    # ``app.auth`` — these methods receive an already-hashed value, so no
    # plaintext password is ever passed into this module.
    # ------------------------------------------------------------------ #
    def create_user(self, *, username: str, password_hash_or_plain: str,
                    role: str = m.ROLE_CLIENT, email: str = "",
                    display_name: str = "", created_by: str = "",
                    notes: str = "", subscribe: bool = False) -> m.User:
        """Insert a user. ``password_hash_or_plain`` is hashed here if it is not.

        Accepting either is a convenience for the CLI and tests, and it is
        unambiguous because a Werkzeug hash always carries a recognisable
        method prefix — a raw password with that prefix would have to be
        deliberately crafted, and would simply be hashed again anyway.
        """
        pw = password_hash_or_plain
        if not pw.startswith(("pbkdf2:", "scrypt:", "argon2")):
            from app.auth import hash_password

            pw = hash_password(pw)

        row = m.User(username=username.strip(), email=(email or "").strip(),
                     password_hash=pw, role=role, status=m.STATUS_ACTIVE,
                     display_name=(display_name or "").strip(),
                     notes=(notes or "").strip(), created_by=created_by)
        self.session.add(row)
        if role == m.ROLE_CLIENT and subscribe:
            self.session.flush()   # assign row.id before the profile references it
            self.session.add(m.ClientProfile(user_id=row.id))
        self.session.commit()
        return row

    def get_user(self, user_id: int) -> m.User | None:
        return self.session.get(m.User, user_id)

    def get_user_by_username(self, username: str) -> m.User | None:
        """Case-insensitive lookup, so ``Admin`` and ``admin`` are one account."""
        if not username:
            return None
        return self.session.execute(
            select(m.User).where(func.lower(m.User.username) == username.strip().lower())
        ).scalars().first()

    def list_users(self, *, role: str | None = None) -> list[m.User]:
        stmt = select(m.User)
        if role:
            stmt = stmt.where(m.User.role == role)
        stmt = stmt.order_by(m.User.role.desc(), m.User.username)
        return list(self.session.execute(stmt).scalars().all())

    def list_clients(self) -> list[m.User]:
        return self.list_users(role=m.ROLE_CLIENT)

    def count_users(self) -> int:
        return int(self.session.execute(
            select(func.count()).select_from(m.User)).scalar_one())

    def set_user_password(self, user_id: int, password: str) -> bool:
        """Replace a user's password hash. Returns False if the user is gone."""
        from app.auth import hash_password

        row = self.session.get(m.User, user_id)
        if row is None:
            return False
        row.password_hash = hash_password(password)
        self.session.commit()
        return True

    def set_user_status(self, user_id: int, status: str) -> m.User | None:
        """Activate or suspend an account (takes effect on the next request)."""
        row = self.session.get(m.User, user_id)
        if row is None:
            return None
        row.status = status
        self.session.commit()
        return row

    def touch_user_login(self, user_id: int, when: datetime | None = None) -> None:
        from trading import time_utils as tu

        row = self.session.get(m.User, user_id)
        if row is None:
            return
        # The project-wide naive-UTC convention, not ``datetime.utcnow()`` — the
        # dashboard renders every stored instant through ``time_utils.utc_to_ny``.
        row.last_login_at = when or tu.now_utc()
        self.session.commit()

    def update_client_profile(self, user_id: int, **fields) -> m.ClientProfile | None:
        """Set subscription fields on a client's profile, creating it if needed.

        Only the known subscription columns are writable; an unexpected key is
        ignored rather than raising, so a form field added in the template can
        never break the save.
        """
        allowed = {"subscription_status", "subscription_plan",
                   "subscription_expires_at"}
        row = self.session.execute(
            select(m.ClientProfile).where(m.ClientProfile.user_id == user_id)
        ).scalars().first()
        if row is None:
            row = m.ClientProfile(user_id=user_id)
            self.session.add(row)
        for key, value in fields.items():
            if key in allowed:
                setattr(row, key, value)
        self.session.commit()
        return row

    def client_profile(self, user_id: int) -> m.ClientProfile | None:
        return self.session.execute(
            select(m.ClientProfile).where(m.ClientProfile.user_id == user_id)
        ).scalars().first()

    # ------------------------------------------------------------------ #
    # Client broker-account links
    # ------------------------------------------------------------------ #
    def add_broker_account(self, *, user_id: int, login: str, server: str = "",
                           provider: str = "mt5", label: str = "") -> m.BrokerAccount:
        row = m.BrokerAccount(user_id=user_id, provider=provider,
                              login=(login or "").strip(),
                              server=(server or "").strip(),
                              label=(label or "").strip())
        self.session.add(row)
        self.session.commit()
        return row

    def list_broker_accounts(self, user_id: int | None = None) -> list[m.BrokerAccount]:
        stmt = select(m.BrokerAccount)
        if user_id is not None:
            stmt = stmt.where(m.BrokerAccount.user_id == user_id)
        return list(self.session.execute(
            stmt.order_by(m.BrokerAccount.id)).scalars().all())

    def remove_broker_account(self, account_id: int) -> bool:
        row = self.session.get(m.BrokerAccount, account_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.commit()
        return True

    def latest_snapshot(self, broker_account_id: int) -> m.BrokerAccountSnapshot | None:
        """The most recent reading for a link, or ``None`` if it was never read.

        ``None`` is the honest answer for a link with no provider behind it, and
        is why the admin UI renders "not connected" rather than a zero.
        """
        return self.session.execute(
            select(m.BrokerAccountSnapshot)
            .where(m.BrokerAccountSnapshot.broker_account_id == broker_account_id)
            .order_by(m.BrokerAccountSnapshot.fetched_at_utc.desc(),
                      m.BrokerAccountSnapshot.id.desc())
        ).scalars().first()

    def save_broker_snapshot(self, broker_account_id: int, *, balance=None,
                             equity=None, margin_free=None, currency=None,
                             source: str = "") -> m.BrokerAccountSnapshot:
        row = m.BrokerAccountSnapshot(
            broker_account_id=broker_account_id, balance=balance, equity=equity,
            margin_free=margin_free, currency=currency, source=source)
        self.session.add(row)
        self.session.commit()
        return row


def iter_session(settings: Settings | None = None) -> Iterator[Session]:
    """Context-managed session for scripts/CLI."""
    s = get_session(settings)
    try:
        yield s
    finally:
        s.close()
