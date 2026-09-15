"""Job manager tests: live sessions and backtests, with no MT5 anywhere.

Every collaborator is injected (client / market / scanner / asset manager /
backtest runner), so these tests exercise the real threading, locking and
queueing logic in ``runner.py`` without a terminal, network or clock dependency.
"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

import runner as runner_mod
from config import reload_settings
from database.models import Base
from database.repository import Repository, get_engine
from runner import (ACCOUNT_DONE, ACCOUNT_ERROR, ACCOUNT_IDLE, BROKER_DONE,
                    BROKER_ERROR, BT_DONE, BT_ERROR, BT_QUEUED, LIVE_ERROR,
                    LIVE_RUNNING, LIVE_STOPPED, JobManager)
from trading import time_utils as tu
from trading.asset_manager import Asset
from trading.bars import make_candle

ASSET = Asset(name="TEST", broker_symbol="TEST", enabled=True)


def _m1(index: int):
    """One synthetic M1 candle, ``index`` minutes after a fixed UTC epoch."""
    t = datetime(2026, 1, 1, 12, 0) + timedelta(minutes=index)
    return make_candle(t_utc=t, open_=100.0, high=101.0, low=99.0, close=100.5,
                       volume=1)


class _FakeClient:
    def __init__(self):
        self.connected = False
        self.range_calls: list[tuple] = []

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def account_info(self):
        return None

    def terminal_trade_allowed(self):
        """The terminal toolbar's Algo Trading switch.

        Defaults to ``True`` so a test that does not care about this gate is not
        accidentally exercising the "MT5 will reject orders" warning; the tests
        that do care override it.
        """
        return True

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        """Empty by default — the market fake supplies the candles."""
        self.range_calls.append((symbol, timeframe, date_from, date_to))
        return []


class _BrokenClient(_FakeClient):
    """Simulates a closed MT5 terminal."""

    def connect(self):
        raise RuntimeError("MT5 terminal not running")


class _FakeMarket:
    """Fake market data with a fixed M1 history.

    Returns real :class:`Candle` objects rather than empty lists, because the
    backtest worker skips an asset whose window came back empty (that is a
    "broker has no data here" result, not a zero-trade strategy result).
    """

    def __init__(self, candles=None, n_bars=0):
        self.polls = 0
        self.lookbacks: list[int] = []
        self._candles = (list(candles) if candles is not None
                         else [_m1(i) for i in range(3)])
        self.n_bars = n_bars
        # Recorded so tests can assert which window was actually requested.
        self.range_calls: list[tuple] = []
        self.pos_calls: list[tuple] = []

    def symbol_exists(self, symbol):
        return True

    def fetch_m1_closed(self, symbol, count, drop_forming=True):
        self.pos_calls.append((symbol, count))
        return list(self._candles)

    def fetch_m1_range(self, symbol, start_utc, end_utc, drop_forming=True):
        self.range_calls.append((symbol, start_utc, end_utc))
        return list(self._candles)

    def probe_m1_bounds(self, symbol):
        oldest = self._candles[0].t_utc if self._candles else None
        newest = self._candles[-1].t_utc if self._candles else None
        return {"symbol": symbol, "n_bars": self.n_bars,
                "oldest_utc": oldest, "newest_utc": newest}

    def poll_closed_candles(self, symbol, lookback=3):
        self.polls += 1
        # Recorded so a test can prove the loop widened its request after a
        # sleep rather than asking for the flat baseline and losing the gap.
        self.lookbacks.append(lookback)
        return []


class _FakeScanner:
    """Stands in for :class:`scanner.AssetScanner`."""

    #: Every instance built, so a test can reach the one a session is driving.
    instances: list = []
    #: What ``setup_states()`` reports. Overridden per test via monkeypatch.
    states: dict = {}

    def __init__(self, asset, repo):
        self.asset = asset
        self.symbol = asset.broker_symbol
        self.repo = repo
        self.warmed = None
        self.steps = 0
        _FakeScanner.instances.append(self)

    def warm(self, candles):
        self.warmed = len(candles)

    def feed_new(self, candles):
        self.steps += 1
        return 0

    def setup_states(self):
        return dict(self.states)

    def last_candle_time_utc(self):
        return datetime(2026, 1, 1, 12, 0)


class _ScannerWithNoStateReport:
    """A scanner that predates ``setup_states`` — it must not break a poll."""

    def __init__(self, asset, repo=None):
        self.asset = asset
        self.symbol = asset.broker_symbol

    def warm(self, candles):
        pass

    def feed_new(self, candles):
        return 0

    def last_candle_time_utc(self):
        return None


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

    The session-hours gate is switched **off** here. These tests drive the loop
    and assert what a poll produces, which only happens while the loop is awake —
    and with the gate on, whether it is awake depends on the wall clock the suite
    happens to run at. That would make every polling test pass on a weekday
    afternoon and fail overnight, which is the definition of a flaky test. The
    gate itself is covered deliberately and with a controlled clock by the
    ``_session_gate``/``_backfill_plan`` tests below.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runner.db'}")
    monkeypatch.setenv("SESSION_GATE_ENABLED", "false")
    # Settings is a snapshot, so the patched env only takes effect on a rebuild —
    # a cached one would still point at the previous test's file and gate.
    settings = reload_settings()
    engine = get_engine(settings)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return settings, maker


def _jobs(settings, maker, *, assets=(ASSET,), client_factory=None,
          market_factory=None, **kw):
    _FakeBacktestRunner.runs = []
    _FakeScanner.instances = []
    _FakeScanner.states = {}
    return JobManager(
        settings=settings,
        client_factory=client_factory or (lambda s: _FakeClient()),
        market_factory=market_factory or (lambda c, s: _FakeMarket()),
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
    assert set(snapshot) == {"live", "backtest", "probe", "broker", "account",
                             "live_running"}
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


# --------------------------------------------------------------------------- #
# The MT5 session
#
# Live, backtest and the history probe all share ``JobManager._mt5_session``.
# Releasing the terminal is the property that matters: MT5 is process-global, so
# a worker that keeps it initialised blocks every other job.
# --------------------------------------------------------------------------- #
def _recording_factory(built: list):
    def factory(_settings):
        client = _FakeClient()
        built.append(client)
        return client
    return factory


def test_the_terminal_is_released_when_a_session_stops(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    built: list = []
    jobs = _jobs(settings, maker, client_factory=_recording_factory(built))

    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)
    assert built and built[0].connected is True   # held while the session runs

    jobs.stop_live()
    assert built[0].connected is False            # ...and given back after


def test_the_terminal_is_released_when_a_worker_raises(tmp_path, monkeypatch):
    """The error path must release MT5 too, not just the clean one."""
    settings, maker = _env(tmp_path, monkeypatch)
    built: list = []

    class _ExplodingMarket(_FakeMarket):
        def fetch_m1_closed(self, *args, **kwargs):
            raise RuntimeError("history unavailable")

    jobs = JobManager(
        settings=settings,
        client_factory=_recording_factory(built),
        market_factory=lambda c, s: _ExplodingMarket(),
        scanner_factory=lambda a, s, r, eq: _FakeScanner(a, r),
        backtest_factory=lambda a, h: _FakeBacktestRunner(a, h),
        manager_factory=lambda s: _FakeAssetManager((ASSET,)),
        repo_factory=lambda: Repository(session=maker()),
    )
    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_ERROR)
    jobs.shutdown(timeout=5)

    assert built and built[0].connected is False
    assert "history unavailable" in jobs.live_state()["last_error"]


def test_a_terminal_that_cannot_connect_is_reported_not_raised(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker, client_factory=lambda s: _BrokenClient())

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_ERROR)
    assert "MT5 terminal not running" in jobs.live_state()["last_error"]


# --------------------------------------------------------------------------- #
# Broker symbol catalogue
# --------------------------------------------------------------------------- #
def _symbol(name, digits=2, contract=1.0, volume_min=0.01):
    return {"name": name, "digits": digits, "trade_contract_size": contract,
            "volume_min": volume_min, "volume_step": 0.01, "volume_max": 100.0,
            "visible": True, "trade_mode": 4}


class _CatalogClient(_FakeClient):
    """A terminal that offers a fixed symbol list."""

    def __init__(self, symbols=None, fail=False):
        super().__init__()
        # A list, always. A bare dict here would be iterated as its *keys*, so the
        # scan would return ``["name", "digits", ...]`` and die on the sort with an
        # opaque AttributeError instead of listing symbols.
        self._symbols = [_symbol("USTEC")] if symbols is None else list(symbols)
        self._fail = fail
        self.scans = 0

    def symbol_catalog(self):
        self.scans += 1
        if self._fail:
            raise RuntimeError("terminal refused the symbol list")
        return list(self._symbols)


def test_broker_scan_populates_the_catalogue(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    client = _CatalogClient([_symbol("XAUUSDm"), _symbol("EURUSDm", digits=5)])
    jobs = _jobs(settings, maker, client_factory=lambda s: client)

    assert jobs.request_broker_scan()["ok"] is True
    assert _wait_for(lambda: jobs.broker_state()["state"] == BROKER_DONE)

    assert jobs.broker_state()["n_symbols"] == 2
    # Sorted, so the browser table does not reshuffle between scans.
    assert [s["name"] for s in jobs.broker_catalog()] == ["EURUSDm", "XAUUSDm"]
    jobs.shutdown(timeout=5)


def test_the_status_summary_omits_the_catalogue_but_the_catalog_accessor_has_it(
        tmp_path, monkeypatch):
    """The list is hundreds of rows and must not ride on the five-second poll."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker, client_factory=lambda s: _CatalogClient())

    # The verdict is asserted, not discarded: a *refused* scan also leaves the
    # thread None, and joining None would report "idle" as if it were a hang.
    assert jobs.request_broker_scan()["ok"] is True
    # Joined rather than polled: the assertion below then reports the state the
    # scan actually reached, instead of timing out with no explanation.
    state = jobs.wait_for_broker_scan(timeout=5)
    assert state["state"] == BROKER_DONE, state

    assert "symbols" not in state
    assert "symbols" not in jobs.status()["broker"]
    assert jobs.broker_catalog()
    jobs.shutdown(timeout=5)


def test_broker_scan_is_refused_while_a_session_runs(tmp_path, monkeypatch):
    """Refused, not queued: waiting on the lock behind a live session looks like a hang."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)

    refused = jobs.request_broker_scan()
    assert refused["ok"] is False
    assert refused["reason"] == "live_running"
    jobs.stop_live(timeout=5)


def test_broker_scan_holds_the_terminal_open_only_for_its_own_work(tmp_path, monkeypatch):
    """The client is disconnected even when the scan fails — MT5 is process-global."""
    settings, maker = _env(tmp_path, monkeypatch)
    built: list = []

    def factory(_settings):
        client = _CatalogClient(fail=True)
        built.append(client)
        return client

    jobs = _jobs(settings, maker, client_factory=factory)
    jobs.request_broker_scan()
    assert _wait_for(lambda: jobs.broker_state()["state"] == BROKER_ERROR)

    assert built and built[0].connected is False
    assert "symbol list" in jobs.broker_state()["last_error"]
    jobs.shutdown(timeout=5)


def test_a_failed_rescan_keeps_the_catalogue_already_on_screen(tmp_path, monkeypatch):
    """Blanking the table the user is reading would be worse than stale rows."""
    settings, maker = _env(tmp_path, monkeypatch)
    state = {"fail": False}

    class _FlakyClient(_CatalogClient):
        def symbol_catalog(self):
            if state["fail"]:
                raise RuntimeError("terminal hiccup")
            return super().symbol_catalog()

    client = _FlakyClient([_symbol("USTEC"), _symbol("US500")])
    jobs = _jobs(settings, maker, client_factory=lambda s: client)

    jobs.request_broker_scan()
    assert _wait_for(lambda: jobs.broker_state()["state"] == BROKER_DONE)
    assert len(jobs.broker_catalog()) == 2

    state["fail"] = True
    jobs.request_broker_scan()
    assert _wait_for(lambda: jobs.broker_state()["state"] == BROKER_ERROR)

    assert len(jobs.broker_catalog()) == 2      # retained
    assert jobs.broker_state()["n_symbols"] == 2
    jobs.shutdown(timeout=5)


# --------------------------------------------------------------------------- #
# Account snapshot
# --------------------------------------------------------------------------- #
class _Account:
    """The shape ``MT5Client.account_info`` returns, without MT5."""

    def __init__(self, balance=10_000.0, equity=9_842.15, margin_free=9_842.15):
        self.login = 12345
        self.server = "Broker-Demo"
        self.name = "Test Account"
        self.currency = "USD"
        self.balance = balance
        self.equity = equity
        self.leverage = 100
        self.margin_free = margin_free


class _AccountClient(_FakeClient):
    """A terminal that reports an account."""

    def __init__(self, fail=False):
        super().__init__()
        self._summary = _Account()
        self._fail = fail
        self.reads = 0

    def account_info(self):
        self.reads += 1
        if self._fail:
            raise RuntimeError("terminal refused the account")
        return self._summary


class _NoAccountClient(_FakeClient):
    """A terminal that is connected but has no account to report (logged out)."""

    def account_info(self):
        return None


def test_account_refresh_publishes_balance_and_equity(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker, client_factory=lambda s: _AccountClient())

    # Nothing is read until it is asked for: the account is not on the status poll's
    # path, so an idle dashboard never opens the terminal.
    assert jobs.account_state()["balance"] is None
    assert jobs.account_state()["state"] == ACCOUNT_IDLE

    assert jobs.request_account_refresh()["ok"] is True
    state = jobs.wait_for_account(timeout=5)

    assert state["state"] == ACCOUNT_DONE
    assert state["balance"] == 10_000.0
    assert state["equity"] == 9_842.15
    assert state["currency"] == "USD"
    assert state["login"] == 12345
    assert state["fetched_at_utc"] is not None
    assert state["last_error"] == ""
    jobs.shutdown(timeout=5)


def test_account_refresh_is_refused_while_a_session_runs(tmp_path, monkeypatch):
    """A live session owns the terminal; it publishes its own, fresher snapshot."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker, client_factory=lambda s: _AccountClient())
    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)

    refused = jobs.request_account_refresh()
    assert refused["ok"] is False
    assert refused["reason"] == "live_running"
    jobs.stop_live(timeout=5)
    jobs.shutdown(timeout=5)


def test_the_live_session_snapshots_the_account_without_a_signal(tmp_path, monkeypatch):
    """The tile has to stay current through a quiet session, not only on signals."""
    settings, maker = _env(tmp_path, monkeypatch)
    client = _AccountClient()
    jobs = _jobs(settings, maker, client_factory=lambda s: client)

    jobs.start_live()
    # One read during warm-up; the poll loop must supply more of its own accord,
    # because the fake market never produces a signal and so never calls `equity`.
    assert _wait_for(lambda: jobs.account_state()["balance"] == 10_000.0)
    assert _wait_for(lambda: client.reads > 1)

    jobs.stop_live(timeout=5)
    jobs.shutdown(timeout=5)


def test_a_failed_account_read_keeps_the_figures_already_on_screen(tmp_path, monkeypatch):
    """Dropping the balance tile to a dash would read as "your account is gone"."""
    settings, maker = _env(tmp_path, monkeypatch)
    state = {"fail": False}

    class _FlakyClient(_AccountClient):
        def account_info(self):
            if state["fail"]:
                self.reads += 1
                raise RuntimeError("terminal hiccup")
            return super().account_info()

    jobs = _jobs(settings, maker, client_factory=lambda s: _FlakyClient())

    jobs.request_account_refresh()
    assert _wait_for(lambda: jobs.account_state()["state"] == ACCOUNT_DONE)

    state["fail"] = True
    jobs.request_account_refresh()
    assert _wait_for(lambda: jobs.account_state()["state"] == ACCOUNT_ERROR)

    assert jobs.account_state()["balance"] == 10_000.0        # retained
    assert "hiccup" in jobs.account_state()["last_error"]
    jobs.shutdown(timeout=5)


def test_account_refresh_holds_the_terminal_open_only_for_its_own_work(tmp_path, monkeypatch):
    """The client is disconnected even when the read fails — MT5 is process-global."""
    settings, maker = _env(tmp_path, monkeypatch)
    built: list = []

    def factory(_settings):
        client = _AccountClient(fail=True)
        built.append(client)
        return client

    jobs = _jobs(settings, maker, client_factory=factory)
    jobs.request_account_refresh()
    assert _wait_for(lambda: jobs.account_state()["state"] == ACCOUNT_ERROR)

    assert built and built[0].connected is False
    jobs.shutdown(timeout=5)


def test_an_account_the_terminal_will_not_name_is_an_error(tmp_path, monkeypatch):
    """A logged-out terminal answers with ``None``; that is not a zero balance."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker, client_factory=lambda s: _NoAccountClient())

    jobs.request_account_refresh()
    assert _wait_for(lambda: jobs.account_state()["state"] == ACCOUNT_ERROR)

    assert jobs.account_state()["balance"] is None
    assert "logged in" in jobs.account_state()["last_error"]
    jobs.shutdown(timeout=5)


# --------------------------------------------------------------------------- #
# Strategy visibility: the console sink and the published setup state
# --------------------------------------------------------------------------- #
def test_the_console_sink_reaches_the_scanner(tmp_path, monkeypatch):
    """The engine's decision lines must reach the live session's console."""
    settings, maker = _env(tmp_path, monkeypatch)
    seen = []
    jobs = _jobs(settings, maker, on_event=seen.append)

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: _FakeScanner.instances)
    scanner = _FakeScanner.instances[0]

    jobs._emit("[live] direct")
    scanner.on_event("[TEST] 1H sell-side liquidity purged")
    assert "[live] direct" in seen
    assert "[TEST] 1H sell-side liquidity purged" in seen
    jobs.stop_live(timeout=5)


def test_live_publishes_each_assets_setup_state(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    monkeypatch.setattr(_FakeScanner, "states",
                        {"buy": "WAITING_FOR_FVG_RETRACE", "sell": "NO_SETUP"})

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["setups"].get("TEST") == {
        "buy": "WAITING_FOR_FVG_RETRACE", "sell": "NO_SETUP"}), \
        jobs.live_state()["setups"]
    jobs.stop_live(timeout=5)


def test_setup_state_is_cleared_when_the_session_stops(tmp_path, monkeypatch):
    """A stopped session has no engine, so it must not show a live-looking state."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    monkeypatch.setattr(_FakeScanner, "states", {"buy": "FVG_FOUND", "sell": "NO_SETUP"})

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["setups"].get("TEST"))
    jobs.stop_live(timeout=5)
    assert jobs.live_state()["setups"] == {}


def test_setup_state_is_empty_before_a_session_runs(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    assert jobs.live_state()["setups"] == {}


def test_a_scanner_that_cannot_report_state_does_not_break_the_session(
        tmp_path, monkeypatch):
    """An older/injected scanner must not take down a live session."""
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = JobManager(
        settings=settings,
        client_factory=lambda s: _FakeClient(),
        market_factory=lambda c, s: _FakeMarket(),
        scanner_factory=lambda a, s, r, eq: _ScannerWithNoStateReport(a, r),
        backtest_factory=lambda a, h: _FakeBacktestRunner(a, h),
        manager_factory=lambda s: _FakeAssetManager([ASSET]),
        repo_factory=lambda: Repository(session=maker()),
    )

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)
    assert _wait_for(lambda: jobs.live_state()["setups"] == {"TEST": {}}), \
        jobs.live_state()["setups"]
    jobs.stop_live(timeout=5)
    assert jobs.live_state()["state"] == LIVE_STOPPED


# --------------------------------------------------------------------------- #
# Session-hours gate
#
# The loop stays up around the clock but only works inside a tradeable session
# on a trading day, so both the calendar and the clock have to be controlled
# here: ``_env`` switches the gate off precisely so the polling tests above do
# not depend on when the suite runs. These turn it back on.
# --------------------------------------------------------------------------- #
SAT_0300 = datetime(2026, 9, 12, 3, 0)     # Saturday — London's window, shut
MON_1000 = datetime(2026, 9, 14, 10, 0)    # Monday NY AM — open
MON_0600 = datetime(2026, 9, 14, 6, 0)     # Monday, between windows


class _Clock:
    """A hand-cranked NY clock, so the gate can be moved across a boundary.

    Patches ``time_utils.now_ny``/``now_utc`` — the one seam everything reads
    the time through — while leaving the real DST-aware conversions in place,
    which is what ``next_activity_start_utc`` walks with.
    """

    def __init__(self, ny_dt):
        self.ny = ny_dt

    def now_ny(self):
        return self.ny

    def now_utc(self):
        return tu.ny_to_utc(self.ny)

    def set(self, ny_dt):
        self.ny = ny_dt


#: Every session the spec marks tradeable, pinned explicitly. The gate reads
#: ``VALID_ENTRY_SESSIONS`` — an operator who excluded NY Premarket would
#: otherwise change what these tests observe through no fault of the code.
ALL_ENTRY_SESSIONS = ["london_open", "ny_premarket", "ny_am", "london_close",
                      "ny_pm"]


def _gated(settings, maker, market=None, sessions=None, **kw):
    """A JobManager with the gate ON, a pinned allow-list and a frozen clock."""
    market = market if market is not None else _FakeMarket()
    jobs = _jobs(replace(settings, session_gate_enabled=True,
                         valid_entry_sessions=list(sessions or ALL_ENTRY_SESSIONS)),
                 maker, market_factory=lambda c, s: market, **kw)
    return jobs, market


def test_session_gate_is_asleep_on_a_weekend(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    clock = _Clock(SAT_0300)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs, _ = _gated(settings, maker)

    active, activity, next_open = jobs._session_gate()

    assert (active, activity) == (False, "market_closed_weekend")
    # And it knows when it will be worth waking up: Monday 02:00 NY.
    assert tu.utc_to_ny(next_open) == datetime(2026, 9, 14, 2, 0)


def test_session_gate_is_awake_inside_a_trading_session(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    clock = _Clock(MON_1000)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs, _ = _gated(settings, maker)

    # No next-open to report while it is awake — there is nothing to wait for.
    assert jobs._session_gate() == (True, "ny_am", None)


def test_session_gate_reports_the_gap_between_windows(tmp_path, monkeypatch):
    """Asleep on a weekday, but for the other reason."""
    settings, maker = _env(tmp_path, monkeypatch)
    clock = _Clock(MON_0600)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs, _ = _gated(settings, maker)

    active, activity, next_open = jobs._session_gate()

    assert (active, activity) == (False, "outside_session")
    assert tu.utc_to_ny(next_open) == datetime(2026, 9, 14, 7, 0)


def test_the_gate_respects_the_entry_allowlist(tmp_path, monkeypatch):
    """A session the operator excluded is not worth waking up for.

    NY Premarket is conditional but tradeable, so 07:00 would normally be the
    next open from 06:00. Excluded from ``VALID_ENTRY_SESSIONS`` it can never
    enter anything, so the gate should sleep through it to NY AM.
    """
    settings, maker = _env(tmp_path, monkeypatch)
    clock = _Clock(MON_0600)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs, _ = _gated(settings, maker,
                     sessions=["london_open", "ny_am", "london_close", "ny_pm"])

    active, activity, next_open = jobs._session_gate()

    assert (active, activity) == (False, "outside_session")
    assert tu.utc_to_ny(next_open) == datetime(2026, 9, 14, 9, 30)


def test_the_gate_can_be_switched_off(tmp_path, monkeypatch):
    """``SESSION_GATE_ENABLED=false`` restores always-on scanning exactly."""
    settings, maker = _env(tmp_path, monkeypatch)     # gate already off
    clock = _Clock(SAT_0300)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs = _jobs(settings, maker)

    assert jobs._session_gate() == (True, "gate_disabled", None)


def test_backfill_plan_widens_the_request_to_span_a_sleep(tmp_path, monkeypatch):
    """The fix for the hole: a pause must not silently drop candles.

    ``_FakeScanner`` reports its last candle at a fixed instant, so the elapsed
    gap is exactly the frozen clock minus that instant.
    """
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    sc = _FakeScanner(ASSET, None)                 # last candle 2026-01-01 12:00

    monkeypatch.setattr(tu, "now_utc", lambda: datetime(2026, 1, 1, 12, 30))
    assert jobs._backfill_plan(sc) == (30 + 5, 0)  # the gap, plus the base slack


def test_backfill_plan_caps_and_reports_an_unfillable_gap(tmp_path, monkeypatch):
    """A gap too large to fetch in one request is truncated *and* reported.

    Silently asking for less than the gap would leave a hole in the M1 stream,
    and every level the model trades is rebuilt from that stream — so the
    shortfall comes back to the caller to log rather than being swallowed.
    """
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    sc = _FakeScanner(ASSET, None)
    cap = settings.warmup_m1_bars

    monkeypatch.setattr(tu, "now_utc", lambda: datetime(2026, 1, 10, 12, 0))
    lookback, missing = jobs._backfill_plan(sc)

    assert lookback == cap
    assert missing == (9 * 24 * 60) + 5 - cap


def test_backfill_plan_with_no_history_asks_for_the_baseline(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    jobs = _jobs(settings, maker)
    sc = _ScannerWithNoStateReport(ASSET, None)    # reports no last candle

    assert jobs._backfill_plan(sc) == (runner_mod.POLL_LOOKBACK_BASE, 0)


def test_the_live_loop_does_not_poll_while_it_is_asleep(tmp_path, monkeypatch):
    """A session that is running but asleep must touch MT5 not at all."""
    settings, maker = _env(tmp_path, monkeypatch)
    clock = _Clock(SAT_0300)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs, market = _gated(settings, maker)

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)
    assert _wait_for(lambda: jobs.live_state()["activity"] == "market_closed_weekend")

    live = jobs.live_state()
    assert live["active"] is False
    assert live["next_open_utc"] == tu.ny_to_utc(datetime(2026, 9, 14, 2, 0))
    # Warm-up still happened — the session is ready, it is just not working.
    assert live["assets"] == ["TEST"]
    assert market.polls == 0
    assert market.lookbacks == []

    jobs.stop_live(timeout=5)
    assert jobs.live_state()["active"] is False
    assert jobs.live_state()["next_open_utc"] is None


def test_the_live_loop_wakes_and_backfills_the_m1_gap(tmp_path, monkeypatch):
    """Waking must ask for the whole gap, not the flat 5-bar baseline."""
    settings, maker = _env(tmp_path, monkeypatch)
    # The real cap is five minutes, which would make this test wait five
    # minutes for the loop to notice the clock moved.
    monkeypatch.setattr(runner_mod, "SESSION_SLEEP_CAP_SECONDS", 0.05)
    clock = _Clock(SAT_0300)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs, market = _gated(settings, maker)

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: jobs.live_state()["active"] is False)
    assert market.polls == 0

    clock.set(MON_1000)                      # Monday: the session opens

    assert _wait_for(lambda: jobs.live_state()["active"] is True)
    assert _wait_for(lambda: market.polls > 0)
    assert jobs.live_state()["activity"] == "ny_am"
    # The frozen scanner's last candle is months behind the frozen clock, so the
    # request is capped at the warm-up depth — far beyond the 5-bar baseline.
    assert max(market.lookbacks) == settings.warmup_m1_bars > 5

    jobs.stop_live(timeout=5)


def test_a_truncated_backfill_is_logged_rather_than_silent(tmp_path, monkeypatch):
    settings, maker = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod, "SESSION_SLEEP_CAP_SECONDS", 0.05)
    clock = _Clock(SAT_0300)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    jobs, market = _gated(settings, maker)

    jobs.start_live()
    assert _wait_for(lambda: jobs.live_state()["active"] is False)
    clock.set(MON_1000)
    assert _wait_for(lambda: market.polls > 0)
    jobs.stop_live(timeout=5)

    with Repository(session=maker()) as repo:
        warnings = [e.message for e in repo.recent_events(limit=200)
                    if e.level == "WARN" and "backfill" in e.message]
    assert warnings, "the truncated backfill was not reported"
    assert "missing from the stream" in warnings[0]


def test_the_live_loop_emits_a_session_line_on_each_transition(tmp_path, monkeypatch):
    """The operator's ``[SESSION] ...`` wording, awake and asleep alike."""
    settings, maker = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod, "SESSION_SLEEP_CAP_SECONDS", 0.02)
    clock = _Clock(SAT_0300)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    seen: list[str] = []
    jobs, _ = _gated(settings, maker, on_event=seen.append)

    assert jobs.start_live()["ok"] is True
    assert _wait_for(lambda: "[SESSION] Saturday — Weekend mode" in seen)

    clock.set(MON_1000)                      # Monday, inside NY AM
    assert _wait_for(
        lambda: "[SESSION] NY AM active — Strategy scanner ON" in seen)
    jobs.stop_live(timeout=5)


def test_a_repeated_session_state_is_logged_once(tmp_path, monkeypatch):
    """Suppression is by rendered line, not by an awake/asleep flag.

    The loop re-evaluates the gate on a short cap here, so it renders the same
    line many times over. Only the first is a transition worth recording;
    repeating it every few seconds would bury the lines that matter.
    """
    settings, maker = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod, "SESSION_SLEEP_CAP_SECONDS", 0.02)
    clock = _Clock(SAT_0300)
    monkeypatch.setattr(tu, "now_ny", clock.now_ny)
    monkeypatch.setattr(tu, "now_utc", clock.now_utc)
    seen: list[str] = []
    jobs, _ = _gated(settings, maker, on_event=seen.append)

    jobs.start_live()
    assert _wait_for(lambda: "[SESSION] Saturday — Weekend mode" in seen)
    # Several more gate evaluations happen before this returns.
    assert _wait_for(lambda: jobs.live_state()["state"] == LIVE_RUNNING)
    jobs.stop_live(timeout=5)

    weekend_lines = [s for s in seen if s == "[SESSION] Saturday — Weekend mode"]
    assert len(weekend_lines) == 1, seen

