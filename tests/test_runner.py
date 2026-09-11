"""Job manager tests: live sessions and backtests, with no MT5 anywhere.

Every collaborator is injected (client / market / scanner / asset manager /
backtest runner), so these tests exercise the real threading, locking and
queueing logic in ``runner.py`` without a terminal, network or clock dependency.
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.orm import sessionmaker

from config import get_settings
from database.models import Base
from database.repository import Repository, get_engine
from runner import (BT_DONE, BT_ERROR, BT_QUEUED, LIVE_ERROR, LIVE_RUNNING,
                    LIVE_STOPPED, JobManager)
from trading.asset_manager import Asset

ASSET = Asset(name="TEST", broker_symbol="TEST", enabled=True)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self):
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def account_info(self):
        return None


class _BrokenClient(_FakeClient):
    """Simulates a closed MT5 terminal."""

    def connect(self):
        raise RuntimeError("MT5 terminal not running")


class _FakeMarket:
    def __init__(self):
        self.polls = 0

    def symbol_exists(self, symbol):
        return True

    def fetch_m1_closed(self, symbol, count, drop_forming=True):
        return []

    def poll_closed_candles(self, symbol, lookback=3):
        self.polls += 1
        return []


class _FakeScanner:
    """Stands in for :class:`scanner.AssetScanner`."""

    def __init__(self, asset, repo):
        self.asset = asset
        self.symbol = asset.broker_symbol
        self.repo = repo
        self.warmed = None
        self.steps = 0

    def warm(self, candles):
        self.warmed = len(candles)

    def feed_new(self, candles):
        self.steps += 1
        return 0

    def last_candle_time_utc(self):
        return datetime(2026, 1, 1, 12, 0)


class _FakeAssetManager:
    def __init__(self, assets):
        self._assets = list(assets)

    def enabled_assets(self):
        return list(self._assets)

    def get(self, name):
        for asset in self._assets:
            if asset.name == name:
                return asset
        raise KeyError(name)


class _FakeSummary:
    def __init__(self):
        self.start_utc = datetime(2026, 1, 1)
        self.end_utc = datetime(2026, 1, 2)
        self.params = {"max_hold_m1": 10}

    def to_dict(self):
        return {"n_signals": 1, "n_trades": 1, "n_open": 0, "n_wins": 1,
                "n_losses": 0, "win_rate": 1.0, "profit_factor": 2.0,
                "total_r": 2.0, "max_drawdown_r": 0.0, "equity_curve": []}


class _FakeBacktestRunner:
    """Records that it ran; returns a fixed summary and no trades."""

    runs: list = []

    def __init__(self, asset, max_hold_m1):
        self.asset = asset
        self.max_hold_m1 = max_hold_m1

    def run(self, candles, name=""):
        _FakeBacktestRunner.runs.append((self.asset.name, self.max_hold_m1))
        return _FakeSummary(), []


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _env(tmp_path, monkeypatch):
    """Settings + a session factory over a temp FILE db.

    A file db (not ``:memory:``) is used deliberately: the job workers run on
    their own threads and each gets its own Session, which is how the live
    dashboard behaves. The engine comes from the real ``get_engine`` so the
    production SQLite pragmas (WAL, check_same_thread) are exercised too.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runner.db'}")
    settings = get_settings()
    engine = get_engine(settings)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return settings, maker


def _jobs(settings, maker, *, assets=(ASSET,), client_factory=None, **kw):
    _FakeBacktestRunner.runs = []
    return JobManager(
        settings=settings,
        client_factory=client_factory or (lambda s: _FakeClient()),
        market_factory=lambda c, s: _FakeMarket(),
        scanner_factory=lambda a, s, r, eq: _FakeScanner(a, r),
        backtest_factory=lambda a, h: _FakeBacktestRunner(a, h),
        manager_factory=lambda s: _FakeAssetManager(assets),
        # A fresh Session per job, mirroring the real worker threads.
        repo_factory=lambda: Repository(session=maker()),
        **kw,
    )


def _read(maker) -> Repository:
    """A session for assertions, on the test's own thread."""
    return Repository(session=maker())


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until true; returns False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# Live session
# --------------------------------------------------------------------------- #
def test_live_session_starts_and_stops(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)
    assert jobs.is_live_running() is True
    assert jobs.live_state()["assets"] == ["TEST"]

    result = jobs.stop_live(timeout=5)
    assert result["ok"] is True
    assert jobs.live_state()["state"] == LIVE_STOPPED
    assert jobs.is_live_running() is False
    assert jobs.live_state()["stopped_at_utc"] is not None


def test_double_start_is_refused(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)

    second = jobs.start_live()
    assert second["ok"] is False
    assert second["reason"] == "already_running"
    jobs.stop_live(timeout=5)


def test_stop_when_idle_reports_not_running(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    result = _jobs(settings, maker).stop_live(timeout=1)
    assert result["ok"] is False
    assert result["reason"] == "not_running"


def test_mt5_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """A closed terminal must surface as LIVE_ERROR, never as a crash."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker, client_factory=lambda s: _BrokenClient())

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_ERROR)
    assert "MT5 terminal not running" in jobs.live_state()["last_error"]
    assert jobs.is_live_running() is False


def test_no_enabled_assets_is_an_error(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker, assets=())

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_ERROR)
    assert "No enabled assets" in jobs.live_state()["last_error"]


def test_status_is_readable_while_a_session_runs(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)

    snapshot = jobs.status()
    assert snapshot["live_running"] is True
    assert set(snapshot) == {"live", "backtest", "live_running"}
    assert snapshot["live"]["state"] == LIVE_RUNNING
    jobs.stop_live(timeout=5)


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def test_backtest_runs_when_idle(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)

    assert jobs.request_backtest()["ok"] is True
    state = jobs.wait_for_backtest(timeout=5)

    assert state["state"] == BT_DONE
    assert state["last_backtest_id"] is not None
    assert _read(maker).count_backtests() == 1
    assert _FakeBacktestRunner.runs == [("TEST", 720)]


def test_backtest_is_queued_while_live_and_runs_after_stop(tmp_path, monkeypatch):
    """The queue rule: MT5 is never driven by two jobs at once."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)

    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)

    queued = jobs.request_backtest()
    assert queued["ok"] is True
    assert queued["queued"] is True
    assert jobs.backtest_state()["state"] == BT_QUEUED
    assert _FakeBacktestRunner.runs == []          # nothing ran yet
    assert _read(maker).count_backtests() == 0

    jobs.stop_live(timeout=5)

    assert _wait_for(lambda: jobs.backtest_state()["state"] == BT_DONE)
    assert _FakeBacktestRunner.runs == [("TEST", 720)]
    assert _read(maker).count_backtests() == 1


def test_backtest_for_a_single_asset(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    other = Asset(name="OTHER", broker_symbol="OTHER", enabled=True)
    jobs = _jobs(settings, maker, assets=(ASSET, other))

    jobs.request_backtest(asset="OTHER", bars=500, max_hold_m1=30)
    state = jobs.wait_for_backtest(timeout=5)

    assert state["state"] == BT_DONE
    assert state["asset"] == "OTHER"
    assert _FakeBacktestRunner.runs == [("OTHER", 30)]


def test_multi_asset_backtest_shares_one_batch_id(tmp_path, monkeypatch):
    """One request over N assets is ONE campaign, not N unrelated rows.

    Without a shared id the run scatters into the history and the assets can
    never be compared against each other.
    """
    settings, maker = _env(tmp_path, monkeypatch)
    other = Asset(name="OTHER", broker_symbol="OTHER", enabled=True)
    jobs = _jobs(settings, maker, assets=(ASSET, other))

    jobs.request_backtest()
    state = jobs.wait_for_backtest(timeout=5)

    assert state["state"] == BT_DONE
    assert state["batch_id"] is not None

    repo = _read(maker)
    rows = repo.backtest_batch(state["batch_id"])
    assert {r.asset for r in rows} == {"TEST", "OTHER"}
    assert {r.batch_id for r in rows} == {state["batch_id"]}
    # Every row in the batch belongs to it -- none left ungrouped.
    assert all(r.batch_id is not None for r in repo.recent_backtests())


def test_separate_requests_get_separate_batch_ids(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)

    jobs.request_backtest()
    first = jobs.wait_for_backtest(timeout=5)["batch_id"]
    jobs.request_backtest()
    second = jobs.wait_for_backtest(timeout=5)["batch_id"]

    assert first and second and first != second
    # Each batch holds only its own run.
    repo = _read(maker)
    assert len(repo.backtest_batch(first)) == 1
    assert len(repo.backtest_batch(second)) == 1


def test_pre_batching_rows_still_surface_in_history(tmp_path, monkeypatch):
    """A row written before batching existed must not vanish from the list."""
    settings, maker = _env(tmp_path, monkeypatch)
    repo = _read(maker)
    legacy = repo.save_backtest(
        name="legacy", asset="OLD", symbol="OLD", batch_id=None,
        start_utc=datetime(2026, 1, 1), end_utc=datetime(2026, 1, 2),
        params={}, summary={"n_trades": 1, "total_r": 1.0}, trades=[])

    batches = repo.recent_batches()
    assert [b["batch_id"] for b in batches] == [f"single:{legacy.id}"]
    assert batches[0]["n_assets"] == 1


def test_backtest_parameters_reach_the_runner(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)

    jobs.request_backtest(bars=1234, max_hold_m1=99)
    state = jobs.wait_for_backtest(timeout=5)

    assert state["bars"] == 1234
    assert state["max_hold_m1"] == 99


def test_backtest_failure_is_reported(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)

    def _boom(asset, max_hold_m1):
        raise RuntimeError("no history for symbol")

    jobs = JobManager(
        settings=settings,
        client_factory=lambda s: _FakeClient(),
        market_factory=lambda c, s: _FakeMarket(),
        scanner_factory=lambda a, s, r, eq: _FakeScanner(a, r),
        backtest_factory=_boom,
        manager_factory=lambda s: _FakeAssetManager([ASSET]),
        repo_factory=lambda: Repository(session=maker()),
    )
    jobs.request_backtest()
    state = jobs.wait_for_backtest(timeout=5)

    assert state["state"] == BT_ERROR
    assert "no history for symbol" in state["last_error"]


def test_start_live_refused_while_backtest_runs(tmp_path, monkeypatch):
    """Live must not pre-empt an in-flight backtest (single MT5 owner)."""
    settings, maker = _env(tmp_path, monkeypatch)

    class _SlowRunner(_FakeBacktestRunner):
        def run(self, candles, name=""):
            time.sleep(0.4)
            return _FakeSummary(), []

    jobs = JobManager(
        settings=settings,
        client_factory=lambda s: _FakeClient(),
        market_factory=lambda c, s: _FakeMarket(),
        scanner_factory=lambda a, s, r, eq: _FakeScanner(a, r),
        backtest_factory=lambda a, h: _SlowRunner(a, h),
        manager_factory=lambda s: _FakeAssetManager([ASSET]),
        repo_factory=lambda: Repository(session=maker()),
    )
    jobs.request_backtest()
    assert _wait_for(lambda: jobs.backtest_state()["state"] in (BT_QUEUED, "starting",
                                                               "running"), timeout=2)

    refused = jobs.start_live()
    assert refused["ok"] is False
    assert refused["reason"] == "backtest_running"
    jobs.wait_for_backtest(timeout=5)


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #
def test_shutdown_stops_a_live_session(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)

    jobs.shutdown(timeout=5)
    assert jobs.is_live_running() is False
