"""Database engine, sessions and repository functions.

The repository is deliberately thin: it persists domain objects (Signal,
ExecutionResult, backtest output) to the schema in :mod:`database.models` and
queries them back for the dashboard. It contains **no strategy logic** and
**no MT5/AI/telegram code**, so it can be tested with an in-memory SQLite DB.

``get_engine`` / ``get_session`` build on the settings ``db_url`` (SQLite by
default; setting ``DATABASE_URL`` migrates to PostgreSQL with no code change).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator

from sqlalchemy import (create_engine, delete, event, func, inspect, or_,
                        select, text, true, update)
from sqlalchemy.orm import Session, sessionmaker

from config import Settings, get_settings
from trading import time_utils as tu

from . import models as m
from .models import Base


def _utcnow() -> datetime:
    """Naive UTC, the project-wide convention (see ``database.models``)."""
    return tu.now_utc()


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
    # 3rader account fields. Added to an existing ``users`` table by ALTER, so
    # each carries a default an existing row can be given without a rewrite.
    # ``updated_at``/``deleted_at`` are nullable for the same reason — NULL is
    # the honest value for a row written before the column existed, and every
    # reader treats NULL there as "never updated" / "not deleted".
    ("users", "phone", "VARCHAR(32) NOT NULL DEFAULT ''"),
    ("users", "country", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("users", "avatar_path", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("users", "updated_at", "DATETIME"),
    ("users", "deleted_at", "DATETIME"),
    ("users", "password_changed_at", "DATETIME"),
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

    def __init__(self, settings: Settings | None = None, session: Session | None = None,
                 engine=None):
        """``engine`` shares one pool while keeping the session *owned*.

        The distinction matters because :meth:`close` only closes a session this
        object created. A caller that passes ``session=`` is saying it manages
        that session's lifetime, so ``close`` deliberately leaves it alone —
        which is right for a request-scoped session dropped at teardown, and
        wrong for a short-lived read, where the connection would be held until
        the garbage collector happened to run. A bounded pool (SQLite's default
        is 15 connections) then runs dry and every later request waits out the
        full 30-second pool timeout.

        ``engine`` is for that second case: build me a session from this shared
        engine and I will close it, returning the connection to the pool the
        moment the read is done.
        """
        self.settings = settings or get_settings()
        self._own_session = session is None
        if session is not None:
            self.session = session
        elif engine is not None:
            self.session = sessionmaker(bind=engine, expire_on_commit=False,
                                        future=True)()
        else:
            self.session = get_session(settings)

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

    def get_user(self, user_id: int, *, include_deleted: bool = False) -> m.User | None:
        """One user by primary key, or ``None``.

        A soft-deleted account is reported as absent by default. That is what
        makes deletion take effect: the session loader calls this on every
        request, so a deleted user's open session stops resolving immediately
        rather than lasting until its cookie expires. Admin screens that need to
        show a removed account pass ``include_deleted=True``.
        """
        row = self.session.get(m.User, user_id)
        if row is None:
            return None
        if row.deleted_at is not None and not include_deleted:
            return None
        return row

    def get_user_by_username(self, username: str, *,
                             include_deleted: bool = False) -> m.User | None:
        """Case-insensitive lookup, so ``Admin`` and ``admin`` are one account."""
        if not username:
            return None
        return self.session.execute(
            select(m.User)
            .where(func.lower(m.User.username) == username.strip().lower())
            .where(m.User.deleted_at.is_(None) if not include_deleted
                   else true())
        ).scalars().first()

    def get_user_by_email(self, email: str, *,
                          include_deleted: bool = False) -> m.User | None:
        """Case-insensitive lookup by email address.

        Email is *not* uniquely constrained in the schema — the column predates
        account self-registration and older rows may share an empty string — so
        this returns the first match and registration checks for an existing one
        before inserting. Making the column unique would need a table rebuild on
        SQLite, which is a migration this feature does not justify.
        """
        if not email:
            return None
        stmt = select(m.User).where(
            func.lower(m.User.email) == email.strip().lower())
        if not include_deleted:
            stmt = stmt.where(m.User.deleted_at.is_(None))
        return self.session.execute(stmt.order_by(m.User.id)).scalars().first()

    def login_identifier_exists(self, identifier: str) -> bool:
        """Is this username or email already taken?

        Both are checked because the sign-in form accepts either, so treating
        them as separate namespaces would let one account's email shadow
        another's username and make sign-in ambiguous.
        """
        if not identifier:
            return False
        return (self.get_user_by_username(identifier) is not None
                or self.get_user_by_email(identifier) is not None)

    def list_users(self, *, role: str | None = None,
                   include_deleted: bool = False) -> list[m.User]:
        stmt = select(m.User)
        if role:
            stmt = stmt.where(m.User.role == role)
        if not include_deleted:
            stmt = stmt.where(m.User.deleted_at.is_(None))
        stmt = stmt.order_by(m.User.role.desc(), m.User.username)
        return list(self.session.execute(stmt).scalars().all())

    def list_clients(self) -> list[m.User]:
        return self.list_users(role=m.ROLE_CLIENT)

    def search_users(self, *, query: str = "", role: str | None = None,
                     plan: str | None = None, status: str | None = None,
                     include_deleted: bool = False, only_deleted: bool = False,
                     limit: int = 500) -> list[m.User]:
        """Admin user list: free-text search plus the three facet filters.

        ``query`` matches name, username or email, case-insensitively. ``plan``
        filters on the subscription, which lives on ``client_profiles``
        (admin-granted) or ``subscriptions`` (bought) — both are consulted so the
        list agrees with what the access layer will decide for that account,
        rather than showing a plan the account cannot use.

        ``status`` filters the *account*, not the subscription: it is the same
        value the table's Status column renders and the same values the filter
        dropdown offers (``active`` / ``suspended``). Filtering the subscription
        status here instead would make ``suspended`` match nothing at all, since
        it is not a subscription state, and ``active`` match only accounts with a
        bought subscription — a filter that silently answers a different
        question than the one on its label.

        ``include_deleted`` keeps removed accounts in the result;
        ``only_deleted`` returns *just* the removed ones, which is what the
        "Removed only" control asks for.

        The whole thing is one query with an outer join per subscription source,
        so filtering does not become a query per user.
        """
        # Newest active subscription per user, as a correlated scalar subquery.
        active_sub = (
            select(m.Subscription.plan_key)
            .where(m.Subscription.user_id == m.User.id)
            .where(m.Subscription.status == m.SUB_ACTIVE)
            .where(or_(m.Subscription.expires_at.is_(None),
                       m.Subscription.expires_at > _utcnow()))
            .order_by(m.Subscription.id.desc())
            .limit(1)
            .correlate(m.User)
            .scalar_subquery()
        )
        effective_plan = func.coalesce(
            active_sub, func.nullif(m.ClientProfile.subscription_plan, ""))

        stmt = (select(m.User)
                .outerjoin(m.ClientProfile, m.ClientProfile.user_id == m.User.id)
                .order_by(m.User.created_at.desc(), m.User.id.desc())
                .limit(max(1, min(limit, 1000))))

        if only_deleted:
            stmt = stmt.where(m.User.deleted_at.is_not(None))
        elif not include_deleted:
            stmt = stmt.where(m.User.deleted_at.is_(None))
        if role:
            stmt = stmt.where(m.User.role == role)
        if plan:
            stmt = stmt.where(effective_plan == plan)
        if status:
            stmt = stmt.where(m.User.status == status)
        if query and query.strip():
            needle = f"%{query.strip().lower()}%"
            stmt = stmt.where(or_(
                func.lower(m.User.username).like(needle),
                func.lower(m.User.email).like(needle),
                func.lower(m.User.display_name).like(needle),
            ))

        return list(self.session.execute(stmt).scalars().unique().all())

    def count_users(self, *, include_deleted: bool = True) -> int:
        """Total accounts.

        Counts *all* rows by default, including soft-deleted ones, because the
        one caller that matters — :func:`app.auth.bootstrap_admin` — uses this to
        answer "is this a fresh install?". Counting only live accounts would let
        a database whose every user was removed look brand new and have an admin
        re-created from the environment on top of live data.
        """
        stmt = select(func.count()).select_from(m.User)
        if not include_deleted:
            stmt = stmt.where(m.User.deleted_at.is_(None))
        return int(self.session.execute(stmt).scalar_one())

    def set_user_password(self, user_id: int, password: str,
                          *, end_sessions: bool = True) -> bool:
        """Replace a user's password hash. Returns False if the user is gone.

        ``end_sessions`` stamps ``password_changed_at``, which the session loader
        compares against — so by default this also signs the account out
        everywhere. That is the point: a password is usually changed *because*
        the old one is no longer trusted, and leaving other sessions alive would
        make the change cosmetic. The one caller that legitimately wants the
        sessions kept (an admin setting a password for someone who has never
        signed in) can pass ``False``.
        """
        from app.auth import hash_password

        row = self.session.get(m.User, user_id)
        if row is None:
            return False
        row.password_hash = hash_password(password)
        row.updated_at = _utcnow()
        if end_sessions:
            row.password_changed_at = row.updated_at
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

    # ------------------------------------------------------------------ #
    # Live engine state (the scanner process -> dashboard bridge)
    # ------------------------------------------------------------------ #
    def save_engine_state(self, **fields) -> m.EngineState:
        """Publish the running engine's state to the singleton row.

        Written by the scanner process on every poll, read by the web process
        through :meth:`load_engine_state`. A singleton rather than a row per
        session because this answers "what is the engine doing *now*" — the
        history of what it did lives in ``signals`` and ``system_events``, which
        are already append-only and already cross the process boundary.

        Committing per poll is deliberate: the row is what makes the dashboard
        live, so a value sitting uncommitted in a session is a value the
        dashboard cannot see.
        """
        row = self.session.get(m.EngineState, 1)
        if row is None:
            row = m.EngineState(id=1)
            self.session.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        self.session.commit()
        return row

    def load_engine_state(self) -> m.EngineState | None:
        """The last published engine state, or ``None`` if none was ever written.

        ``None`` is the honest answer for a database the scanner has never run
        against, and is why the dashboard distinguishes "no engine" from "an
        engine with nothing to report" instead of rendering both as one thing.
        """
        return self.session.get(m.EngineState, 1)

    # ------------------------------------------------------------------ #
    # Plans
    #
    # Seeded from ``app.plans`` (the canonical definition) and then owned by
    # the database, so an operator's edit is never reverted by a restart.
    # ------------------------------------------------------------------ #
    def sync_plans(self, rows: list[dict]) -> int:
        """Insert any plan key that has never been stored. Returns how many.

        **Never updates an existing row.** That is the whole contract: a plan
        whose price an operator changed must survive the next deployment, and a
        seed that overwrote would silently undo it. Adding a *new* tier to
        ``app.plans`` does land here, which is how a fourth plan would ship.
        """
        existing = {key for (key,) in self.session.execute(
            select(m.Plan.key)).all()}
        added = 0
        for row in rows:
            if row["key"] in existing:
                continue
            self.session.add(m.Plan(**row))
            added += 1
        if added:
            self.session.commit()
        return added

    def list_plans(self, *, active_only: bool = False) -> list[m.Plan]:
        stmt = select(m.Plan)
        if active_only:
            stmt = stmt.where(m.Plan.is_active.is_(True))
        return list(self.session.execute(
            stmt.order_by(m.Plan.sort_order, m.Plan.level)).scalars().all())

    def get_plan_by_key(self, key: str) -> m.Plan | None:
        if not key:
            return None
        return self.session.execute(
            select(m.Plan).where(m.Plan.key == str(key).strip().lower())
        ).scalars().first()

    def set_plan_fields(self, plan_key: str, **fields) -> m.Plan | None:
        """Edit a stored plan. Only the operator-editable columns are writable.

        ``key`` and ``level`` are deliberately excluded: the key is referenced by
        subscriptions and payment metadata, and the level orders every access
        decision. Changing either from a free-text form would silently re-rank
        what customers can reach.

        The identifier is named ``plan_key`` rather than ``key`` so a caller can
        safely spread a form dict into ``**fields`` — with a parameter called
        ``key``, a form field of that name would raise ``TypeError`` instead of
        being ignored by the allow-list below.
        """
        allowed = {"name", "price_minor", "currency", "description", "features",
                   "is_active", "highlight", "sort_order"}
        row = self.get_plan_by_key(plan_key)
        if row is None:
            return None
        for field, value in fields.items():
            if field in allowed:
                setattr(row, field, value)
        row.updated_at = _utcnow()
        self.session.commit()
        return row

    # ------------------------------------------------------------------ #
    # Subscriptions
    # ------------------------------------------------------------------ #
    def active_subscription(self, user_id: int) -> m.Subscription | None:
        """The user's live subscription, or ``None``.

        The single implementation of "what is this account entitled to buy". A
        row counts as live only while its status is ``active`` **and** its end
        date has not passed, so an elapsed subscription stops granting access
        the moment it lapses rather than at the next cleanup job.
        """
        return self.session.execute(
            select(m.Subscription)
            .where(m.Subscription.user_id == user_id)
            .where(m.Subscription.status == m.SUB_ACTIVE)
            .where(or_(m.Subscription.expires_at.is_(None),
                       m.Subscription.expires_at > _utcnow()))
            .order_by(m.Subscription.id.desc())
        ).scalars().first()

    def subscription_history(self, user_id: int) -> list[m.Subscription]:
        return list(self.session.execute(
            select(m.Subscription)
            .where(m.Subscription.user_id == user_id)
            .order_by(m.Subscription.id.desc())
        ).scalars().all())

    def list_subscriptions(self, *, status: str | None = None,
                           limit: int = 500) -> list[m.Subscription]:
        stmt = select(m.Subscription)
        if status:
            stmt = stmt.where(m.Subscription.status == status)
        return list(self.session.execute(
            stmt.order_by(m.Subscription.id.desc())
            .limit(max(1, min(limit, 2000)))).scalars().all())

    def activate_subscription(self, *, user_id: int, plan, payment: m.Payment,
                              period_days: int) -> m.Subscription | None:
        """Grant ``plan`` to ``user_id`` for one period, exactly once per payment.

        Idempotent on ``payment_id``: the column carries a UNIQUE constraint, so
        a second call for the same payment — a replayed webhook, or the browser
        return leg racing the webhook — collides instead of granting a second
        period. The existing row is returned rather than an error, because from
        the caller's point of view "already granted" and "just granted" are the
        same successful outcome.

        Any subscription the user already holds is expired first, so an upgrade
        never leaves two live entitlements and the access layer has one
        unambiguous answer.
        """
        existing = self.session.execute(
            select(m.Subscription)
            .where(m.Subscription.payment_id == payment.id)
        ).scalars().first()
        if existing is not None:
            return existing

        now = _utcnow()
        for old in self.session.execute(
                select(m.Subscription)
                .where(m.Subscription.user_id == user_id)
                .where(m.Subscription.status == m.SUB_ACTIVE)).scalars().all():
            old.status = m.SUB_EXPIRED
            old.updated_at = now

        row = m.Subscription(
            user_id=user_id, plan_id=plan.id, plan_key=plan.key,
            status=m.SUB_ACTIVE, started_at=now,
            expires_at=now + timedelta(days=max(1, int(period_days))),
            payment_id=payment.id)
        self.session.add(row)
        self.session.commit()
        return row

    def cancel_subscription(self, subscription_id: int) -> m.Subscription | None:
        """Stop a subscription renewing/continuing. History is untouched."""
        row = self.session.get(m.Subscription, subscription_id)
        if row is None:
            return None
        row.status = m.SUB_CANCELLED
        row.cancelled_at = _utcnow()
        row.updated_at = row.cancelled_at
        self.session.commit()
        return row

    # ------------------------------------------------------------------ #
    # Payments
    # ------------------------------------------------------------------ #
    def create_payment(self, *, user_id: int, plan, reference: str,
                       amount_minor: int, currency: str,
                       provider: str = "flutterwave",
                       checkout_url: str = "") -> m.Payment:
        """Record an attempt *before* the customer leaves for the provider.

        Written up front so the return leg and the webhook both have a row to
        reconcile against, and so an abandoned checkout is visible to the admin
        as a pending payment rather than vanishing.
        """
        row = m.Payment(user_id=user_id, plan_id=plan.id, plan_key=plan.key,
                        reference=reference, provider=provider,
                        amount_minor=int(amount_minor), currency=currency,
                        status=m.PAY_PENDING, checkout_url=checkout_url or "")
        self.session.add(row)
        self.session.commit()
        return row

    def get_payment(self, payment_id: int) -> m.Payment | None:
        return self.session.get(m.Payment, payment_id)

    def get_payment_by_reference(self, reference: str) -> m.Payment | None:
        if not reference:
            return None
        return self.session.execute(
            select(m.Payment).where(m.Payment.reference == reference)
        ).scalars().first()

    def get_payment_by_provider_tx(self, provider_tx_id) -> m.Payment | None:
        if not provider_tx_id:
            return None
        return self.session.execute(
            select(m.Payment)
            .where(m.Payment.provider_tx_id == str(provider_tx_id))
        ).scalars().first()

    def settle_payment(self, reference: str, *, provider_tx_id, amount_minor: int,
                       currency: str, payload: dict) -> tuple[m.Payment | None, bool]:
        """Move a pending payment to successful, **once**.

        Returns ``(payment, first_time)``. ``first_time`` is False when the
        payment was already settled, which is the replay signal: the caller must
        not re-grant the subscription or re-send the receipt.

        The guard is a conditional UPDATE — ``WHERE status = 'pending'`` — rather
        than a read-then-write. Two webhook deliveries arriving on two threads
        can both pass a read, but only one can win the update; the other sees
        ``rowcount == 0``. The amount and currency are re-checked inside the same
        statement's caller (see :mod:`app.payments`) against the plan price, so a
        tampered charge cannot settle at a lower figure.
        """
        now = _utcnow()
        result = self.session.execute(
            update(m.Payment)
            .where(m.Payment.reference == reference)
            .where(m.Payment.status == m.PAY_PENDING)
            .values(status=m.PAY_SUCCESSFUL, verified=True,
                    provider_tx_id=str(provider_tx_id) if provider_tx_id else None,
                    amount_minor=int(amount_minor), currency=currency,
                    verification_json=payload or {}, paid_at=now, updated_at=now)
        )
        self.session.commit()
        row = self.get_payment_by_reference(reference)
        return row, result.rowcount == 1

    def fail_payment(self, reference: str, reason: str = "") -> m.Payment | None:
        """Mark a payment failed. Never touches an already-successful row.

        A late failure notice for a payment that succeeded must not revoke a
        subscription the customer paid for, so this refuses to move a settled
        row and says so by returning it unchanged.
        """
        row = self.get_payment_by_reference(reference)
        if row is None:
            return None
        if row.status == m.PAY_SUCCESSFUL:
            return row
        row.status = m.PAY_FAILED
        row.failure_reason = (reason or "")[:500]
        row.updated_at = _utcnow()
        self.session.commit()
        return row

    def list_payments(self, *, user_id: int | None = None,
                      status: str | None = None, limit: int = 200,
                      ) -> list[m.Payment]:
        stmt = select(m.Payment)
        if user_id is not None:
            stmt = stmt.where(m.Payment.user_id == user_id)
        if status:
            stmt = stmt.where(m.Payment.status == status)
        return list(self.session.execute(
            stmt.order_by(m.Payment.id.desc())
            .limit(max(1, min(limit, 2000)))).scalars().all())

    # ------------------------------------------------------------------ #
    # In-app notifications
    # ------------------------------------------------------------------ #
    def notify(self, user_id: int, *, kind: str, title: str, body: str = "",
               link: str = "") -> m.Notification:
        row = m.Notification(user_id=user_id, kind=kind, title=title[:160],
                             body=body, link=link)
        self.session.add(row)
        self.session.commit()
        return row

    def list_notifications(self, user_id: int, *, limit: int = 50,
                           ) -> list[m.Notification]:
        return list(self.session.execute(
            select(m.Notification)
            .where(m.Notification.user_id == user_id)
            .order_by(m.Notification.id.desc())
            .limit(max(1, min(limit, 200)))).scalars().all())

    def unread_notification_count(self, user_id: int) -> int:
        return int(self.session.execute(
            select(func.count()).select_from(m.Notification)
            .where(m.Notification.user_id == user_id)
            .where(m.Notification.read_at.is_(None))).scalar_one())

    def mark_notifications_read(self, user_id: int) -> int:
        """Mark every notification read for one user. Scoped by user id.

        The ``user_id`` predicate is not optional: without it this would mark
        the whole table read, which is exactly the kind of ownership bug that
        turns a "mark all read" button into a cross-account write.
        """
        result = self.session.execute(
            update(m.Notification)
            .where(m.Notification.user_id == user_id)
            .where(m.Notification.read_at.is_(None))
            .values(read_at=_utcnow()))
        self.session.commit()
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------ #
    # Password resets
    # ------------------------------------------------------------------ #
    def create_password_reset(self, *, user_id: int, token_hash: str,
                              expires_at: datetime) -> m.PasswordReset:
        """Store a reset grant. Any earlier unused grant is invalidated first.

        Superseding rather than accumulating means a user who clicks "forgot
        password" three times has exactly one working link — the newest — which
        is what they expect and shrinks the window an old email stays useful.
        """
        now = _utcnow()
        for old in self.session.execute(
                select(m.PasswordReset)
                .where(m.PasswordReset.user_id == user_id)
                .where(m.PasswordReset.used_at.is_(None))).scalars().all():
            old.used_at = now
        row = m.PasswordReset(user_id=user_id, token_hash=token_hash,
                              expires_at=expires_at)
        self.session.add(row)
        self.session.commit()
        return row

    def consume_password_reset(self, token_hash: str) -> m.User | None:
        """Redeem a reset grant, once. Returns the user it belonged to.

        The claim is a conditional UPDATE on ``used_at IS NULL``, so two
        submissions of the same link cannot both succeed — the second sees
        ``rowcount == 0`` and is refused. An expired or already-used token, or
        one for a deleted account, returns ``None``.
        """
        row = self.session.execute(
            select(m.PasswordReset)
            .where(m.PasswordReset.token_hash == token_hash)
        ).scalars().first()
        if row is None or row.expires_at <= _utcnow():
            return None

        result = self.session.execute(
            update(m.PasswordReset)
            .where(m.PasswordReset.id == row.id)
            .where(m.PasswordReset.used_at.is_(None))
            .values(used_at=_utcnow()))
        self.session.commit()
        if result.rowcount != 1:
            return None
        return self.get_user(row.user_id)

    def purge_expired_password_resets(self, *, older_than_days: int = 7) -> int:
        """Housekeeping: drop grants that are long dead."""
        cutoff = _utcnow() - timedelta(days=max(1, older_than_days))
        result = self.session.execute(
            delete(m.PasswordReset).where(m.PasswordReset.expires_at < cutoff))
        self.session.commit()
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------ #
    # Account maintenance (self-service profile + admin edits)
    # ------------------------------------------------------------------ #
    #: Columns a user may change about themselves. Role, status, password and
    #: subscription are absent on purpose: those are exactly the fields the brief
    #: says a user must not be able to set for themselves.
    SELF_EDITABLE = ("display_name", "email", "phone", "country", "avatar_path")

    #: What an *administrator* may edit on any account: everything the account
    #: holder may edit about themselves, plus the fields that are the operator's
    #: and not the user's. ``notes`` is the only one so far — an internal
    #: remark about a client that the client must never see, which is exactly
    #: why it is not in ``SELF_EDITABLE``.
    #:
    #: Note what is absent from both lists and must stay absent: ``role``,
    #: ``status`` and ``password_hash``. Each has its own method
    #: (:meth:`set_user_role`, :meth:`set_user_status`,
    #: :meth:`set_user_password`) with its own validation, so there is no way to
    #: write one of them by putting an extra field in a profile form.
    ADMIN_EDITABLE = SELF_EDITABLE + ("notes",)

    def update_user(self, user_id: int, **fields) -> m.User | None:
        """Update a user's own profile fields. Unknown keys are ignored.

        Ignoring rather than raising matches :meth:`update_client_profile`: a
        form field added in a template must never be able to break the save, and
        — more importantly — must never be able to write a column it was not
        meant to.
        """
        return self._update_user_fields(user_id, fields, self.SELF_EDITABLE)

    def update_user_as_admin(self, user_id: int, **fields) -> m.User | None:
        """Update a user's profile as an administrator.

        Same contract as :meth:`update_user` against the wider
        :data:`ADMIN_EDITABLE` set. Kept as a separate method rather than a flag
        on the first one so that the two permission levels cannot be confused at
        a call site: which one was called is the whole decision.
        """
        return self._update_user_fields(user_id, fields, self.ADMIN_EDITABLE)

    def _update_user_fields(self, user_id: int, fields: dict,
                            allowed: tuple[str, ...]) -> m.User | None:
        row = self.get_user(user_id)
        if row is None:
            return None
        for field, value in fields.items():
            if field in allowed:
                setattr(row, field, value)
        row.updated_at = _utcnow()
        self.session.commit()
        return row

    def set_user_role(self, user_id: int, role: str) -> m.User | None:
        """Change a role. Only the two declared roles are accepted."""
        if role not in m.ROLES:
            return None
        row = self.get_user(user_id)
        if row is None:
            return None
        row.role = role
        row.updated_at = _utcnow()
        self.session.commit()
        return row

    def soft_delete_user(self, user_id: int) -> m.User | None:
        """Remove an account without destroying its financial history.

        A hard DELETE would either orphan the user's payments and subscriptions
        or cascade them away — and those are the records an operator needs if a
        charge is ever disputed. Marking the row instead keeps the audit trail
        intact while making the account unreachable: :meth:`get_user` reports it
        absent, so any open session stops resolving on its very next request.

        Also expires any live subscription, so a deleted account is not still
        holding a plan that counts toward the platform's active figures.
        """
        row = self.session.get(m.User, user_id)
        if row is None:
            return None
        now = _utcnow()
        row.deleted_at = now
        row.updated_at = now
        row.status = m.STATUS_SUSPENDED
        for sub in self.session.execute(
                select(m.Subscription)
                .where(m.Subscription.user_id == user_id)
                .where(m.Subscription.status == m.SUB_ACTIVE)).scalars().all():
            sub.status = m.SUB_EXPIRED
            sub.updated_at = now
        self.session.commit()
        return row

    def restore_user(self, user_id: int) -> m.User | None:
        row = self.session.get(m.User, user_id)
        if row is None:
            return None
        row.deleted_at = None
        row.status = m.STATUS_ACTIVE
        row.updated_at = _utcnow()
        self.session.commit()
        return row

    # ------------------------------------------------------------------ #
    # Platform statistics (admin dashboard)
    #
    # Every figure is a COUNT over stored rows. Nothing here is estimated,
    # extrapolated or invented — a number the operator cannot reconcile against
    # a table is worse than no number.
    # ------------------------------------------------------------------ #
    def platform_stats(self) -> dict:
        now = _utcnow()
        users = select(func.count()).select_from(m.User)
        live_users = users.where(m.User.deleted_at.is_(None))
        stats: dict[str, int] = {
            "users_total": int(self.session.execute(live_users).scalar_one()),
            "users_active": int(self.session.execute(
                live_users.where(m.User.status == m.STATUS_ACTIVE)).scalar_one()),
            "users_suspended": int(self.session.execute(
                live_users.where(m.User.status == m.STATUS_SUSPENDED)).scalar_one()),
            "admins": int(self.session.execute(
                live_users.where(m.User.role == m.ROLE_ADMIN)).scalar_one()),
            "users_deleted": int(self.session.execute(
                users.where(m.User.deleted_at.is_not(None))).scalar_one()),
        }

        # Live subscriptions per plan, from the subscriptions table.
        live_sub = (select(func.count()).select_from(m.Subscription)
                    .where(m.Subscription.status == m.SUB_ACTIVE)
                    .where(or_(m.Subscription.expires_at.is_(None),
                               m.Subscription.expires_at > now)))
        for key in ("basic", "premium", "vip"):
            stats[f"subscribers_{key}"] = int(self.session.execute(
                live_sub.where(m.Subscription.plan_key == key)).scalar_one())
        stats["subscriptions_active"] = int(self.session.execute(
            live_sub).scalar_one())
        stats["subscriptions_total"] = int(self.session.execute(
            select(func.count()).select_from(m.Subscription)).scalar_one())

        # Payments by state, plus realised revenue in minor units.
        for state in m.PAYMENT_STATES:
            stats[f"payments_{state}"] = int(self.session.execute(
                select(func.count()).select_from(m.Payment)
                .where(m.Payment.status == state)).scalar_one())
        stats["revenue_minor"] = int(self.session.execute(
            select(func.coalesce(func.sum(m.Payment.amount_minor), 0))
            .where(m.Payment.status == m.PAY_SUCCESSFUL)).scalar_one() or 0)
        stats["payments_total"] = int(self.session.execute(
            select(func.count()).select_from(m.Payment)).scalar_one())
        return stats


def iter_session(settings: Settings | None = None) -> Iterator[Session]:
    """Context-managed session for scripts/CLI."""
    s = get_session(settings)
    try:
        yield s
    finally:
        s.close()
