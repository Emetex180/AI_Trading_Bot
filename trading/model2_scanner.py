"""Model 2 scanner using the existing persistence, alerts and execution pipeline.

The runner opts into live_quotes: candle replay advances consumption and setup
state without executing historical signals; its existing quote poll authorizes
live entries. Standalone/offline scanners retain M1 signal evaluation.

Consumption tombstones are snapshots in the existing SystemEvent table, keyed
by asset and broker symbol. They are committed before signals are handled, and
restored before warm-up, without a schema change. Pending entries are deliberately
not restored on restart: their underlying consumed levels cannot become new ones.
The Model 2-only executor adds a session/freshness guard before the shared send
funnel; the shared AUTO_TRADING hard gate remains untouched.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from config import Settings, get_settings
from trading import time_utils as tu
from trading.asset_manager import Asset
from trading import model2_sessions as m2
from trading.executor import Executor
from trading.model2 import MODEL_2, Model2Strategy, model_meta

from scanner import AssetScanner


class _Model2Executor(Executor):
    """Keep the shared hard switch/order funnel; guard delayed Model 2 sends."""

    def _send(self, signal, symbol, lots, now):
        # Analysis/notification may have delayed the pipeline after the trigger.
        # Never submit a historical entry or carry it across the session close.
        now = tu.now_utc()
        ny = tu.utc_to_ny(now)
        max_age = max(5.0, 2 * self.settings.scanner_poll_interval_ms / 1000.0)
        if model_meta(signal).get("entry_basis") != "live_quote":
            return self._skipped(signal, symbol, lots, "model2_requires_live_quote", now)
        if (signal.purge_time_ny is None or ny.date() != signal.purge_time_ny.date()
                or m2.session_key_at(ny) != signal.session_primary):
            return self._skipped(signal, symbol, lots, "model2_session_expired", now)
        if not 0 <= (now - signal.entry_time_utc).total_seconds() <= max_age:
            return self._skipped(signal, symbol, lots, "model2_quote_expired", now)
        return super()._send(signal, symbol, lots, now)


class Model2Scanner(AssetScanner):
    """Run one asset through the Model 2 engine and the shared pipeline."""

    #: The model this scanner speaks for. Read by the live loop and the
    #: dashboard so a published setup table can say which engine produced it.
    MODEL = MODEL_2

    def __init__(self, asset: Asset, *, settings: Settings | None = None,
                 engine: Model2Strategy | None = None,
                 live_quotes: bool = False, **kwargs):
        cfg = settings or get_settings()
        default_executor = kwargs.get("executor") is None
        if default_executor:
            kwargs["executor"] = _Model2Executor(settings=cfg, spec=getattr(asset, "spec", None))
        # ``log=self._strategy_log`` is only needed when this class builds the
        # engine itself: it routes the engine's decision lines to the console and
        # the event log, exactly as the parent does for Model 1. A caller who
        # injects an engine owns how that engine was built, so nothing is
        # overridden here.
        super().__init__(
            asset,
            settings=cfg,
            engine=engine or Model2Strategy(asset, settings=cfg,
                                            log=self._strategy_log),
            **kwargs,
        )
        self.live_quotes = live_quotes
        if default_executor and hasattr(self.engine, "risk"):
            # Same per-asset policy/normalization at signal and execution gates.
            self.executor.risk = self.engine.risk
        identity = f"{asset.name}|{self.symbol}"
        self._state_source = "m2_consumed:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self._saved_state = ""
        if hasattr(self.engine, "restore_consumption"):
            rows = self.repo.recent_events_by_source(self._state_source, limit=1)
            if rows:
                self.engine.restore_consumption(json.loads(rows[0].message))
                self._saved_state = rows[0].message

    def _persist_consumption(self) -> None:
        if not hasattr(self.engine, "consumption_state"):
            return
        state = json.dumps(self.engine.consumption_state(), sort_keys=True)
        if state != self._saved_state:
            # Commit tombstones BEFORE routing a signal. A persistence failure
            # must propagate, not permit an entry whose purge can revive later.
            self.repo.log_event("INFO", self._state_source, state)
            self._saved_state = state

    def warm(self, candles: list) -> None:
        super().warm(candles)
        self._persist_consumption()

    def step(self, candles: list) -> int:
        handled = 0
        for candle in sorted(candles, key=lambda c: c.t_utc):
            signals = self.engine.feed(candle)
            self._persist_consumption()
            # Closed-bar replay keeps state causal, but is not a market order
            # at an invented historical fill. Live orders require a live quote.
            if not self.live_quotes:
                handled += sum(bool(self._on_signal(sig)) for sig in signals)
        return handled

    def expire_at(self, t_utc: datetime) -> None:
        self.engine.expire_at(t_utc)

    def feed_quote(self, quote: dict, *, now_utc: datetime | None = None) -> int:
        if not self.live_quotes:
            return 0
        now = now_utc or tu.now_utc()
        self.engine.expire_at(now)
        stamp = quote.get("time_utc")
        last = self.engine.stream.last_m1()
        max_age = max(5.0, 2 * self.settings.scanner_poll_interval_ms / 1000.0)
        # Missing, stale, future or unsynchronised quotes cannot authorize an
        # entry. The runner has already converted broker timestamps to real UTC.
        if (not isinstance(stamp, datetime) or last is None
                or not 0 <= (now - stamp).total_seconds() <= max_age
                or not last.t_utc + timedelta(minutes=1) <= stamp
                or (stamp - last.t_utc).total_seconds() > 90):
            return 0
        try:
            bid, ask = float(quote["bid"]), float(quote["ask"])
        except (KeyError, TypeError, ValueError):
            return 0
        signals = self.engine.feed_quote(t_utc=stamp, bid=bid, ask=ask)
        self._persist_consumption()
        return sum(bool(self._on_signal(sig)) for sig in signals)

    def _on_signal(self, signal) -> bool:
        # The parent's insert is idempotent, but it still sends alerts/orders
        # for an existing row. Close that restart gap for Model 2 only.
        if self.repo.fingerprint_exists(signal.fingerprint()):
            return False
        return super()._on_signal(signal)

    @property
    def model(self) -> str:
        """``"MODEL_2"`` — this scanner's engine, not the parent's."""
        return self.MODEL

    def liquidity_log(self) -> list[str]:
        """The session levels currently driving this asset, for diagnostics.

        Read-only, and never consulted by a decision — it exists so an operator
        can see *what* the engine is watching without reading the database.
        Returns ``"<label> <price>"`` lines, newest session first.
        """
        try:
            return [f"{lvl.label} {lvl.price}"
                    for lvl in self.engine.session_levels()]
        except Exception:  # a half-built engine must not break a poll
            return []
