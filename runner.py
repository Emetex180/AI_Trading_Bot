"""Shared background job runner: live scanning sessions and backtests.

This module owns the **only** background threads in the project. It is
deliberately transport-agnostic — it must never import Flask — so the CLI
(``run.py``) and the dashboard (``app/api.py``) drive the *same* implementation
of "run a live session" and "run a backtest" rather than two that drift apart.

Threading contract
------------------
The ``MetaTrader5`` package is process-global: ``MT5Client.connect()`` calls
``mt5.initialize()`` and ``disconnect()`` calls ``mt5.shutdown()``. Two rules
follow, and both matter:

1. **All MT5 work happens inside a worker thread.** The client is constructed in
   the thread, used only there, and disconnected in a ``finally``. The Flask
   request thread must never build or call an ``MT5Client`` — that is what keeps
   :meth:`JobManager.status` safe to poll while the terminal is closed.
2. **One lock serialises MT5-owning jobs.** Live holds ``_mt5_lock`` for its
   whole life; the backtest worker takes it before touching history. Combined
   with the queue rule below, a backtest can never run concurrently with live.

Because only one MT5 job may run at a time, a backtest requested during a live
session is **queued** and launched automatically when the session stops.

Safety posture is unchanged: signals flow through :class:`scanner.AssetScanner`
into the existing :class:`trading.executor.Executor`, which records ``SKIPPED``
whenever ``AUTO_TRADING`` is off. Nothing here can enable trading.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from config import Settings, get_settings
from database.repository import Repository, init_db
from trading import time_utils as tu
from trading.instrument import prepare_asset

# Live session states.
LIVE_IDLE = "idle"
LIVE_STARTING = "starting"
LIVE_RUNNING = "running"
LIVE_STOPPING = "stopping"
LIVE_STOPPED = "stopped"
LIVE_ERROR = "error"

# Backtest states.
BT_IDLE = "idle"
BT_STARTING = "starting"
BT_QUEUED = "queued"
BT_RUNNING = "running"
BT_DONE = "done"
BT_ERROR = "error"

# History-probe states (read-only "what data does the broker hold?").
PROBE_IDLE = "idle"
PROBE_RUNNING = "running"
PROBE_DONE = "done"
PROBE_ERROR = "error"

# Seconds to wait for a live thread to unwind on stop before reporting a timeout.
STOP_TIMEOUT_SECONDS = 30.0


def _default_max_hold_m1() -> int:
    from backtesting.engine import DEFAULT_MAX_HOLD_M1

    return DEFAULT_MAX_HOLD_M1


# --------------------------------------------------------------------------- #
# Default collaborator factories (lazy so importing this module stays cheap and
# so tests can inject fakes without MT5 anywhere in the import graph).
# --------------------------------------------------------------------------- #
def _default_client_factory(settings: Settings):
    from trading.mt5_client import MT5Client

    return MT5Client(settings)


def _default_market_factory(client, settings: Settings):
    from trading.market_data import MarketData

    return MarketData(client, settings.mt5_server_utc_offset)


def _default_scanner_factory(asset, settings: Settings, repo: Repository,
                             equity_provider):
    from scanner import AssetScanner

    return AssetScanner(asset, settings=settings, repo=repo,
                        equity_provider=equity_provider)


def _default_backtest_factory(asset, max_hold_m1: int):
    from backtesting.engine import BacktestRunner

    return BacktestRunner(asset, max_hold_m1=max_hold_m1)


# --------------------------------------------------------------------------- #
# Job state
#
# Dataclasses rather than dicts: a misspelled field is now an immediate
# ``TypeError``/``AttributeError`` at the typo, instead of a silently-created
# key that the dashboard dutifully renders as blank. ``asdict`` is what the HTTP
# layer serialises, so the JSON the browser receives is byte-for-byte unchanged.
# --------------------------------------------------------------------------- #
@dataclass
class LiveState:
    state: str = LIVE_IDLE
    started_at_utc: datetime | None = None
    stopped_at_utc: datetime | None = None
    assets: list[str] = field(default_factory=list)
    last_candle_utc: datetime | None = None
    signals_session: int = 0
    last_error: str = ""


@dataclass
class BacktestState:
    state: str = BT_IDLE
    asset: str | None = None
    # Which selector the run used: "range" (explicit dates) or "bars" (the last
    # N candles). Reported back so the UI can describe the window.
    mode: str = "bars"
    bars: int | None = None
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    max_hold_m1: int | None = None
    # Groups every per-asset row one request produces. Without it a multi-asset
    # run is N unrelated rows and cannot be ranked.
    batch_id: str | None = None
    queued: bool = False
    queued_reason: str = ""
    progress_done: int = 0
    progress_total: int = 0
    last_backtest_id: int | None = None
    last_error: str = ""
    finished_at_utc: datetime | None = None


@dataclass
class ProbeState:
    """State for the read-only "what history do you have?" job."""

    state: str = PROBE_IDLE
    asset: str | None = None
    symbol: str | None = None
    n_bars: int = 0
    oldest_utc: datetime | None = None
    newest_utc: datetime | None = None
    last_error: str = ""
    finished_at_utc: datetime | None = None


class JobManager:
    """Start/stop the live scanner and run backtests off the request thread."""

    def __init__(self, settings: Settings | None = None, *,
                 on_event: Callable[[str], None] | None = None,
                 client_factory: Callable | None = None,
                 market_factory: Callable | None = None,
                 scanner_factory: Callable | None = None,
                 backtest_factory: Callable | None = None,
                 manager_factory: Callable | None = None,
                 repo_factory: Callable | None = None):
        self.settings = settings or get_settings()
        self.on_event = on_event
        self._client_factory = client_factory or _default_client_factory
        self._market_factory = market_factory or _default_market_factory
        self._scanner_factory = scanner_factory or _default_scanner_factory
        self._backtest_factory = backtest_factory or _default_backtest_factory
        self._manager_factory = manager_factory or (lambda cfg: _asset_manager(cfg))
        self._repo_factory = repo_factory or (lambda: Repository(settings=self.settings))

        # Guards the two state dicts so `status()` never reads a half-written job.
        self._state_lock = threading.RLock()
        # Serialises MT5-owning jobs (see the module docstring).
        self._mt5_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._live_thread: threading.Thread | None = None
        self._bt_thread: threading.Thread | None = None
        self._probe_thread: threading.Thread | None = None
        self._pending_backtest: dict[str, Any] | None = None

        self._live = LiveState()
        self._backtest = BacktestState()
        self._probe = ProbeState()

    # ------------------------------------------------------------------ #
    # State access (no MT5, no DB — safe to call from any thread)
    #
    # Each accessor returns a detached ``dict`` so callers (and the HTTP layer)
    # can hold it without the worker mutating it underneath them.
    # ------------------------------------------------------------------ #
    def live_state(self) -> dict[str, Any]:
        with self._state_lock:
            return asdict(self._live)

    def backtest_state(self) -> dict[str, Any]:
        with self._state_lock:
            return asdict(self._backtest)

    def probe_state(self) -> dict[str, Any]:
        with self._state_lock:
            return asdict(self._probe)

    def is_live_running(self) -> bool:
        with self._state_lock:
            thread = self._live_thread
            if thread is not None and thread.is_alive():
                return True
            return self._live.state in (LIVE_STARTING, LIVE_RUNNING, LIVE_STOPPING)

    def status(self) -> dict[str, Any]:
        return {
            "live": self.live_state(),
            "backtest": self.backtest_state(),
            "probe": self.probe_state(),
            "live_running": self.is_live_running(),
        }

    # ------------------------------------------------------------------ #
    # Live session
    # ------------------------------------------------------------------ #
    def start_live(self) -> dict[str, Any]:
        """Spawn the live scanner thread. Returns ``{"ok": bool, ...}``."""
        with self._state_lock:
            if self.is_live_running():
                return {"ok": False, "reason": "already_running",
                        "message": "A live session is already running.",
                        "live": self.live_state()}
            if self._backtest.state in (BT_STARTING, BT_RUNNING):
                return {"ok": False, "reason": "backtest_running",
                        "message": "A backtest is running — wait for it to finish.",
                        "live": self.live_state()}

            self._stop_event = threading.Event()
            self._live = LiveState(state=LIVE_STARTING, started_at_utc=tu.now_utc())
            thread = threading.Thread(target=self._live_worker, name="live-scanner",
                                      daemon=True)
            self._live_thread = thread

        thread.start()
        return {"ok": True, "message": "Live session starting.",
                "live": self.live_state()}

    def stop_live(self, timeout: float = STOP_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Signal the live thread to stop and wait for it to unwind."""
        with self._state_lock:
            thread = self._live_thread
            if thread is None or not thread.is_alive():
                return {"ok": False, "reason": "not_running",
                        "message": "No live session is running.",
                        "live": self.live_state()}
            self._stop_event.set()
            self._live.state = LIVE_STOPPING

        # Join outside the lock: the worker takes it to publish progress.
        thread.join(timeout)

        if thread.is_alive():
            return {"ok": False, "reason": "stop_timeout",
                    "message": "Live session did not stop in time.",
                    "live": self.live_state()}

        self._start_pending_backtest()
        return {"ok": True, "message": "Live session stopped.",
                "live": self.live_state()}

    @contextmanager
    def _mt5_session(self):
        """Own the MT5 terminal for the duration of the block.

        The three jobs that touch the terminal (live, backtest, history probe)
        all need the same four things, and getting any of them wrong is how a
        session ends up half-initialised: take ``_mt5_lock``, build the client
        *inside* the worker thread that will use it, connect, and guarantee a
        disconnect. Yields ``(client, market)``.

        A failing ``disconnect`` is swallowed deliberately — it must not replace
        the exception that actually ended the job.
        """
        with self._mt5_lock:
            client = self._client_factory(self.settings)
            client.connect()
            try:
                yield client, self._market_factory(client, self.settings)
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass

    def _live_worker(self) -> None:
        repo: Repository | None = None
        error = ""
        try:
            manager = self._manager_factory(self.settings)
            assets = manager.enabled_assets()
            if not assets:
                raise RuntimeError(
                    f"No enabled assets in the registry ({self.settings.assets_file}).")

            # Everything MT5 happens inside this thread and this session.
            with self._mt5_session() as (client, market):
                repo = self._repo_factory()
                self._log(repo, "INFO", "scanner",
                          f"starting; auto_trading={self.settings.auto_trading}")

                def equity():
                    acc = client.account_info()
                    return acc.equity if acc else None

                warm_count = self.settings.warmup_m1_bars
                scanners: dict[str, Any] = {}
                for asset in assets:
                    if self._stop_event.is_set():
                        break
                    resolved, _spec = prepare_asset(
                        client, asset, self.settings.symbol_auto_resolve)
                    if resolved is None:
                        self._log(repo, "WARN", "scanner",
                                  f"{asset.name}: symbol {asset.broker_symbol} "
                                  "not offered by the broker")
                        self._emit(f"[scan] {asset.name} skipped — "
                                   f"{asset.broker_symbol} not found at broker.")
                        continue
                    if resolved.broker_symbol != asset.broker_symbol:
                        self._log(repo, "INFO", "scanner",
                                  f"{asset.name}: {asset.broker_symbol} -> "
                                  f"{resolved.broker_symbol}")
                    warm = market.fetch_m1_closed(resolved.broker_symbol, warm_count,
                                                  drop_forming=True)
                    sc = self._scanner_factory(resolved, self.settings, repo, equity)
                    sc.warm(warm)
                    scanners[resolved.name] = sc
                    self._log(repo, "INFO", "scanner",
                              f"{resolved.name} warmed ({len(warm)} M1)")
                    self._emit(f"[scan] {resolved.name} ({resolved.broker_symbol}) "
                               f"warmed with {len(warm)} M1 candles.")

                with self._state_lock:
                    self._live.assets = list(scanners)
                    self._live.state = LIVE_RUNNING

                self._emit(f"[scan] LIVE — AUTO_TRADING={self.settings.auto_trading}.")

                poll = self.settings.scanner_poll_interval_ms / 1000.0
                while not self._stop_event.is_set():
                    for name, sc in scanners.items():
                        candles = market.poll_closed_candles(sc.symbol, lookback=5)
                        handled = sc.feed_new(candles)
                        last = sc.last_candle_time_utc()
                        with self._state_lock:
                            self._live.last_candle_utc = last
                            if handled:
                                self._live.signals_session += handled
                        if handled:
                            self._log(repo, "INFO", "scanner",
                                      f"{name}: {handled} new signal(s)")
                    # Wait on the event so Stop takes effect immediately rather
                    # than after the full poll interval.
                    self._stop_event.wait(poll)

                self._log(repo, "INFO", "scanner", "stopped")
        except Exception as exc:  # surfaced in the dashboard, never fatal
            error = f"{type(exc).__name__}: {exc}"
            self._emit(f"[scan] stopped: {error}")
            if repo is not None:
                self._log(repo, "ERROR", "scanner", error)
        finally:
            if repo is not None:
                repo.close()
            with self._state_lock:
                self._live.stopped_at_utc = tu.now_utc()
                self._live.last_error = error
                self._live.state = LIVE_ERROR if error else LIVE_STOPPED

    # ------------------------------------------------------------------ #
    # Backtest
    # ------------------------------------------------------------------ #
    def request_backtest(self, *, asset: str | None = None,
                         bars: int | None = None,
                         start_utc: datetime | None = None,
                         end_utc: datetime | None = None,
                         max_hold_m1: int | None = None) -> dict[str, Any]:
        """Run a backtest now, or queue it if a live session holds MT5.

        Two mutually exclusive selectors: an explicit ``start_utc``/``end_utc``
        window, or ``bars`` (the most recent N M1 candles). A window wins when
        both are supplied, because it is the more specific request.
        """
        use_range = start_utc is not None and end_utc is not None
        req = {
            "asset": (asset or "").strip() or None,
            "mode": "range" if use_range else "bars",
            "bars": None if use_range else int(bars or self.settings.backtest_m1_bars),
            "start_utc": start_utc if use_range else None,
            "end_utc": end_utc if use_range else None,
            "max_hold_m1": int(max_hold_m1 or _default_max_hold_m1()),
            # One id for the whole campaign. Every asset in this request shares
            # it, which is what allows a cross-asset comparison afterwards.
            "batch_id": uuid4().hex,
        }
        with self._state_lock:
            if self._backtest.state in (BT_STARTING, BT_RUNNING):
                return {"ok": False, "reason": "already_running",
                        "message": "A backtest is already running.",
                        "backtest": self.backtest_state()}
            if self.is_live_running():
                self._pending_backtest = req
                self._backtest = BacktestState(
                    state=BT_QUEUED, queued=True,
                    queued_reason="A live session is running — the backtest will "
                                  "start when you stop it.",
                    **req)
                return {"ok": True, "queued": True,
                        "message": "Backtest queued until the live session stops.",
                        "backtest": self.backtest_state()}

        self._spawn_backtest(req)
        return {"ok": True, "queued": False, "message": "Backtest starting.",
                "backtest": self.backtest_state()}

    def _start_pending_backtest(self) -> None:
        with self._state_lock:
            req = self._pending_backtest
            self._pending_backtest = None
        if req is not None:
            self._spawn_backtest(req)

    def _spawn_backtest(self, req: dict[str, Any]) -> None:
        with self._state_lock:
            self._backtest = BacktestState(state=BT_STARTING, **req)
            thread = threading.Thread(target=self._backtest_worker, args=(req,),
                                      name="backtest", daemon=True)
            self._bt_thread = thread
        thread.start()

    def _backtest_worker(self, req: dict[str, Any]) -> None:
        repo: Repository | None = None
        error = ""
        try:
            manager = self._manager_factory(self.settings)
            if req["asset"]:
                assets = [manager.get(req["asset"])]
            else:
                assets = manager.enabled_assets()
            if not assets:
                raise RuntimeError("No enabled assets in the registry.")

            init_db(self.settings)
            repo = self._repo_factory()
            window = (f"{req['start_utc']}..{req['end_utc']}"
                      if req["mode"] == "range"
                      else f"last {req['bars']} M1")
            repo.log_event("INFO", "backtest",
                           f"starting over {window} per asset")

            with self._state_lock:
                self._backtest.state = BT_RUNNING
                self._backtest.progress_total = len(assets)

            with self._mt5_session() as (client, market):
                for index, asset in enumerate(assets, start=1):
                    resolved, _spec = prepare_asset(
                        client, asset, self.settings.symbol_auto_resolve)
                    if resolved is None:
                        repo.log_event("WARN", "backtest",
                                       f"{asset.name}: symbol not offered "
                                       "by the broker")
                        continue
                    if req["mode"] == "range":
                        candles = market.fetch_m1_range(
                            resolved.broker_symbol, req["start_utc"],
                            req["end_utc"], drop_forming=True)
                    else:
                        candles = market.fetch_m1_closed(
                            resolved.broker_symbol, req["bars"],
                            drop_forming=True)
                    if not candles:
                        # An empty replay would be stored as a zero-trade
                        # result, which reads as "the strategy found nothing"
                        # rather than "the broker had no bars here".
                        repo.log_event("WARN", "backtest",
                                       f"{resolved.name}: no M1 candles in the "
                                       "requested window")
                        self._emit(f"[backtest] {resolved.name}: no M1 data in "
                                   "the requested window")
                        continue
                    runner = self._backtest_factory(resolved, req["max_hold_m1"])
                    summary, trades = runner.run(candles, name=resolved.name)
                    row = repo.save_backtest(
                        name=resolved.name,
                        asset=resolved.name,
                        symbol=resolved.broker_symbol,
                        batch_id=req["batch_id"],
                        start_utc=summary.start_utc,
                        end_utc=summary.end_utc,
                        params=summary.params,
                        summary=summary.to_dict(),
                        trades=[t.as_db_dict() for t in trades],
                    )
                    d = summary.to_dict()
                    self._emit(
                        f"[backtest] {resolved.name}: {d['n_signals']} signals, "
                        f"{d['n_trades']} trades, win {d['win_rate']:.1%}, "
                        f"PF {d['profit_factor']:.2f}, total R {d['total_r']:.2f}")
                    with self._state_lock:
                        self._backtest.progress_done = index
                        self._backtest.last_backtest_id = row.id
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._emit(f"[backtest] failed: {error}")
            if repo is not None:
                repo.log_event("ERROR", "backtest", error)
        finally:
            if repo is not None:
                repo.close()
            with self._state_lock:
                self._backtest.last_error = error
                self._backtest.queued = False
                self._backtest.queued_reason = ""
                self._backtest.finished_at_utc = tu.now_utc()
                self._backtest.state = BT_ERROR if error else BT_DONE

    # ------------------------------------------------------------------ #
    # History probe (read-only)
    # ------------------------------------------------------------------ #
    def request_data_probe(self, asset: str) -> dict[str, Any]:
        """Ask the broker what M1 history it holds for ``asset``.

        Read-only, but it still owns MT5, so it runs on its own thread under
        ``_mt5_lock`` (module docstring). Unlike a backtest it is **refused**
        rather than queued while another MT5 job is running: the user is waiting
        on the answer, and parking the request until a session ends would just
        look like a hang.
        """
        name = (asset or "").strip()
        with self._state_lock:
            if self.is_live_running():
                return {"ok": False, "reason": "live_running",
                        "message": "A live session owns the MT5 terminal. "
                                   "Stop it to inspect history.",
                        "probe": self.probe_state()}
            if self._backtest.state in (BT_STARTING, BT_RUNNING):
                return {"ok": False, "reason": "backtest_running",
                        "message": "A backtest is running. Wait for it to finish.",
                        "probe": self.probe_state()}
            if self._probe.state == PROBE_RUNNING:
                return {"ok": False, "reason": "already_running",
                        "message": "Already checking history.",
                        "probe": self.probe_state()}

            self._probe = ProbeState(state=PROBE_RUNNING, asset=name)
            thread = threading.Thread(target=self._probe_worker, args=(name,),
                                      name="history-probe", daemon=True)
            self._probe_thread = thread

        thread.start()
        return {"ok": True, "message": "Checking available history…",
                "probe": self.probe_state()}

    def _probe_worker(self, asset_name: str) -> None:
        error = ""
        try:
            manager = self._manager_factory(self.settings)
            asset = manager.get(asset_name)
            with self._mt5_session() as (client, market):
                resolved, _spec = prepare_asset(
                    client, asset, self.settings.symbol_auto_resolve)
                if resolved is None:
                    raise RuntimeError(
                        f"Broker does not offer {asset.broker_symbol!r} "
                        f"(for {asset.name}).")
                bounds = market.probe_m1_bounds(resolved.broker_symbol)
                with self._state_lock:
                    self._probe.symbol = resolved.broker_symbol
                    self._probe.n_bars = bounds["n_bars"]
                    self._probe.oldest_utc = bounds["oldest_utc"]
                    self._probe.newest_utc = bounds["newest_utc"]
        except Exception as exc:  # surfaced in the dashboard, never fatal
            error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._state_lock:
                self._probe.last_error = error
                self._probe.finished_at_utc = tu.now_utc()
                self._probe.state = PROBE_ERROR if error else PROBE_DONE

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def shutdown(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Stop any running job (tests / interpreter exit)."""
        if self.is_live_running():
            self.stop_live(timeout=timeout)
        for thread in (self._bt_thread, self._probe_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout)

    def wait_for_backtest(self, timeout: float | None = None) -> dict[str, Any]:
        """Block until the current backtest finishes (for the synchronous CLI)."""
        thread = self._bt_thread
        if thread is not None:
            thread.join(timeout)
        return self.backtest_state()

    def wait_for_probe(self, timeout: float | None = None) -> dict[str, Any]:
        """Block until the current history probe finishes (tests / CLI)."""
        thread = self._probe_thread
        if thread is not None:
            thread.join(timeout)
        return self.probe_state()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _emit(self, message: str) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(message)
        except Exception:  # a broken console must not kill a live session
            pass

    @staticmethod
    def _log(repo: Repository, level: str, source: str, message: str) -> None:
        try:
            repo.log_event(level, source, message)
        except Exception:  # logging must never take down a session
            pass


def _asset_manager(settings: Settings):
    from trading.asset_manager import AssetManager

    return AssetManager(settings=settings)
