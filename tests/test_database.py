"""Database layer tests (in-memory SQLite)."""
from dataclasses import dataclass, fields as dc_fields, replace
from datetime import datetime

import pytest
from database.models import Base
from database.repository import Repository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import get_settings
from trading.signal_engine import Signal

from database import models as m


def _signal(**kw):
    base = dict(
        asset="USTEC", direction="buy",
        entry=101.7, sl=99.5, tp=106.0,
        entry_time_utc=datetime(2026, 1, 6, 13, 10),
        entry_time_ny=datetime(2026, 1, 6, 9, 10),
        session_keys=["ny_am"], session_primary="ny_am",
        silver_bullet=None, macro="pre_ny_open",
        liquidity_type="PDL", liquidity_price=100.0,
        purge_time_ny=datetime(2026, 1, 6, 8, 0),
        cisd_tf="M15", cisd_confirm_time_ny=datetime(2026, 1, 6, 8, 30),
        fvg_direction="bullish", fvg_lower=101.4, fvg_upper=101.55,
        rr=2.0, status="APPROVED", alert_only=True, risk_approved=True,
    )
    base.update(kw)
    return Signal(**base)


def _repo():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return Repository(session=maker())


def test_signal_roundtrip_and_dedupe():
    r = _repo()
    sig = _signal()
    r.save_signal(sig)
    r.save_signal(sig)  # same fingerprint => no duplicate
    rows = r.recent_signals()
    assert len(rows) == 1
    assert rows[0].fingerprint == sig.fingerprint()
    assert rows[0].asset == "USTEC"
    assert rows[0].rr == 2.0


def test_recent_signals_order_desc():
    r = _repo()
    r.save_signal(_signal(entry=1.0))
    r.save_signal(_signal(entry=2.0, liquidity_price=99.0))
    rows = r.recent_signals(limit=10)
    assert [x.entry for x in rows] == [2.0, 1.0]


def test_fingerprint_exists():
    r = _repo()
    sig = _signal()
    assert not r.fingerprint_exists(sig.fingerprint())
    r.save_signal(sig)
    assert r.fingerprint_exists(sig.fingerprint())


def test_trade_save():
    from trading.executor import ExecutionResult
    r = _repo()
    sig = _signal()
    row_sig = r.save_signal(sig)
    res = ExecutionResult(
        asset="USTEC", symbol="USTEC", direction="buy",
        signal_fingerprint=sig.fingerprint(), entry=101.7, sl=99.5, tp=106.0,
        lots=0.0, status="SKIPPED", reason="auto_trading_disabled",
        requested_at_utc=datetime(2026, 1, 6, 13, 10),
    )
    r.save_trade(res, signal_id=row_sig.id)
    trades = r.recent_trades()
    assert len(trades) == 1
    assert trades[0].status == "SKIPPED"


def test_backtest_roundtrip():
    r = _repo()
    r.save_backtest(
        name="bt1", asset="USTEC", symbol="USTEC",
        start_utc=datetime(2026, 1, 1), end_utc=datetime(2026, 1, 2),
        params={"min_rr": 1.5}, summary={"n_trades": 1},
        trades=[dict(asset="USTEC", direction="buy", entry=1.0, sl=0.9, tp=1.3,
                     exit_price=1.3, entry_time_utc=datetime(2026, 1, 1, 10),
                     exit_time_utc=datetime(2026, 1, 1, 12),
                     outcome="WIN", pnl=0.3, rr=3.0, bars_held=120, reason="tp")],
    )
    bts = r.recent_backtests()
    assert len(bts) == 1
    trades = r.backtest_trades(bts[0].id)
    assert len(trades) == 1 and trades[0].outcome == "WIN"


def test_events_and_assets():
    r = _repo()
    r.log_event("INFO", "scanner", "started")
    assert len(r.recent_events()) == 1
    r.upsert_asset("GOLD", "XAUUSDm", enabled=False)
    r.upsert_asset("GOLD", "XAUUSDm", enabled=True, digits=2)
    assets = r.list_assets()
    assert len(assets) == 1 and assets[0].enabled is True


def test_counts_and_filtered_signals():
    r = _repo()
    a = r.save_signal(_signal(status="APPROVED"))
    r.save_signal(_signal(status="REJECTED", entry=99.0, liquidity_price=101.0,
                          direction="sell"))
    assert r.count_signals() == 2
    assert r.count_signals("APPROVED") == 1
    assert r.get_signal(a.id).status == "APPROVED"
    found = r.find_signals(status="APPROVED", asset="USTEC")
    assert [x.id for x in found] == [a.id]
    assert r.find_signals(asset="NOPE") == []


def test_trades_for_fingerprint():
    from trading.executor import ExecutionResult
    r = _repo()
    sig = _signal()
    row = r.save_signal(sig)
    r.save_trade(_result(sig, status="SKIPPED"), signal_id=row.id)
    assert len(r.trades_for_fingerprint(sig.fingerprint())) == 1
    assert r.count_trades() == 1 and r.count_trades("SKIPPED") == 1
    assert r.count_trades("SENT") == 0
    assert r.count_backtests() == 0


def _result(sig, status="SKIPPED"):
    from trading.executor import ExecutionResult
    return ExecutionResult(
        asset=sig.asset, symbol=sig.asset, direction=sig.direction,
        signal_fingerprint=sig.fingerprint(), entry=sig.entry, sl=sig.sl,
        tp=sig.tp, lots=0.0, status=status, reason="auto_trading_disabled",
        requested_at_utc=sig.entry_time_utc,
    )


# --------------------------------------------------------------------------- #
# The dataclass <-> table contract
#
# ``Repository.save_signal`` builds its INSERT from the model's own columns, so
# keeping the dataclass and the table in step is what makes a new field persist.
# These are the only thing between a forgotten field and silent data loss.
# --------------------------------------------------------------------------- #
def test_every_signal_column_has_a_dataclass_field():
    columns = {c.name for c in m.Signal.__table__.columns}
    dataclass_fields = {f.name for f in dc_fields(Signal)}

    # Every column is filled — ``fingerprint`` is computed, the other two are
    # assigned by the database...
    assert columns - dataclass_fields == {"id", "created_at", "fingerprint"}
    # ...and no field is left with nowhere to go.
    assert dataclass_fields <= columns


def test_save_signal_persists_every_field():
    """A field that stops being written fails here rather than in production."""
    r = _repo()
    sig = _signal(ai_status="ok", ai_score=0.8, ai_decision="agree",
                  ai_reasoning="clean reclaim", ai_confidence=0.7,
                  ai_strengths=["session"], ai_risks=["news"])
    row = r.save_signal(sig)

    for f in dc_fields(Signal):
        assert getattr(row, f.name) == getattr(sig, f.name), f.name


def test_update_signal_ai_writes_every_ai_field():
    r = _repo()
    row = r.save_signal(_signal())
    sig = _signal(ai_status="ok", ai_score=0.8, ai_decision="agree",
                  ai_reasoning="clean reclaim", ai_confidence=0.7,
                  ai_strengths=["session"], ai_risks=["news"])

    updated = r.update_signal_ai(row.id, sig)

    assert updated.ai_status == "ok"
    assert updated.ai_decision == "agree"
    assert updated.ai_confidence == 0.7
    assert updated.ai_strengths == ["session"]
    assert updated.ai_risks == ["news"]


def test_save_signal_names_the_field_it_cannot_persist():
    """An object missing a column fails loudly instead of writing NULLs."""

    @dataclass
    class _Partial:
        asset: str = "USTEC"
        direction: str = "buy"

        def fingerprint(self) -> str:
            return "deadbeef"

    with pytest.raises(TypeError, match="required by the signals table"):
        _repo().save_signal(_Partial())


# --------------------------------------------------------------------------- #
# Additive migrations against a database created by an older build
# --------------------------------------------------------------------------- #
def _legacy_engine(tmp_path):
    """A database holding the ``signals`` table *without* the ICT columns."""
    from sqlalchemy import text

    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for column in ("purge_grade", "target_kind", "target_price",
                       "target_grade", "risk_points", "reward_points",
                       "efficiency_score", "setup_id", "state", "digits",
                       "structure_time_ny"):
            conn.execute(text(f"ALTER TABLE signals DROP COLUMN {column}"))
    return engine


def _columns(engine, table="signals"):
    from sqlalchemy import inspect

    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_ensure_schema_backfills_columns_an_older_build_never_had(tmp_path):
    """The whole point of the additive migration: an existing DB is upgraded.

    ``create_all`` only CREATEs a missing table, so without this the first insert
    on an existing database fails with "no such column".
    """
    from database.repository import ensure_schema

    engine = _legacy_engine(tmp_path)
    missing = {"purge_grade", "target_kind", "target_price", "efficiency_score",
               "setup_id", "state", "digits"}
    assert not (missing & _columns(engine)), "the fixture failed to strip them"

    ensure_schema(engine)

    assert missing <= _columns(engine)


def test_ensure_schema_is_idempotent(tmp_path):
    from database.repository import ensure_schema

    engine = _legacy_engine(tmp_path)
    ensure_schema(engine)
    before = _columns(engine)
    ensure_schema(engine)          # must not raise, must not change anything
    ensure_schema(engine)
    assert _columns(engine) == before


def test_ensure_schema_keeps_existing_rows(tmp_path):
    """Upgrading must never drop or rewrite data."""
    from sqlalchemy import text

    from database.repository import ensure_schema

    engine = _legacy_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO signals (fingerprint, asset, direction, entry, sl, tp,"
            " entry_time_utc, entry_time_ny, session_keys, session_primary,"
            " silver_bullet, macro, liquidity_type, liquidity_price, purge_time_ny,"
            " cisd_tf, cisd_confirm_time_ny, fvg_direction, fvg_lower, fvg_upper,"
            " fvg_formation_time_ny, structure_extreme_price, rr, status, reason,"
            " alert_only, risk_approved, created_at)"
            " VALUES ('fp1','US100','buy',101.7,99.5,106.0,"
            " '2026-01-06 13:10:00','2026-01-06 09:10:00','[\"ny_am\"]','ny_am',"
            " NULL,NULL,'PDL',100.0,'2026-01-06 08:00:00','M5','2026-01-06 08:30:00',"
            " 'bullish',101.4,101.55,'2026-01-06 09:05:00',99.6,2.0,'APPROVED','',"
            " 1,1,'2026-01-06 13:10:00')"))

    ensure_schema(engine)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT fingerprint, asset FROM signals")).all()
    assert rows == [("fp1", "US100")]


def test_the_dashboard_entry_point_migrates_an_existing_database(tmp_path):
    """``create_app`` is a first-class entry point and must migrate too.

    It used to call ``create_all`` alone, so starting the dashboard against a
    database from an older build left every new column missing — and the first
    signal insert died with "no such column".
    """
    from app.web import create_app

    engine = _legacy_engine(tmp_path)
    legacy_url = f"sqlite:///{tmp_path/'legacy.db'}"

    # Real settings, pointed at the legacy file. A hand-rolled stub used to do
    # here, but ``create_app`` now also wires authentication, which reads the
    # session and throttle settings — so the stub has to be the real thing.
    cfg = replace(get_settings(), db_url=legacy_url,
                  flask_secret_key="test-secret")

    create_app(settings=cfg)             # setup_db defaults to True
    engine.dispose()

    from sqlalchemy import create_engine as _create

    upgraded = _create(legacy_url, future=True)
    assert {"purge_grade", "target_kind", "setup_id", "state"} <= _columns(upgraded)
    upgraded.dispose()


def test_engine_state_is_a_singleton_that_updates_in_place():
    """The engine publishes into one row, so a reading is never stale-by-append.

    The scanner overwrites this row on every poll; a table that grew a row per
    tick would leave the dashboard reading whichever one it happened to pick.
    """
    r = _repo()
    r.save_engine_state(instance_id="abc", pid=1, state="running",
                        assets=["USTEC"], prices={"USTEC": 101.75},
                        heartbeat_utc=datetime(2026, 1, 6, 18, 33),
                        lease_seconds=5.0)

    # A partial write, as the loop's per-poll publish is: keys it does not pass
    # keep their previous value rather than being cleared.
    r.save_engine_state(instance_id="abc", pid=1, state="running",
                        activity="Scanning M5", active=True)

    assert r.session.query(m.EngineState).count() == 1
    row = r.load_engine_state()
    assert row.assets == ["USTEC"]          # JSON re-read, not the same object
    assert row.prices == {"USTEC": 101.75}
    assert row.lease_seconds == 5.0
    assert row.activity == "Scanning M5"
    assert row.active is True


def test_engine_state_json_survives_a_reopened_session():
    """A fresh session proves the values are in the database, not in memory.

    This is the property the dashboard depends on: the web process opens its own
    session and must see what the scanner process wrote.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    Repository(session=maker()).save_engine_state(
        instance_id="abc", pid=7, state="running", quotes={
            "USTEC": {"bid": 101.70, "ask": 101.74, "spread": 0.04,
                      "spread_points": 4, "digits": 2,
                      "time_utc": "2026-01-06T18:33:00"}},
        setups={"USTEC": {"buy": "WAITING_FOR_FVG_RETRACE", "sell": "NO_SETUP"}},
        price_times={"USTEC": "2026-01-06T18:32:00"})

    row = Repository(session=maker()).load_engine_state()
    assert row.quotes["USTEC"]["bid"] == 101.70
    assert row.quotes["USTEC"]["spread_points"] == 4
    assert row.setups["USTEC"]["buy"] == "WAITING_FOR_FVG_RETRACE"
    assert row.price_times["USTEC"] == "2026-01-06T18:32:00"


def test_an_owned_session_is_closed_and_an_injected_one_is_left_alone(tmp_path):
    """The distinction that a pooled engine makes load-bearing.

    ``session=`` means the caller manages that session's lifetime, so ``close``
    leaves it be. ``engine=`` shares a pool while keeping the session owned, so
    ``close`` returns the connection immediately rather than whenever the
    garbage collector happens to run. A short-lived read that relied on the
    collector would hold its connection past the end of the request.

    A file-backed database, not ``:memory:`` — in-memory SQLite is served by
    ``SingletonThreadPool``, which exposes no ``checkedout()`` at all. The pool
    that can actually run dry is ``QueuePool``, and a file URL is what selects
    it, so this mirrors the deployed configuration as well as being testable.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}", future=True)
    Base.metadata.create_all(engine)
    pool = engine.pool

    injected = Repository(session=sessionmaker(bind=engine, future=True)())
    injected.load_engine_state()
    injected.close()
    assert pool.checkedout() == 1        # the caller still owns this one
    injected.session.close()
    assert pool.checkedout() == 0

    owned = Repository(engine=engine)
    owned.load_engine_state()
    assert pool.checkedout() == 1
    owned.close()
    assert pool.checkedout() == 0        # returned on close, not on collection
