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
           "Repository"]

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
    engine = get_engine(settings)
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
