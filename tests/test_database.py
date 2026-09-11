"""Database layer tests (in-memory SQLite)."""
from dataclasses import dataclass, fields as dc_fields
from datetime import datetime

import pytest
from database.models import Base
from database.repository import Repository
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
