"""Flask dashboard tests (test client + in-memory SQLite; no MT5/network)."""
import json
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.web import create_app
from config import get_settings
from database.models import Base
from database.repository import Repository
from trading import time_utils as tu
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

    # The list is a list of *batches*. A row written before batching existed
    # surfaces as a one-asset batch rather than disappearing from the page.
    body = client.get("/backtests").get_data(as_text=True)
    assert "TEST" in body
    assert "/backtests/batch/" in body

    bt = repo.recent_backtests()[0]
    resp = client.get(f"/backtests/{bt.id}")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Equity curve" in text
    assert "WIN" in text and "equityChart" in text


def _save_batch_asset(repo, batch_id, asset, total_r, wins, losses):
    """One per-asset row of a batch, with only the legacy summary keys set.

    Deliberately omits the extended metrics so the comparison path is exercised
    against summaries written before those keys existed.
    """
    n_trades = wins + losses
    return repo.save_backtest(
        name=asset, asset=asset, symbol=asset, batch_id=batch_id,
        start_utc=datetime(2026, 1, 1), end_utc=datetime(2026, 1, 2),
        params={},
        summary={
            "asset": asset, "n_signals": n_trades, "n_trades": n_trades,
            "n_open": 0, "n_wins": wins, "n_losses": losses,
            "win_rate": (wins / n_trades) if n_trades else 0.0,
            "profit_factor": 1.0, "total_r": total_r,
            "max_drawdown_r": 0.0, "equity_curve": [],
        },
        trades=[],
    )


def test_backtest_batch_comparison_ranks_assets_by_expectancy():
    repo = _repo()
    batch = "batch-under-test"
    # Same total opportunity, opposite outcomes: GOOD earns 2R/trade, BAD loses 1R.
    _save_batch_asset(repo, batch, "GOOD", total_r=4.0, wins=2, losses=0)
    _save_batch_asset(repo, batch, "BAD", total_r=-2.0, wins=0, losses=2)
    client = _client(repo)

    body = client.get(f"/backtests/batch/{batch}").get_data(as_text=True)
    assert "Best asset by expectancy" in body
    # Ranked best-first: the profitable asset is rendered before the losing one.
    assert body.index("GOOD") < body.index("BAD")


def test_backtest_batch_with_no_trades_ranks_nothing_best():
    repo = _repo()
    batch = "empty-batch"
    _save_batch_asset(repo, batch, "QUIET", total_r=0.0, wins=0, losses=0)
    client = _client(repo)

    body = client.get(f"/backtests/batch/{batch}").get_data(as_text=True)
    # "Never traded" is no evidence, so it must not be presented as a pick.
    assert "No asset produced a closed trade" in body
    assert "Best asset by expectancy" not in body


def test_unknown_batch_is_404():
    client = _client(_repo())
    assert client.get("/backtests/batch/nope").status_code == 404


def test_health_endpoint():
    client = _client(_repo())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# Control API (start/stop live, run backtest, poll status)
# --------------------------------------------------------------------------- #
class _FakeJobs:
    """Stand-in for runner.JobManager so no test can reach MT5."""

    def __init__(self):
        self.live = {"state": "idle", "started_at_utc": None,
                     "stopped_at_utc": None, "assets": [],
                     "last_candle_utc": None, "signals_session": 0,
                     "last_error": ""}
        self.backtest = {"state": "idle", "asset": None, "mode": "bars",
                         "bars": None, "start_utc": None, "end_utc": None,
                         "max_hold_m1": None, "queued": False,
                         "queued_reason": "", "progress_done": 0,
                         "progress_total": 0, "last_backtest_id": None,
                         "last_error": "", "finished_at_utc": None}
        self.probe = {"state": "idle", "asset": None, "symbol": None,
                      "n_bars": 0, "oldest_utc": None, "newest_utc": None,
                      "last_error": "", "finished_at_utc": None}
        self.start_result = {"ok": True, "message": "Live session starting."}
        self.requests = []
        self.probe_requests = []

    def start_live(self):
        result = dict(self.start_result)
        if result["ok"]:
            self.live["state"] = "running"
        result["live"] = dict(self.live)
        return result

    def stop_live(self):
        self.live["state"] = "stopped"
        return {"ok": True, "message": "Live session stopped.",
                "live": dict(self.live)}

    def request_backtest(self, **kw):
        self.requests.append(kw)
        state = dict(self.backtest)
        state.update({k: v for k, v in kw.items() if v is not None})
        state["mode"] = "range" if kw.get("start_utc") else "bars"
        self.backtest = state
        return {"ok": True, "queued": False, "message": "Backtest starting.",
                "backtest": dict(state)}

    def request_data_probe(self, asset):
        self.probe_requests.append(asset)
        self.probe = {**self.probe, "state": "done", "asset": asset,
                      "symbol": asset, "n_bars": 1000}
        return {"ok": True, "message": "Checking available history…",
                "probe": dict(self.probe)}

    def status(self):
        return {"live": dict(self.live), "backtest": dict(self.backtest),
                "probe": dict(self.probe),
                "live_running": self.live["state"] == "running"}

    def is_live_running(self):
        return self.live["state"] == "running"

    def live_state(self):
        return dict(self.live)

    def backtest_state(self):
        return dict(self.backtest)

    def probe_state(self):
        return dict(self.probe)


def _api_client(repo, jobs=None, cfg=None):
    """Client with Telegram forced off so UI warnings are deterministic."""
    settings = cfg or replace(get_settings(), telegram_enabled=False,
                              telegram_bot_token="", telegram_chat_id="")
    return create_app(settings=settings, repository=repo, setup_db=False,
                      jobs=jobs or _FakeJobs()).test_client()


def _same_origin():
    """Headers a browser sends for a same-origin POST from the dashboard."""
    return {"Origin": "http://localhost"}


def test_status_endpoint_shape():
    repo = _repo()
    payload = _api_client(repo).get("/api/status").get_json()
    assert payload["live"]["state"] == "idle"
    assert payload["backtest"]["state"] == "idle"
    assert payload["signals"]["last_id"] == 0
    assert "telegram" in payload and "configured" in payload["telegram"]
    assert "assets" in payload and "backtest_defaults" in payload


def test_status_timestamps_use_the_ny_key_the_ui_reads():
    """The dashboard JS reads ``*_ny``; keep the API contract pinned."""
    jobs = _FakeJobs()
    jobs.live["started_at_utc"] = datetime(2026, 1, 5, 14, 30)
    jobs.live["last_candle_utc"] = datetime(2026, 1, 5, 14, 31)
    jobs.backtest["finished_at_utc"] = datetime(2026, 1, 5, 15, 0)

    payload = _api_client(_repo(), jobs).get("/api/status").get_json()

    assert payload["live"]["started_at_ny"] == "2026-01-05 10:30"   # UTC-4
    assert payload["live"]["last_candle_ny"] == "2026-01-05 10:31"
    assert payload["backtest"]["finished_at_ny"] == "2026-01-05 11:00"


def test_status_reports_new_signals_after_id():
    repo = _repo()
    first = _save_signal(repo)
    second = _save_signal(repo, entry=99.0, direction="sell")

    payload = _api_client(repo).get(
        f"/api/status?after_signal_id={first.id}").get_json()
    ids = [s["id"] for s in payload["signals"]["new"]]
    assert ids == [second.id]
    assert payload["signals"]["last_id"] == second.id
    new = payload["signals"]["new"][0]
    assert new["direction"] == "sell" and new["url"].endswith(f"/signals/{second.id}")


def test_live_start_and_stop_endpoints():
    jobs = _FakeJobs()
    client = _api_client(_repo(), jobs)

    started = client.post("/api/live/start", headers=_same_origin())
    assert started.status_code == 200
    assert started.get_json()["ok"] is True
    assert jobs.live["state"] == "running"

    stopped = client.post("/api/live/stop", headers=_same_origin())
    assert stopped.status_code == 200
    assert jobs.live["state"] == "stopped"


def test_live_start_conflict_returns_409():
    jobs = _FakeJobs()
    jobs.start_result = {"ok": False, "reason": "already_running",
                         "message": "A live session is already running."}
    resp = _api_client(_repo(), jobs).post("/api/live/start",
                                           headers=_same_origin())
    assert resp.status_code == 409
    assert resp.get_json()["reason"] == "already_running"


def test_backtest_run_passes_validated_parameters():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post(
        "/api/backtest/run",
        headers=_same_origin(),
        data={"asset": "USTEC", "bars": "1500", "max_hold_m1": "60"},
    )
    assert resp.status_code == 200
    assert jobs.requests == [{"asset": "USTEC", "bars": 1500,
                              "start_utc": None, "end_utc": None,
                              "max_hold_m1": 60}]


def test_backtest_run_rejects_unknown_asset():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post("/api/backtest/run",
                                           headers=_same_origin(),
                                           data={"asset": "NOT_A_SYMBOL"})
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "unknown_asset"
    assert jobs.requests == []


def test_backtest_run_rejects_out_of_range_bars():
    jobs = _FakeJobs()
    client = _api_client(_repo(), jobs)

    too_small = client.post("/api/backtest/run", headers=_same_origin(),
                            data={"bars": "5"})
    assert too_small.status_code == 400
    assert too_small.get_json()["reason"] == "bad_bars"

    not_a_number = client.post("/api/backtest/run", headers=_same_origin(),
                               data={"bars": "many"})
    assert not_a_number.status_code == 400
    assert jobs.requests == []


def test_cross_origin_post_is_refused():
    """A malicious page must not be able to start a session via localhost."""
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post(
        "/api/live/start", headers={"Origin": "http://evil.example.com"})
    assert resp.status_code == 403
    assert jobs.live["state"] == "idle"


def test_cross_origin_backtest_post_is_refused():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post(
        "/api/backtest/run", headers={"Origin": "http://evil.example.com"},
        data={"bars": "1000"})
    assert resp.status_code == 403
    assert jobs.requests == []


def test_dashboard_renders_live_controls():
    body = _api_client(_repo()).get("/").get_data(as_text=True)
    assert "Live session" in body
    assert 'id="live-start"' in body and 'id="live-stop"' in body
    assert "dashboard.js" in body


def test_dashboard_warns_when_telegram_unconfigured():
    """Signals would be detected but silently go nowhere; the UI must say so."""
    body = _api_client(_repo()).get("/").get_data(as_text=True)
    assert "Telegram is not configured" in body


def test_backtests_page_renders_run_form():
    body = _api_client(_repo()).get("/backtests").get_data(as_text=True)
    assert "Run a backtest" in body
    assert 'id="backtest-form"' in body
    assert "USTEC" in body            # asset dropdown populated from the registry
    assert "Use the form above" in body


# --------------------------------------------------------------------------- #
# Backtest date range
# --------------------------------------------------------------------------- #
def test_backtest_accepts_an_explicit_date_range():
    """The core fix: a start/end window instead of only 'the last N bars'."""
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post(
        "/api/backtest/run", headers=_same_origin(),
        data={"asset": "USTEC", "start": "2024-01-01", "end": "2024-06-30"},
    )
    assert resp.status_code == 200

    sent = jobs.requests[0]
    assert sent["bars"] is None          # the range wins over the bar count
    # A bare NY date covers the whole NY day, both ends.
    assert sent["start_utc"] == tu.ny_to_utc(datetime(2024, 1, 1, 0, 0))
    assert sent["end_utc"] == tu.ny_to_utc(datetime(2024, 6, 30, 23, 59))


def test_backtest_accepts_a_precise_start_time():
    jobs = _FakeJobs()
    _api_client(_repo(), jobs).post(
        "/api/backtest/run", headers=_same_origin(),
        data={"asset": "USTEC", "start": "2024-01-01T09:30", "end": "2024-01-02"},
    )
    assert jobs.requests[0]["start_utc"] == tu.ny_to_utc(datetime(2024, 1, 1, 9, 30))


@pytest.mark.parametrize("payload", [
    {"start": "2024-01-01"},                                      # one-sided
    {"end": "2024-01-01"},                                        # one-sided
    {"start": "2024-06-30", "end": "2024-01-01"},                 # reversed
    {"start": "2024-01-01", "end": "2024-01-01"},                 # same day
    {"start": "not-a-date", "end": "2024-01-02"},
    {"start": "2024-01-01", "end": "2099-01-01"},                 # absurd span
])
def test_backtest_rejects_a_bad_date_range(payload):
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post("/api/backtest/run",
                                           headers=_same_origin(), data=payload)
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "bad_range"
    assert jobs.requests == []          # nothing was queued


def test_backtest_still_accepts_the_bar_count():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post(
        "/api/backtest/run", headers=_same_origin(),
        data={"asset": "USTEC", "bars": "1500"},
    )
    assert resp.status_code == 200
    assert jobs.requests[0]["bars"] == 1500
    assert jobs.requests[0]["start_utc"] is None


def test_range_error_is_a_sentence_a_user_can_act_on():
    resp = _api_client(_repo(), _FakeJobs()).post(
        "/api/backtest/run", headers=_same_origin(),
        data={"start": "2024-06-30", "end": "2024-01-01"})
    assert "after the start" in resp.get_json()["message"]


# --------------------------------------------------------------------------- #
# History probe
# --------------------------------------------------------------------------- #
def test_probe_reports_available_history():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post("/api/data/probe",
                                           headers=_same_origin(),
                                           data={"asset": "USTEC"})
    assert resp.status_code == 200
    assert jobs.probe_requests == ["USTEC"]
    assert resp.get_json()["probe"]["n_bars"] == 1000


def test_probe_requires_an_asset():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post("/api/data/probe",
                                           headers=_same_origin(), data={})
    assert resp.status_code == 400
    assert jobs.probe_requests == []


def test_probe_rejects_an_unknown_asset():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post("/api/data/probe",
                                           headers=_same_origin(),
                                           data={"asset": "NOPE"})
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "unknown_asset"
    assert jobs.probe_requests == []


def test_probe_is_refused_cross_origin():
    jobs = _FakeJobs()
    resp = _api_client(_repo(), jobs).post(
        "/api/data/probe", headers={"Origin": "http://evil.example.com"},
        data={"asset": "USTEC"})
    assert resp.status_code == 403
    assert jobs.probe_requests == []


def test_status_publishes_probe_and_ai_state():
    payload = _api_client(_repo()).get("/api/status").get_json()
    assert "probe" in payload
    assert set(payload["ai"]) == {"enabled", "model", "provider_host"}
    # The credential must never leave the server.
    assert "llm_api_key" not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# Asset registry management
# --------------------------------------------------------------------------- #
@pytest.fixture
def registry_cfg(tmp_path):
    """Settings pointing at a throwaway registry file."""
    path = tmp_path / "assets.json"
    path.write_text(json.dumps({"assets": [
        {"name": "USTEC", "broker_symbol": "USTEC", "enabled": True, "digits": 2},
        {"name": "GOLD", "broker_symbol": "XAUUSDm", "enabled": False, "digits": 2},
    ]}), encoding="utf-8")
    return replace(get_settings(), assets_file=path,
                   telegram_enabled=False, telegram_bot_token="",
                   telegram_chat_id="")


def _registry_client(repo, jobs, cfg):
    return create_app(settings=cfg, repository=repo, setup_db=False,
                      jobs=jobs).test_client()


def _read_registry(cfg) -> dict:
    raw = json.loads(cfg.assets_file.read_text(encoding="utf-8"))
    return {a["name"]: a for a in raw["assets"]}


def test_assets_add_writes_to_the_registry(registry_cfg):
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/add", headers=_same_origin(),
        data={"name": "eurusd", "broker_symbol": "EURUSDm", "digits": "5",
              "enabled": "on"})

    assert resp.status_code == 200
    stored = _read_registry(registry_cfg)["EURUSD"]   # name is upper-cased
    assert stored["broker_symbol"] == "EURUSDm"
    assert stored["enabled"] is True
    assert stored["digits"] == 5


def test_assets_add_rejects_a_duplicate(registry_cfg):
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/add", headers=_same_origin(),
        data={"name": "USTEC", "broker_symbol": "USTEC"})
    assert resp.status_code == 409
    assert resp.get_json()["reason"] == "already_exists"


@pytest.mark.parametrize("payload", [
    {"name": "", "broker_symbol": "XAUUSDm"},
    {"name": "GOLD", "broker_symbol": ""},
    {},
])
def test_assets_add_requires_a_name_and_symbol(registry_cfg, payload):
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/add", headers=_same_origin(), data=payload)
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "missing_fields"


def test_assets_toggle_enables_a_disabled_asset(registry_cfg):
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/toggle", headers=_same_origin(),
        data={"name": "GOLD", "enabled": "true"})

    assert resp.status_code == 200
    assert _read_registry(registry_cfg)["GOLD"]["enabled"] is True


def test_assets_toggle_rejects_an_unknown_asset(registry_cfg):
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/toggle", headers=_same_origin(),
        data={"name": "NOPE", "enabled": "true"})
    assert resp.status_code == 404


def test_assets_remove_deletes_a_disabled_asset(registry_cfg):
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/remove", headers=_same_origin(), data={"name": "GOLD"})

    assert resp.status_code == 200
    assert "GOLD" not in _read_registry(registry_cfg)


def test_assets_remove_refuses_the_last_enabled_asset(registry_cfg):
    """Removing it would leave the scanner with nothing to scan."""
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/remove", headers=_same_origin(), data={"name": "USTEC"})

    assert resp.status_code == 409
    assert resp.get_json()["reason"] == "last_enabled"
    assert "USTEC" in _read_registry(registry_cfg)


def test_assets_remove_is_refused_cross_origin(registry_cfg):
    resp = _registry_client(_repo(), _FakeJobs(), registry_cfg).post(
        "/api/assets/remove", headers={"Origin": "http://evil.example.com"},
        data={"name": "GOLD"})
    assert resp.status_code == 403
    assert "GOLD" in _read_registry(registry_cfg)


def test_dropdowns_read_the_registry_not_a_stale_db_row(registry_cfg):
    """A DB row the registry does not know about must not be offered.

    The registry is what the engine actually trades, so offering anything else
    would let the UI request an asset the scanner refuses to run.
    """
    repo = _repo()
    repo.upsert_asset("GHOST", "GHOSTX", enabled=True)
    body = _registry_client(repo, _FakeJobs(), registry_cfg).get("/backtests") \
        .get_data(as_text=True)

    assert "USTEC" in body
    assert "GHOST" not in body
