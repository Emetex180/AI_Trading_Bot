"""Flask dashboard tests (test client + in-memory SQLite; no MT5/network)."""
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.web import create_app
from database.models import Base
from database.repository import Repository
from trading.executor import ExecutionResult

from test_database import _signal


def _repo():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return Repository(session=maker())


def _client(repo):
    return create_app(repository=repo, setup_db=False).test_client()


def _save_signal(repo, **kw):
    return repo.save_signal(_signal(**kw))


def _save_backtest(repo):
    return repo.save_backtest(
        name="daily", asset="TEST", symbol="TEST",
        start_utc=datetime(2026, 1, 1), end_utc=datetime(2026, 1, 2),
        params={"min_rr": 1.5, "max_hold_m1": 720},
        summary={
            "n_signals": 2, "n_trades": 1, "n_open": 1, "n_wins": 1,
            "n_losses": 0, "win_rate": 1.0, "profit_factor": float("inf"),
            "total_r": 2.0, "max_drawdown_r": 0.0,
            "equity_curve": [["2026-01-01T14:00:00", 2.0]],
        },
        trades=[dict(asset="TEST", direction="buy", entry=101.7, sl=99.5, tp=106.0,
                     exit_price=106.0, entry_time_utc=datetime(2026, 1, 1, 13, 10),
                     exit_time_utc=datetime(2026, 1, 1, 14, 0),
                     outcome="WIN", pnl=2.0, rr=2.0, bars_held=50, reason="tp")],
    )


def test_index_renders_auto_trading_off_and_seeded_signal():
    repo = _repo()
    row = _save_signal(repo)
    client = _client(repo)

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ICT Multi-Asset Scanner" in body
    assert "AUTO-TRADING OFF" in body or "auto-trading-pill" in body
    assert "USTEC" in body          # seeded signal rendered
    assert "Approved" in body
    assert "1" in body  # signal count present


def test_signals_page_filters():
    repo = _repo()
    _save_signal(repo, status="APPROVED")
    _save_signal(repo, status="REJECTED", entry=99.0, liquidity_price=101.0,
                 direction="sell")
    client = _client(repo)

    all_body = client.get("/signals").get_data(as_text=True)
    assert "APPROVED" in all_body and "REJECTED" in all_body

    appr = client.get("/signals?status=APPROVED").get_data(as_text=True)
    assert "101.7000" in appr and "99.0000" not in appr  # rejected row gone


def test_signal_detail_shows_ai_and_executions():
    repo = _repo()
    row = _save_signal(repo, ai_status="AI_ANALYZED", ai_decision="agree",
                       ai_score=72.0, ai_confidence=0.8, ai_reasoning="clean sweep",
                       ai_strengths=["PDL"], ai_risks=["news risk"])
    res = ExecutionResult(
        asset="TEST", symbol="TEST", direction="buy",
        signal_fingerprint=row.fingerprint, entry=101.7, sl=99.5, tp=106.0,
        lots=0.0, status="SKIPPED", reason="auto_trading_disabled",
        requested_at_utc=datetime(2026, 1, 6, 13, 10),
    )
    repo.save_trade(res, signal_id=row.id)
    client = _client(repo)

    body = client.get(f"/signals/{row.id}").get_data(as_text=True)
    assert "AI_ANALYZED" in body or "AI analysis" in body
    assert "agree" in body
    assert "auto_trading_disabled" in body


def test_unknown_signal_is_404():
    client = _client(_repo())
    assert client.get("/signals/999").status_code == 404


def test_backtests_list_and_detail_with_equity_chart():
    repo = _repo()
    _save_backtest(repo)
    client = _client(repo)

    body = client.get("/backtests").get_data(as_text=True)
    assert "daily" in body and "TEST" in body

    bt = repo.recent_backtests()[0]
    resp = client.get(f"/backtests/{bt.id}")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Equity curve" in text
    assert "WIN" in text and "equityChart" in text


def test_health_endpoint():
    client = _client(_repo())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
