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

Safety posture: signals flow through :class:`scanner.AssetScanner` into the
existing :class:`trading.executor.Executor`, which records ``SKIPPED`` whenever
the master switch is off. :meth:`JobManager.set_auto_trading` can operate that
one switch, session-only (it is never written back to ``.env``); nothing here
can reach any other gate in the executor.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from config import Settings, get_settings
from database.repository import Repository, init_db
from trading import sessions as sess
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

# Broker-catalogue states (read-only "what symbols does the broker offer?").
BROKER_IDLE = "idle"
BROKER_RUNNING = "running"
BROKER_DONE = "done"
BROKER_ERROR = "error"

# Account-snapshot states (read-only "what does the terminal say my account is?").
ACCOUNT_IDLE = "idle"
ACCOUNT_RUNNING = "running"
ACCOUNT_DONE = "done"
ACCOUNT_ERROR = "error"

# Seconds to wait for a live thread to unwind on stop before reporting a timeout.
STOP_TIMEOUT_SECONDS = 30.0

#: M1 candles to request on an ordinary poll. Enough to cover a dropped tick
#: without over-fetching; ``AssetScanner.feed_new`` discards anything already
#: seen, so a larger request costs bandwidth but never correctness.
POLL_LOOKBACK_BASE = 5

#: Longest the live loop will sleep in one go while outside a session. The loop
#: otherwise sleeps until the next window opens, but ``Event.wait`` measures
#: elapsed time monotonically — an NTP step or a suspended VM is invisible to it,
#: and the loop would then wake late by however much the clock moved. Capping the
#: sleep means any such surprise self-corrects within five minutes, at the cost
#: of one no-op wake per five minutes.
SESSION_SLEEP_CAP_SECONDS = 300.0


def _default_max_hold_m1() -> int:
    from backtesting.engine import DEFAULT_MAX_HOLD_M1

    return DEFAULT_MAX_HOLD_M1


# --------------------------------------------------------------------------- #
# JSON shaping for the persisted engine state
#
# The state row stores these maps as JSON, so every value has to survive a
# round-trip. Datetimes do not, and the dashboard renders these without a
# timezone of their own, so they travel as ISO-8601 strings and are parsed back
# on read. Kept as module functions rather than inline lambdas so the writer and
# any reader agree on exactly one format.
# --------------------------------------------------------------------------- #
def _iso_or_none(value) -> str | None:
    """A datetime as an ISO-8601 string, or ``None``."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _jsonable_quote(quote: dict) -> dict:
    """One asset's quote with its timestamp rendered for JSON."""
    out = dict(quote or {})
    stamp = out.get("time_utc")
    out["time_utc"] = _iso_or_none(stamp)
    return out


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
    #: asset name -> {"buy": state, "sell": state}. The per-symbol setup state
    #: machine, published each poll so the dashboard can show where every asset
    #: actually is (waiting on an FVG retrace, purged, invalidated) instead of
    #: only reporting signals after the fact. Cleared when a session stops: the
    #: engine that produced it is gone, and a stale "waiting for retrace" would
    #: read as live.
    setups: dict[str, dict[str, str]] = field(default_factory=dict)
    #: asset name -> last *closed* M1 close, from the engine's own candle stream.
    #: Published alongside ``setups`` for the client dashboard's market table.
    #:
    #: A separate key rather than a field inside ``setups``: that dict's shape is
    #: ``{direction: state}`` and the console renders it as such, so widening it
    #: would change a published contract for no gain. Read-only — it is the
    #: engine's last seen price, never a quote fetched for display, so it is
    #: always the same price the strategy itself acted on. Absent for an asset
    #: whose stream has not seen a candle yet, which the UI renders as unknown
    #: rather than as zero.
    prices: dict[str, float] = field(default_factory=dict)
    #: asset name -> UTC close time of the candle ``prices`` came from, so a
    #: figure is never shown without its age.
    price_times: dict[str, datetime] = field(default_factory=dict)
    #: asset name -> the last quote read from the terminal, as
    #: ``{"bid", "ask", "spread", "spread_points", "digits", "time_utc"}``.
    #:
    #: Distinct from ``prices``, and deliberately so: ``prices`` is the last
    #: *closed* M1 close, which is the price the strategy itself acted on, while
    #: this is the live bid/ask. A reader comparing the two is looking at the
    #: difference between what the model decided on and what the market is doing
    #: now, so collapsing them into one field would destroy the distinction.
    #: Read from the same ``_mt5_session`` the engine already holds — never a
    #: second terminal connection.
    quotes: dict[str, dict] = field(default_factory=dict)
    #: Whether the session is *awake*. The live thread stays up around the clock
    #: but only polls inside a tradeable session on a trading day; this is False
    #: while it sleeps. A running-but-asleep session is still "running" for
    #: :meth:`JobManager.is_live_running`, because it is alive and will resume on
    #: its own — the dashboard distinguishes the two rather than guessing.
    active: bool = False
    #: Why it is awake or not. The session key that justified waking (``ny_am``),
    #: or ``market_closed_weekend`` / ``outside_session`` / ``gate_disabled``.
    #: Empty before the first evaluation.
    activity: str = ""
    #: UTC instant of the next window the loop will wake for. ``None`` while
    #: awake, and ``None`` if no window falls inside the search horizon.
    next_open_utc: datetime | None = None
    #: UTC instant the live loop last completed a pass — its in-process
    #: heartbeat. The persisted engine-state row carries its own copy for a
    #: dashboard in another process; this one exists so a dashboard *in this
    #: process* reports the same fact instead of having nothing to show. Left
    #: ``None`` until the first pass, which is the honest answer while a session
    #: is still warming up.
    heartbeat_utc: datetime | None = None
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


@dataclass
class BrokerState:
    """State for the read-only "what symbols does the broker offer?" job.

    Deliberately holds only the *summary* of the scan. The catalogue itself is
    hundreds of rows and lives in a plain attribute on the manager instead, so
    that ``asdict`` here stays cheap enough to serialise on every ``/api/status``
    poll — deep-copying a thousand dicts every five seconds to throw them away
    would be pure waste.
    """

    state: str = BROKER_IDLE
    n_symbols: int = 0
    last_error: str = ""
    finished_at_utc: datetime | None = None


@dataclass
class AccountState:
    """The most recent account snapshot for the dashboard balance tile.

    A *snapshot*, not a live value, and that is forced by the threading contract
    above: MT5 is process-global and single-owner, so the browser's five-second
    ``/api/status`` poll can never read the account itself. Instead whoever holds
    the terminal writes here — the live worker on every poll of its session, or a
    manual refresh on the dashboard — and the UI renders the figure together with
    :attr:`fetched_at_utc` so a stale number can never be mistaken for a current
    one.

    Every money field is ``None`` until the first successful read, which is
    deliberately distinct from a genuine zero balance.
    """

    state: str = ACCOUNT_IDLE
    balance: float | None = None
    equity: float | None = None
    margin_free: float | None = None
    currency: str | None = None
    login: int | None = None
    server: str | None = None
    name: str | None = None
    last_error: str = ""
    fetched_at_utc: datetime | None = None
    # The terminal's own Algo Trading button, read in the same MT5 session that
    # produced the figures above. ``None`` means "not known" (never read, or the
    # terminal could not be asked) — which is why it defaults to None rather
    # than False: the dashboard must not claim the terminal is blocking orders
    # when it simply has not checked.
    trade_allowed: bool | None = None


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
        #: Identifies *this* manager against the persisted engine-state row, so
        #: a start can tell "an engine I already run" from "an engine in another
        #: process". Random per construction: a restart is a new instance, which
        #: is exactly what the duplicate-engine guard needs to know.
        self._instance_id = uuid4().hex

        self._stop_event = threading.Event()
        self._live_thread: threading.Thread | None = None
        self._bt_thread: threading.Thread | None = None
        self._probe_thread: threading.Thread | None = None
        self._broker_thread: threading.Thread | None = None
        self._account_thread: threading.Thread | None = None
        self._pending_backtest: dict[str, Any] | None = None

        self._live = LiveState()
        self._backtest = BacktestState()
        self._probe = ProbeState()
        self._broker = BrokerState()
        self._account = AccountState()
        # The scanned symbol catalogue, kept out of BrokerState so no poll pays
        # to deep-copy it. Guarded by ``_state_lock`` like the state dataclasses.
        self._broker_symbols: list[dict[str, Any]] = []

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

    def broker_state(self) -> dict[str, Any]:
        """Summary of the last broker scan — safe to put on the status poll."""
        with self._state_lock:
            return asdict(self._broker)

    def broker_catalog(self) -> list[dict[str, Any]]:
        """The scanned broker symbol list.

        Separate from :meth:`broker_state` on purpose: this is hundreds of rows,
        so it is fetched on demand by the assets page rather than riding along on
        the five-second ``/api/status`` poll.
        """
        with self._state_lock:
            return list(self._broker_symbols)

    def account_state(self) -> dict[str, Any]:
        """The last account snapshot — small enough to ride the status poll."""
        with self._state_lock:
            return asdict(self._account)

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
            "broker": self.broker_state(),
            "account": self.account_state(),
            "live_running": self.is_live_running(),
        }

    # ------------------------------------------------------------------ #
    # Persisted engine state (the bridge to a dashboard in another process)
    # ------------------------------------------------------------------ #
    #: Slack added to a lease before it is treated as expired. Covers the time
    #: between the writer deciding its wait budget and actually sleeping, plus
    #: ordinary scheduler jitter. Generous rather than tight: reporting a live
    #: engine as dead is the failure that would actually mislead an operator.
    LEASE_SLACK_SECONDS = 20.0

    def _publish_engine_state(self, repo, *, lease_seconds: float) -> None:
        """Mirror the in-memory live state into the persisted singleton row.

        Called from the live worker only, on every pass of its loop. This is what
        lets a dashboard in a *different* process show a price, a setup state or
        a scanner badge at all — ``LiveState`` is otherwise confined to the
        scanner's own address space (see ``database.models.EngineState``).

        ``lease_seconds`` is how long the worker is about to sleep, recorded so
        the reader can tell a sleeping engine from a dead one without assuming a
        fixed poll interval.

        Best-effort by construction: a database problem must never take down a
        scanning session, exactly like the ``_log``/``_emit`` sinks it sits
        beside. The failure is surfaced once through ``_emit`` rather than
        swallowed, because a dashboard that has silently stopped updating is
        precisely the bug this method exists to fix.
        """
        try:
            with self._state_lock:
                live = asdict(self._live)
            repo.save_engine_state(
                instance_id=self._instance_id,
                pid=os.getpid(),
                state=live["state"],
                active=bool(live["active"]),
                activity=live["activity"] or "",
                assets=list(live["assets"]),
                setups=live["setups"],
                prices=live["prices"],
                price_times={k: _iso_or_none(v)
                             for k, v in live["price_times"].items()},
                quotes={k: _jsonable_quote(v) for k, v in live["quotes"].items()},
                last_candle_utc=live["last_candle_utc"],
                next_open_utc=live["next_open_utc"],
                signals_session=live["signals_session"],
                started_at_utc=live["started_at_utc"],
                stopped_at_utc=live["stopped_at_utc"],
                last_error=live["last_error"] or "",
                heartbeat_utc=tu.now_utc(),
                lease_seconds=float(lease_seconds),
            )
        except Exception as exc:  # a bridge failure must not stop the engine
            self._emit(f"[scan] engine-state publish failed: "
                       f"{type(exc).__name__}: {exc}")

    def engine_lease(self) -> dict[str, Any]:
        """The persisted engine state, with its lease already evaluated.

        Answers "is an engine running, and is it this one?" without assuming the
        caller shares a process with it. Never raises: a database that cannot be
        read reports "no engine", which is the safe direction — the dashboard
        then says it does not know rather than claiming a live feed it cannot see.

        The engine is alive while ``now - heartbeat_utc <= lease_seconds``. The
        lease is what the writer is *allowed* to sleep for, so a correctly idle
        engine outside a trading session stays alive across its long sleep while
        a crashed one expires.
        """
        try:
            repo = self._repo_factory()
        except Exception:
            return {"alive": False, "readable": False, "row": None,
                    "mine": False, "age_seconds": None}
        try:
            row = repo.load_engine_state()
        except Exception:
            return {"alive": False, "readable": False, "row": None,
                    "mine": False, "age_seconds": None}
        finally:
            try:
                repo.close()
            except Exception:
                pass

        if row is None:
            return {"alive": False, "readable": True, "row": None,
                    "mine": False, "age_seconds": None}

        heartbeat = row.heartbeat_utc
        age = ((tu.now_utc() - heartbeat).total_seconds()
               if heartbeat is not None else None)
        lease = float(row.lease_seconds or 0.0) + self.LEASE_SLACK_SECONDS
        # Two conditions, not one. The lease answers "has the heartbeat gone
        # quiet for longer than the writer said it would", which catches a
        # process that died without running its cleanup. The state answers "did
        # the engine say it was finished", which catches an orderly stop
        # immediately — otherwise a stopped session would keep reading as alive
        # for the whole slack window.
        alive = (age is not None and age <= lease
                 and row.state in (LIVE_STARTING, LIVE_RUNNING, LIVE_STOPPING))
        return {
            "alive": alive,
            "readable": True,
            "mine": row.instance_id == self._instance_id,
            "age_seconds": age,
            "state": row.state,
            "active": bool(row.active),
            "activity": row.activity or "",
            "assets": list(row.assets or []),
            "setups": dict(row.setups or {}),
            "prices": dict(row.prices or {}),
            "price_times": dict(row.price_times or {}),
            "quotes": dict(row.quotes or {}),
            "last_candle_utc": row.last_candle_utc,
            "next_open_utc": row.next_open_utc,
            "signals_session": int(row.signals_session or 0),
            "started_at_utc": row.started_at_utc,
            "stopped_at_utc": row.stopped_at_utc,
            "last_error": row.last_error or "",
            "heartbeat_utc": heartbeat,
            "lease_seconds": row.lease_seconds,
            "pid": row.pid,
            "instance_id": row.instance_id,
        }

    def _remember_account(self, acc, *, trade_allowed: bool | None = None) -> None:
        """Publish an ``AccountSummary`` as the current snapshot.

        Called *only* from worker threads that already hold the terminal (see the
        threading contract above), so a dashboard request can never reach MT5
        through here. ``None`` — a terminal that is closed or logged out — leaves
        the previous snapshot in place rather than blanking the tile to zeros.

        ``trade_allowed`` is the terminal's toolbar switch, read by the caller in
        the same MT5 session. It rides along here rather than getting its own
        writer so the snapshot and its flags can never disagree about which read
        they came from.
        """
        if acc is None:
            return
        with self._state_lock:
            self._account.balance = getattr(acc, "balance", None)
            self._account.equity = getattr(acc, "equity", None)
            self._account.margin_free = getattr(acc, "margin_free", None)
            self._account.currency = getattr(acc, "currency", None)
            self._account.login = getattr(acc, "login", None)
            self._account.server = getattr(acc, "server", None)
            self._account.name = getattr(acc, "name", None)
            self._account.trade_allowed = trade_allowed
            self._account.last_error = ""
            self._account.fetched_at_utc = tu.now_utc()

    # ------------------------------------------------------------------ #
    # Live session
    # ------------------------------------------------------------------ #
    def start_live(self, *, force: bool = False) -> dict[str, Any]:
        """Spawn the live scanner thread. Returns ``{"ok": bool, ...}``.

        Refuses when a *different* process already holds a live engine lease.
        Two engines scanning the same registry would each decide the same setups
        independently and broadcast them, so the operator would get duplicate
        Telegram alerts for every signal — the same failure the reloader warning
        in ``run.py`` guards against, reached from the other direction.

        ``force=True`` overrides, for the case the lease cannot distinguish: a
        hard crash leaves a fresh-looking lease behind until it expires, so an
        operator restarting immediately needs a way through. The lease row is
        only ever read, never trusted blindly — an unreadable database reports
        "no lease" and the start proceeds.
        """
        with self._state_lock:
            if self.is_live_running():
                return {"ok": False, "reason": "already_running",
                        "message": "A live session is already running.",
                        "live": self.live_state()}
            if self._backtest.state in (BT_STARTING, BT_RUNNING):
                return {"ok": False, "reason": "backtest_running",
                        "message": "A backtest is running — wait for it to finish.",
                        "live": self.live_state()}

        if not force:
            lease = self.engine_lease()
            if lease.get("alive") and not lease.get("mine"):
                # Deliberately outside the lock above: reading the lease opens a
                # database session, and holding the state lock across I/O would
                # block every status poll for the duration.
                return {
                    "ok": False, "reason": "engine_elsewhere",
                    "message": (
                        "Another process is already running the scanner "
                        f"(pid {lease.get('pid')}, started "
                        f"{lease.get('started_at_utc')}). Starting a second one "
                        "would duplicate every signal and Telegram alert. Stop "
                        "it first, or force the start if you know it is gone."),
                    "lease": lease,
                    "live": self.live_state(),
                }

        with self._state_lock:
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

        The four jobs that touch the terminal (live, backtest, history probe,
        broker catalogue) all need the same four things, and getting any of them
        wrong is how a session ends up half-initialised: take ``_mt5_lock``,
        build the client *inside* the worker thread that will use it, connect,
        and guarantee a disconnect. Yields ``(client, market)``.

        A failing ``disconnect`` is swallowed deliberately — it must not replace
        the exception that actually ended the job.
        """
        with self._mt5_lock:
            client = self._client_factory(self.settings)
            client.connect()
            try:
                self._resolve_broker_clock(client)
                market = self._market_factory(client, self.settings)
                sink = getattr(market, "set_debug_sink", None)
                if callable(sink):
                    sink(self._emit)
                yield client, market
            finally:
                try:
                    client.disconnect()
                except Exception:
                    pass

    def _resolve_broker_clock(self, client) -> None:
        """Discover the broker's UTC offset once per terminal session.

        Runs immediately after connecting, before any candle is fetched, because
        every later broker->UTC conversion reads the published value. The offset
        is *measured* from the terminal's own clock, never assumed: see
        :meth:`trading.mt5_client.MT5Client.discover_server_utc_offset_hours`.

        Called on the one code path all four MT5 jobs share, so live scanning,
        backtesting, the history probe and the catalogue browser cannot end up
        with different ideas of what the broker clock means.

        A failure here is not fatal — the conversion layer keeps its previous or
        pinned value — but it is never silent, because an unverified broker
        offset mis-dates every session window.
        """
        discover = getattr(client, "discover_and_publish_server_offset", None)
        if callable(discover):
            preferred = [a.broker_symbol for a in self._enabled_broker_symbols()]
            try:
                offset, detail = discover(preferred or None)
            except Exception as exc:  # a probe must never take down a session
                offset, detail = None, f"discovery raised: {exc}"
            if offset is not None:
                self._emit(f"[time] MT5 broker clock: UTC{offset:+.2f} — {detail}")
            else:
                self._emit(f"[time] MT5 broker clock NOT verified: {detail}")

        warning = tu.warn_if_server_offset_unverified()
        if warning:
            self._emit(f"[time] ERROR {warning}")

    def _retry_broker_clock(self, client, repo, *, announce: bool = True) -> bool:
        """One re-attempt at verifying the broker offset. ``True`` when it took.

        Discovery needs a *live* tick to confirm the reading (see
        :meth:`trading.mt5_client.MT5Client.discover_server_utc_offset_hours`),
        so a terminal connected while the market was shut cannot resolve the
        offset at startup and only ever can once quotes resume. Retrying here is
        what lets the session recover on its own instead of sitting dead until
        the operator restarts it — and the gate stays closed until it succeeds,
        so the retry can never let a mis-dated scan through.

        ``announce`` is False for the steady state of a still-closed gate: the
        condition is a standing property, not an event, and repeating it every
        poll would bury the log it is trying to protect.
        """
        discover = getattr(client, "discover_and_publish_server_offset", None)
        if not callable(discover):
            self._clock_blocked(repo, announce=announce)
            return False
        try:
            preferred = [a.broker_symbol for a in self._enabled_broker_symbols()]
            offset, detail = discover(preferred or None)
        except Exception as exc:  # a probe must never take down a session
            offset, detail = None, f"discovery raised: {exc}"
        if offset is None or not tu.broker_offset_verified():
            self._clock_blocked(repo, detail, announce=announce)
            return False
        self._emit(f"[time] MT5 broker clock verified: UTC{offset:+.2f} — {detail}")
        self._log(repo, "INFO", "scanner", f"broker clock verified: UTC{offset:+.2f}")
        return True

    def _clock_blocked(self, repo, detail: str = "", *, announce: bool = True) -> None:
        """Say why the scanner is idle — which is never a reason to scan anyway."""
        if not announce:
            return
        message = ("scanner held: the MT5 broker clock is still NOT verified, so "
                   "every ICT session window would be mis-dated. Retrying; set "
                   "MT5_SERVER_UTC_OFFSET to pin a verified value and start "
                   "immediately.")
        if detail:
            message += f" ({detail})"
        self._emit(f"[time] ERROR {message}")
        self._log(repo, "ERROR", "scanner", message)

    def _enabled_broker_symbols(self) -> list:
        """The traded assets' broker symbols, for broker-clock probing.

        Best-effort: the registry may be unreadable at this point in startup, and
        a probe with no preferred symbols still works from the majors fallback.
        """
        try:
            return list(self._manager_factory(self.settings).enabled_assets())
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # Session-hours gate
    # ------------------------------------------------------------------ #
    def _trading_days(self) -> frozenset[int]:
        """The weekday set this session may run on, parsed once per call site."""
        return sess.parse_trading_days(getattr(self.settings, "trading_days", None))

    def _session_gate(self, now_ny: datetime | None = None
                      ) -> tuple[bool, str, datetime | None]:
        """``(active, activity, next_open_utc)`` for *right now*.

        Reads only the clock and the settings — no MT5, no engine — so it can be
        evaluated on every poll and unit-tested on its own. When the gate is
        disabled the session is always awake, which is the pre-gate behaviour.

        ``next_open_utc`` is computed only when asleep: it drives the "next open"
        display and the sleep length, and it is the expensive half (a bounded
        minute-by-minute walk). Computing it while awake would be pure waste, and
        the dashboard has nothing to show for it then anyway.

        ``now_ny`` lets the caller pass the instant it already read. That matters
        because the caller also renders the log line from the same instant, and
        two separate readings could straddle a window boundary — producing a
        "London" line next to an "asleep" state.
        """
        if not getattr(self.settings, "session_gate_enabled", True):
            return True, "gate_disabled", None
        allowed = list(getattr(self.settings, "valid_entry_sessions", None) or []) or None
        days = self._trading_days()
        if now_ny is None:
            now_ny = tu.now_ny()
        active, activity = sess.session_activity(now_ny, allowed_sessions=allowed,
                                                 days=days)
        if active:
            return True, activity, None
        return False, activity, sess.next_activity_start_utc(
            now_ny, allowed_sessions=allowed, days=days)

    def _backfill_plan(self, sc) -> tuple[int, int]:
        """``(lookback, missing_minutes)`` for this scanner's next poll.

        The loop used to ask for a flat 5 bars every poll, which is only correct
        while it never pauses. It now sleeps between sessions, so the first poll
        after a wake has to span the whole gap: every level the model trades
        (PDH/PDL, session and Asian ranges, equal highs and lows) is rebuilt from
        this stream, and a hole degrades all of them at once without failing
        anything.

        Over-requesting is free — :meth:`scanner.AssetScanner.feed_new` keeps only
        candles strictly newer than the engine's last, so the boundary candle is
        idempotent and nothing is double-counted. Under-requesting is not
        recoverable, so when the gap exceeds ``warmup_m1_bars`` the truncation is
        returned rather than applied silently, and the caller warns.

        Replaying the gap through ``warm()`` instead is *not* an option:
        ``BarsStream.add`` raises on an out-of-order candle, so an overlapping
        re-warm would raise rather than backfill.
        """
        last = sc.last_candle_time_utc()
        if last is None:
            return POLL_LOOKBACK_BASE, 0
        gap = max(0, int((tu.now_utc() - last).total_seconds() // 60))
        wanted = gap + POLL_LOOKBACK_BASE
        cap = int(getattr(self.settings, "warmup_m1_bars", 0) or 0)
        if cap > 0 and wanted > cap:
            return cap, wanted - cap
        return wanted, 0

    @staticmethod
    def _format_open(next_open_utc: datetime | None) -> str:
        """A UTC instant as ``Mon 02:00 NY``, for the log and the dashboard."""
        if next_open_utc is None:
            return "—"
        ny = tu.utc_to_ny(next_open_utc)
        return f"{ny.strftime('%a %H:%M')} NY"

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
                          f"starting; auto_trading="
                          f"{self.settings.effective_auto_trading}")

                def account_snapshot():
                    """Read the account, publishing it as the dashboard snapshot.

                    A terminal hiccup must never take down a live session: a failed
                    read returns ``None`` and leaves the previous snapshot in place,
                    which surfaces as an unchanged balance rather than a dead scanner.

                    The terminal's Algo Trading switch is read alongside the
                    account so the dashboard can explain a rejection that
                    ``AUTO_TRADING`` alone would not account for. It may be
                    ``None`` if the terminal will not answer — recorded as
                    unknown, never as a guess.
                    """
                    try:
                        acc = client.account_info()
                        trade_allowed = client.terminal_trade_allowed()
                    except Exception:
                        return None
                    self._remember_account(acc, trade_allowed=trade_allowed)
                    return acc

                def equity():
                    acc = account_snapshot()
                    return acc.equity if acc else None

                # Seed the dashboard tile the moment the terminal is ours, so the
                # balance is on screen during warm-up rather than one poll later.
                account_snapshot()

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
                    self._attach_console(sc)
                    sc.warm(warm)
                    scanners[resolved.name] = sc
                    self._log(repo, "INFO", "scanner",
                              f"{resolved.name} warmed ({len(warm)} M1)")
                    self._emit(f"[scan] {resolved.name} ({resolved.broker_symbol}) "
                               f"warmed with {len(warm)} M1 candles.")

                with self._state_lock:
                    self._live.assets = list(scanners)
                    self._live.state = LIVE_RUNNING

                self._emit(f"[scan] LIVE — AUTO_TRADING="
                           f"{self.settings.effective_auto_trading}.")

                poll = self.settings.scanner_poll_interval_ms / 1000.0
                # ``None`` until the first evaluation, so the very first poll
                # always logs its state rather than being suppressed as "no
                # change" against a default.
                #
                # Keyed on the rendered line, not on a bare awake/asleep flag:
                # the wording changes at every session boundary (London -> NY
                # Lunch -> NY PM), and those boundaries are exactly the moments
                # worth seeing in the log. Minutes inside one window render the
                # same string, so the 5s poll still cannot spam.
                prev_line: str | None = None
                warned_backfill: set[str] = set()
                #: The unverified-broker-clock hold is announced once, not on
                #: every poll — same reasoning as ``prev_line`` above.
                clock_warned = False
                while not self._stop_event.is_set():
                    # ---- session-hours gate -------------------------------- #
                    now_ny = tu.now_ny()
                    active, activity, next_open = self._session_gate(now_ny)
                    with self._state_lock:
                        self._live.active = active
                        self._live.activity = activity
                        self._live.next_open_utc = next_open
                        # Stamped here rather than only into the persisted row:
                        # a dashboard reading this state object in-process would
                        # otherwise report "last heartbeat: none" for a loop that
                        # is demonstrably beating. Same instant, same meaning —
                        # the moment this pass began.
                        self._live.heartbeat_utc = tu.now_utc()

                    # How long this pass is allowed to sleep, resolved *before*
                    # the heartbeat below so the lease the dashboard reads is the
                    # sleep the loop is actually about to take. Awake it is the
                    # poll interval; idle it is the wait to the next window,
                    # capped so a clock jump cannot make us oversleep (see
                    # SESSION_SLEEP_CAP_SECONDS).
                    wait = poll
                    if not active:
                        wait = SESSION_SLEEP_CAP_SECONDS
                        if next_open is not None:
                            wait = min(wait, max(0.0,
                                                 (next_open - tu.now_utc()).total_seconds()))

                    # Published on every pass, including while correctly idle —
                    # this is the beat that tells a dashboard in another process
                    # the engine is alive rather than gone. Skipping it while
                    # asleep would make a healthy engine look dead for the whole
                    # of a weekend.
                    self._publish_engine_state(repo, lease_seconds=wait)

                    line = sess.activity_log_line(now_ny, active, activity)
                    if line != prev_line:
                        # The next-open hint is genuinely useful but is not part
                        # of the operator's line format, so it goes to the event
                        # log rather than the console line.
                        detail = line
                        if not active:
                            detail += (" — " + (f"next open "
                                                f"{self._format_open(next_open)}"
                                                if next_open
                                                else "no open within 7 days"))
                        self._emit(line)
                        self._log(repo, "INFO", "scanner", detail)
                        prev_line = line

                    if not active:
                        # Nothing to poll for. Wait on the event so Stop is still
                        # immediate.
                        self._stop_event.wait(wait)
                        continue

                    # ---- broker-clock gate (fail closed) -------------------- #
                    # Every session window is derived from the broker->UTC
                    # conversion, so an unverified offset mis-dates all of them
                    # at once and the scanner would trade a confidently wrong
                    # clock. Refusing to scan is the recoverable failure; a
                    # signal from a mis-dated window is not. Discovery is
                    # re-attempted here because it needs a *live tick* to
                    # confirm, so a terminal that was quiet at connect can still
                    # resolve it a poll later without a restart.
                    if not tu.broker_offset_verified():
                        if not self._retry_broker_clock(client, repo,
                                                        announce=not clock_warned):
                            clock_warned = True
                            self._stop_event.wait(poll)
                            continue
                        clock_warned = False

                    # Once per poll, not only when a signal fires (which is what
                    # `equity` is for): a quiet session would otherwise leave the
                    # dashboard's balance tile frozen at whenever the last signal
                    # happened to appear.
                    account_snapshot()
                    # Collected across this poll's asset loop, then published in
                    # one pass below alongside the price and setup tables.
                    quotes: dict[str, Any] = {}
                    for name, sc in scanners.items():
                        lookback, missing = self._backfill_plan(sc)
                        if missing and name not in warned_backfill:
                            # A truncated backfill is a standing property of the
                            # stream, not an event: a symbol whose history has
                            # genuinely run out would otherwise repeat this every
                            # poll for as long as the session runs. Once per
                            # asset is enough to be unmissable without burying
                            # the log.
                            warned_backfill.add(name)
                            self._log(repo, "WARN", "scanner",
                                      f"{name}: backfill capped at {lookback} M1 — "
                                      f"{missing} minute(s) of history are missing "
                                      "from the stream")
                        candles = market.poll_closed_candles(sc.symbol,
                                                             lookback=lookback)
                        handled = sc.feed_new(candles)
                        # The live quote, read in the terminal session this
                        # worker already owns — never a second connection. Kept
                        # beside the candle poll because both describe the same
                        # asset at the same moment, and a failure to read a
                        # quote must not disturb the candles the strategy needs.
                        try:
                            quote = client.symbol_tick(sc.symbol)
                        except Exception:
                            quote = None
                        if quote is not None:
                            quotes[name] = quote
                        last = sc.last_candle_time_utc()
                        with self._state_lock:
                            self._live.last_candle_utc = last
                            if handled:
                                self._live.signals_session += handled
                        if handled:
                            self._log(repo, "INFO", "scanner",
                                      f"{name}: {handled} new signal(s)")
                    # After the whole poll, not inside it: one pass over the
                    # scanners instead of one full dict rebuild per asset.
                    with self._state_lock:
                        self._live.setups = {
                            name: self._setup_states(sc)
                            for name, sc in scanners.items()}
                        # Same pass, same lock: the price table and the setup
                        # table are read together by the dashboard, so publishing
                        # them under one lock keeps them from disagreeing about
                        # which poll they describe.
                        self._live.prices = {
                            name: price for name, price in
                            ((n, self._last_price(sc)) for n, sc in scanners.items())
                            if price is not None}
                        self._live.price_times = {
                            name: when for name, when in
                            ((n, self._last_candle_time(sc))
                             for n, sc in scanners.items())
                            if when is not None}
                        # An asset whose quote failed to read keeps its previous
                        # one rather than vanishing: a blank cell and a stale
                        # cell are different facts, and the row carries the
                        # timestamp that tells them apart.
                        self._live.quotes.update(quotes)
                    # Republished after the scan so the row carries this poll's
                    # data, not the previous one's. The pass already beat at the
                    # top of the loop; this second write is the fresh payload.
                    self._publish_engine_state(repo, lease_seconds=poll)
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
            with self._state_lock:
                self._live.stopped_at_utc = tu.now_utc()
                self._live.last_error = error
                self._live.setups = {}   # no engine behind it any more
                # Same reasoning as ``setups``: the stream that produced these
                # died with the session, and a frozen price would read as live.
                self._live.prices = {}
                self._live.price_times = {}
                self._live.quotes = {}
                self._live.active = False
                self._live.activity = ""
                self._live.next_open_utc = None
                self._live.state = LIVE_ERROR if error else LIVE_STOPPED

            # Published before the repository is closed, and with a lease of
            # zero: the engine is gone, so the dashboard must stop reporting it
            # as alive immediately rather than waiting out the last lease it
            # held while it was running.
            if repo is not None:
                self._publish_engine_state(repo, lease_seconds=0.0)
                repo.close()

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
    # Broker catalogue (read-only)
    # ------------------------------------------------------------------ #
    def request_broker_scan(self) -> dict[str, Any]:
        """List every symbol the broker offers, for the dashboard asset browser.

        Read-only, but it still owns MT5, so it runs on its own thread under
        ``_mt5_lock``. Like the history probe it is **refused** rather than
        queued while another MT5 job runs — blocking on the lock instead would
        look like a hang, because a live session holds it for its whole life.
        """
        with self._state_lock:
            if self.is_live_running():
                return {"ok": False, "reason": "live_running",
                        "message": "A live session owns the MT5 terminal. "
                                   "Stop it to browse broker symbols.",
                        "broker": self.broker_state()}
            if self._backtest.state in (BT_STARTING, BT_RUNNING):
                return {"ok": False, "reason": "backtest_running",
                        "message": "A backtest is running. Wait for it to finish.",
                        "broker": self.broker_state()}
            if self._probe.state == PROBE_RUNNING:
                return {"ok": False, "reason": "probe_running",
                        "message": "A history check is running. Wait for it to "
                                   "finish.",
                        "broker": self.broker_state()}
            if self._broker.state == BROKER_RUNNING:
                return {"ok": False, "reason": "already_running",
                        "message": "Already reading the broker's symbol list.",
                        "broker": self.broker_state()}

            self._broker = BrokerState(state=BROKER_RUNNING)
            thread = threading.Thread(target=self._broker_worker,
                                      name="broker-catalogue", daemon=True)
            self._broker_thread = thread

        thread.start()
        return {"ok": True, "message": "Reading the broker's symbol list…",
                "broker": self.broker_state()}

    def _broker_worker(self) -> None:
        error = ""
        symbols: list[dict[str, Any]] = []
        try:
            with self._mt5_session() as (client, _market):
                symbols = client.symbol_catalog()
                if not symbols:
                    raise RuntimeError(
                        "The terminal returned no symbols — is a symbol list "
                        "available on this account?")
                # Sorted here so the browser is stable between scans; MT5's own
                # ordering is not guaranteed and would reshuffle the table.
                symbols.sort(key=lambda s: str(s.get("name", "")).upper())
        except Exception as exc:  # surfaced in the dashboard, never fatal
            error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._state_lock:
                # A failed scan keeps the previous catalogue rather than blanking
                # the table the user is reading.
                if not error:
                    self._broker_symbols = symbols
                self._broker.n_symbols = len(self._broker_symbols)
                self._broker.last_error = error
                self._broker.finished_at_utc = tu.now_utc()
                self._broker.state = BROKER_ERROR if error else BROKER_DONE

    # ------------------------------------------------------------------ #
    # Account snapshot (read-only)
    # ------------------------------------------------------------------ #
    def request_account_refresh(self) -> dict[str, Any]:
        """Read balance / equity / free margin from the terminal, once.

        Read-only, but it still owns MT5, so it follows the same rules as the
        broker scan: its own thread under ``_mt5_lock``, and **refused** rather
        than queued while another job holds the terminal. Blocking on the lock
        would look like a hang, because a live session holds it for its whole
        life — which is also why a live session is the one refusal that is not a
        failure. The live worker publishes its own snapshot every poll, so the
        dashboard already holds a fresher figure than this route could fetch.
        """
        with self._state_lock:
            if self.is_live_running():
                return {"ok": False, "reason": "live_running",
                        "message": "A live session owns the terminal — showing "
                                   "the account it reports.",
                        "account": self.account_state()}
            if self._backtest.state in (BT_STARTING, BT_RUNNING):
                return {"ok": False, "reason": "backtest_running",
                        "message": "A backtest is running. Wait for it to finish.",
                        "account": self.account_state()}
            if self._probe.state == PROBE_RUNNING:
                return {"ok": False, "reason": "probe_running",
                        "message": "A history check is running. Wait for it to "
                                   "finish.",
                        "account": self.account_state()}
            if self._broker.state == BROKER_RUNNING:
                return {"ok": False, "reason": "broker_running",
                        "message": "A broker symbol scan is running. Wait for it "
                                   "to finish.",
                        "account": self.account_state()}
            if self._account.state == ACCOUNT_RUNNING:
                return {"ok": False, "reason": "already_running",
                        "message": "Already reading the account.",
                        "account": self.account_state()}

            # In place, not a fresh ``AccountState``: the figures on screen stay
            # up while the read is in flight, so pressing Refresh never blanks a
            # balance the user is looking at.
            self._account.state = ACCOUNT_RUNNING
            self._account.last_error = ""
            thread = threading.Thread(target=self._account_worker,
                                      name="account-snapshot", daemon=True)
            self._account_thread = thread

        thread.start()
        return {"ok": True, "message": "Reading the account from the terminal…",
                "account": self.account_state()}

    def _account_worker(self) -> None:
        error = ""
        summary = None
        trade_allowed = None
        try:
            with self._mt5_session() as (client, _market):
                summary = client.account_info()
                if summary is None:
                    raise RuntimeError(
                        "The terminal reported no account — is it logged in?")
                # Only meaningful alongside an account, and read after the check
                # above so "no account" stays the reported cause of a failure.
                trade_allowed = client.terminal_trade_allowed()
        except Exception as exc:  # surfaced in the dashboard, never fatal
            error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._state_lock:
                # A failed read keeps the previous figures rather than blanking a
                # balance the user may be reading; only the error is new.
                if not error:
                    self._remember_account(summary, trade_allowed=trade_allowed)
                self._account.last_error = error
                self._account.state = ACCOUNT_ERROR if error else ACCOUNT_DONE

    # ------------------------------------------------------------------ #
    # Auto-trading override (session-only)
    # ------------------------------------------------------------------ #
    def set_auto_trading(self, enabled: bool | None) -> dict[str, Any]:
        """Set or clear the runtime override for the master trading switch.

        ``None`` clears it, handing control back to the ``AUTO_TRADING`` value
        read from ``.env`` at startup. The override is **never** written back to
        ``.env`` — it lives on the settings object for this process only, so a
        restart always returns to the baseline and the bot cannot come back up
        armed.

        Mutating that shared settings object is precisely what lets this reach a
        *running* session, and it is why the switch is a field rather than a
        parameter threaded down into the executor: the live worker builds its
        scanners once per session and they hold this object by reference, while
        the executor re-reads the switch on every ``execute()``. So the next
        signal obeys the new value with no restart and no re-wiring.

        Only the master switch is reachable from here. Every other gate in
        :mod:`trading.executor` — signal approved, risk-approved, registry symbol,
        risk-manager sizing — is untouched and cannot be influenced this way.
        """
        with self._state_lock:
            self.settings.auto_trading_override = enabled
            return {
                "ok": True,
                "auto_trading": self.settings.effective_auto_trading,
                "baseline": self.settings.auto_trading,
                "override": enabled,
                "live_running": self.is_live_running(),
            }

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def shutdown(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Stop any running job (tests / interpreter exit)."""
        if self.is_live_running():
            self.stop_live(timeout=timeout)
        for thread in (self._bt_thread, self._probe_thread, self._broker_thread,
                       self._account_thread):
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

    def wait_for_broker_scan(self, timeout: float | None = None) -> dict[str, Any]:
        """Block until the current broker scan finishes (tests / CLI)."""
        thread = self._broker_thread
        if thread is not None:
            thread.join(timeout)
        return self.broker_state()

    def wait_for_account(self, timeout: float | None = None) -> dict[str, Any]:
        """Block until the current account refresh finishes (tests / CLI)."""
        thread = self._account_thread
        if thread is not None:
            thread.join(timeout)
        return self.account_state()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _attach_console(self, sc) -> None:
        """Give a scanner the live session's console sink.

        Assigned here rather than passed into ``_default_scanner_factory``
        because that factory's call signature is relied on by callers and tests.
        A scanner that will not take the attribute is left alone — the session
        still runs, it just has no console stream.
        """
        try:
            sc.on_event = self._emit
        except Exception:
            pass

    @staticmethod
    def _setup_states(sc) -> dict[str, str]:
        """A scanner's per-direction setup state; ``{}`` if it cannot report one."""
        try:
            return sc.setup_states()
        except Exception:
            return {}

    @staticmethod
    def _last_price(sc) -> float | None:
        """The close of the last M1 candle the scanner's engine has seen.

        Read from the engine's own stream, so it is the price the strategy acted
        on rather than a separately fetched quote that could disagree with it.
        ``None`` when the stream is empty or the scanner cannot answer — the
        caller drops those, and the UI shows "—" rather than a zero.
        """
        try:
            candle = sc.engine.stream.last_m1()
        except Exception:
            return None
        return getattr(candle, "close", None) if candle is not None else None

    @staticmethod
    def _last_candle_time(sc):
        """UTC open time of the scanner's last M1 candle, or ``None``."""
        try:
            candle = sc.engine.stream.last_m1()
        except Exception:
            return None
        return getattr(candle, "t_utc", None) if candle is not None else None

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
