"""Per-asset live scanner.

An :class:`AssetScanner` owns one asset's ICT engine and routes every approved
signal through the full read-only pipeline, in order:

1. In-process dedupe (:class:`RecentSignals`) and DB fingerprint dedupe.
2. Persist the signal.
3. AI **advisory** overlay (analyzer is allowed to read; it can never change
   entry/SL/TP/direction — see ``ai/analyzer.py``).
4. Telegram ALERT-ONLY broadcast (no-op when disabled).
5. Safe execution attempt via :class:`trading.executor.Executor` — a no-op that
   records ``SKIPPED`` whenever AUTO_TRADING is off.

Warm-up is a replay of historical *closed* M1 candles through the engine with
any signals they would have emitted **discarded** — this advances the engine's
episode state to "now" without re-alerting stale setups. Signals that form
*after* warm-up are handled normally. Restart safety: fingerprints are unique
in the DB, so a signal can never be alerted or executed twice.

Visibility: the engine's decision log (``[US100] ...`` lines — purge found,
CISD confirmed, FVG formed, and *why* a candidate was rejected) is routed to
the same two sinks as everything else here — the console stream and the
persisted event log — and :meth:`setup_states` exposes the per-symbol setup
state machine for the dashboard. Both are read-only views of the engine; neither
can influence a decision.

All downstream collaborators are injectable so the scanner is fully testable
without MT5, Telegram or an LLM. ``equity_provider`` supplies live account
equity to the executor when auto-trading is on.
"""
from __future__ import annotations

from typing import Callable

from config import Settings, get_settings
from trading.asset_manager import Asset
from trading.bars import Candle
from trading.executor import Executor
from trading.signal_engine import RecentSignals, Signal
from trading.strategy import ICTStrategy

from ai.analyzer import AiAnalyzer, apply_ai
from database.repository import Repository
from notifications.telegram import TelegramNotifier


class AssetScanner:
    """Run one asset through the ICT strategy + downstream pipeline."""

    def __init__(self, asset: Asset, *, settings: Settings | None = None,
                 engine: ICTStrategy | None = None,
                 repo: Repository | None = None,
                 analyzer: AiAnalyzer | None = None,
                 notifier: TelegramNotifier | None = None,
                 executor: Executor | None = None,
                 dedupe: RecentSignals | None = None,
                 symbol: str | None = None,
                 on_event: Callable[[str], None] | None = None,
                 equity_provider: Callable[[], float] | None = None):
        cfg = settings or get_settings()
        self.asset = asset
        self.settings = cfg
        self.symbol = symbol or asset.broker_symbol
        self.equity_provider = equity_provider
        self.on_event = on_event
        self.repo = repo or Repository(settings=cfg)
        # Constructed after the repo so the engine's decision log can reach the
        # event log from the very first candle. An injected engine keeps its own
        # sink — the caller owns how it was built.
        self.engine = engine or ICTStrategy(asset, settings=cfg,
                                            log=self._strategy_log)
        self.analyzer = analyzer or AiAnalyzer(settings=cfg)
        self.notifier = notifier or TelegramNotifier(settings=cfg)
        self.executor = executor or Executor(settings=cfg, spec=getattr(asset, "spec", None))
        self.dedupe = dedupe or RecentSignals()
        self.processed = 0

    # ------------------------------------------------------------------ #
    # Warm-up / live stepping
    # ------------------------------------------------------------------ #
    def warm(self, candles: list[Candle]) -> None:
        """Replay historical closed M1 candles to advance engine state to 'now'.

        Signals emitted during warm-up are intentionally discarded: they
        represent setups that fired before this process attached, and re-alerting
        them would spam stale entries. Fingerprint dedupe still protects against
        double-alerting if a warm-up replay overlaps a live boundary.
        """
        for candle in candles:
            self.engine.feed(candle)

    def step(self, candles: list[Candle]) -> int:
        """Feed newly closed M1 candles; return how many signals were handled."""
        handled = 0
        for candle in sorted(candles, key=lambda c: c.t_utc):
            for signal in self.engine.feed(candle):
                if self._on_signal(signal):
                    handled += 1
        return handled

    def feed_new(self, candles: list[Candle]) -> int:
        """Like :meth:`step` but filters to candles after the engine's last one.

        Broker polls can return the same closed bars repeatedly; this keeps the
        scanner strictly forward-moving and avoids re-processing duplicates.
        """
        last = self.engine.stream.last_m1()
        new = [c for c in candles
               if last is None or c.t_utc > last.t_utc]
        return self.step(new)

    # ------------------------------------------------------------------ #
    # Pipeline
    # ------------------------------------------------------------------ #
    def _on_signal(self, signal: Signal) -> bool:
        """Route one *approved* signal through the pipeline. Returns True if new."""
        if signal.status != "APPROVED":
            return False
        if self.dedupe.is_duplicate(signal):
            return False

        row = self.repo.save_signal(signal)          # idempotent on fingerprint

        # AI advisory overlay: read-only, never touches risk geometry.
        analysis = self.analyzer.analyze(signal)
        apply_ai(signal, analysis)
        if row is not None:
            self.repo.update_signal_ai(row.id, signal)

        # ALERT ONLY broadcast.
        self.notifier.send_signal(signal)

        # Safe execution attempt (SKIPPED whenever auto-trading is disabled).
        equity = self.equity_provider() if self.equity_provider is not None else None
        result = self.executor.execute(signal, symbol=self.symbol, equity=equity)
        self.repo.save_trade(result, signal_id=row.id if row is not None else None)

        self.dedupe.mark(signal)
        self.processed += 1
        return True

    # ------------------------------------------------------------------ #
    # Strategy visibility (read-only)
    # ------------------------------------------------------------------ #
    def setup_states(self) -> dict[str, str]:
        """The per-direction setup state machine for this asset.

        Returns e.g. ``{"buy": "WAITING_FOR_FVG_RETRACE", "sell": "NO_SETUP"}``.
        Read-only: it reports what the engine decided, and gives the dashboard
        the same view the log lines describe.
        """
        try:
            return self.engine.states()
        except Exception:  # a fake or half-built engine must not break a poll
            return {}

    def _strategy_log(self, message: str) -> None:
        """One engine decision line, to the console and the event log.

        The engine already prefixes the symbol (``[US100] ...``); the console
        gets the scanner's own ``[scan]`` tag so a decision line is attributable
        to the session that produced it.
        """
        self._emit(f"[scan] {message}")
        self._log_event("INFO", "strategy", message)

    def _emit(self, message: str) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(message)
        except Exception:  # a broken console must not stop a session
            pass

    def _log_event(self, level: str, source: str, message: str) -> None:
        try:
            self.repo.log_event(level, source, message)
        except Exception:  # logging must never take down a session
            pass

    # ------------------------------------------------------------------ #
    def last_candle_time_utc(self):
        last = self.engine.stream.last_m1()
        return last.t_utc if last is not None else None
