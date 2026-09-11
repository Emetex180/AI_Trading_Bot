"""Safe trade execution module (MT5 order placement).

**This module is the ONLY place that may call ``order_send``.**

Safety contract (hard constraint, must never be relaxed):

* The master switch must be on ⇒ :meth:`Executor.execute` NEVER calls
  ``order_send`` otherwise. It returns a :class:`ExecutionResult` with
  ``executed=False`` and ``reason="auto_trading_disabled"`` so the intent is
  recorded (dashboard / audit trail) but no order reaches the broker.

  The switch is :func:`config.auto_trading_enabled`: the ``AUTO_TRADING``
  baseline from ``.env``, unless a runtime override is set. The dashboard may
  set that override (session-only — it is never written back to ``.env``), which
  makes this gate *operable*, not weaker: it is still a hard gate that no signal,
  no AI output and no code path here can bypass, and it is read fresh on every
  call so a running session obeys it on its next signal.
* Even with the switch on, an order is placed ONLY when **every** gate
  passes: and this is where the override's authority stops — the dashboard
  cannot reach any gate below.
    - the :class:`~trading.signal_engine.Signal` exists and is ``APPROVED``,
    - the signal was deterministically risk-approved (``risk_approved``), and
      the risk manager independently re-checks the geometry (AI can never
      change SL/TP/entry, so this is a tamper check),
    - the symbol resolves through the asset registry (no hardcoded symbols),
    - position size is computed by the risk manager from account equity — never
      from the AI or the signal's own numbers. The broker's own contract
      specification (:mod:`trading.symbol_spec`) converts that dollar risk into
      lots, so the same code is correct for FX, metals, indices and crypto.
* AI is structurally absent from this path: the method accepts a signal and an
  optional risk manager only. There is no ``ai_*`` parameter and no code path
  that consults AI output.

The executor performs **pure decision + bookkeeping** here; the actual MT5 call
lives in a tiny private helper so it can be replaced/stubbed in tests and later
audited in one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from config import Settings, auto_trading_enabled, get_settings

from . import time_utils as tu
from .risk_manager import RiskManager
from .signal_engine import Signal
from .symbol_spec import FILLING_FOK, FILLING_IOC, FILLING_RETURN, SymbolSpec

# Execution lifecycle states.
EXECUTION_SKIPPED = "SKIPPED"          # gated off (auto-trading disabled, etc.)
EXECUTION_SENT = "SENT"                # order_send was actually invoked
EXECUTION_FAILED = "FAILED"            # an order was attempted but rejected


@dataclass(frozen=True)
class ExecutionResult:
    asset: str
    symbol: str | None              # broker symbol resolved from the registry
    direction: str
    signal_fingerprint: str
    entry: float
    sl: float
    tp: float
    lots: float
    status: str                     # EXECUTION_*
    reason: str                     # "" when SENT
    requested_at_utc: datetime

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["requested_at_utc"] = self.requested_at_utc.isoformat()
        return d


class Executor:
    """Gate-and-execute approved signals. Never trades when auto-trading is off."""

    def __init__(self, settings: Settings | None = None,
                 risk_manager: RiskManager | None = None,
                 spec: SymbolSpec | None = None):
        self.settings = settings or get_settings()
        self.spec = spec
        self.risk = risk_manager or RiskManager(settings=self.settings, spec=spec)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def execute(self, signal: Signal, *, symbol: str | None = None,
                equity: float | None = None) -> ExecutionResult:
        """Attempt to execute an approved signal through MT5.

        ``symbol`` is the *broker* symbol for the signal's asset (resolved by
        the caller from the asset registry). ``equity`` defaults to the live
        account equity when omitted.
        """
        now = tu.now_utc()

        # ---- Gate 1: hard safety switch ---------------------------------- #
        if not auto_trading_enabled(self.settings):
            return self._skipped(signal, symbol, 0.0, "auto_trading_disabled", now)

        # ---- Gate 2: signal must be fully approved deterministically ------ #
        if signal is None or signal.status != "APPROVED":
            return self._skipped(signal, symbol, 0.0, "signal_not_approved", now)
        if not signal.risk_approved:
            return self._skipped(signal, symbol, 0.0, "signal_not_risk_approved", now)

        # ---- Gate 3: risk manager re-check (tamper guard on SL/TP/entry) -- #
        decision = self.risk.approve(signal.entry, signal.sl, signal.tp, signal.direction)
        if not decision.approved:
            return self._skipped(signal, symbol, 0.0, f"risk_recheck:{decision.reason}", now)

        # ---- Gate 4: size from the risk manager, never from elsewhere ----- #
        lots = self.risk.size_position(equity or 0.0,
                                       signal.entry, signal.sl, signal.direction)
        if lots <= 0:
            return self._skipped(signal, symbol, 0.0, "invalid_lot_size", now)

        # ---- Send ---------------------------------------------------------- #
        return self._send(signal, symbol, lots, now)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _send(self, signal: Signal, symbol: str | None, lots: float,
              now: datetime) -> ExecutionResult:
        """Invoke order_send — the single funnel for broker orders.

        Subclasses / tests may stub this to simulate fills. A real MT5 order is
        only ever requested from here.
        """
        if not symbol:
            return self._result(signal, None, lots, EXECUTION_FAILED,
                                "no_broker_symbol", now)
        try:
            result = self._order_send(symbol, signal, lots)
        except Exception as exc:  # pragma: no cover - defensive
            return self._result(signal, symbol, lots, EXECUTION_FAILED,
                                f"order_send_error:{exc}", now)
        if result is None or result.retcode != 10009:  # TRADE_RETCODE_DONE
            code = getattr(result, "retcode", "?")
            return self._result(signal, symbol, lots, EXECUTION_FAILED,
                                f"broker_rejected:{code}", now)
        return self._result(signal, symbol, lots, EXECUTION_SENT, "", now)

    def _order_send(self, symbol: str, signal: Signal, lots: float):  # pragma: no cover
        """The only order_send call in the codebase.

        Imported lazily so tests never need a live terminal.
        """
        import MetaTrader5 as mt5  # type: ignore

        spec = self.spec
        digits = spec.digits if spec is not None else 0

        def price(value: float) -> float:
            # Brokers reject prices whose precision exceeds the symbol's digits.
            return round(float(value), digits) if digits > 0 else float(value)

        order_type = mt5.ORDER_TYPE_BUY if signal.direction == "buy" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price(signal.entry),
            "sl": price(signal.sl),
            "tp": price(signal.tp),
            "deviation": int(self.settings.order_deviation),
            "magic": 202601,
            "comment": f"ICT {signal.direction} {signal.asset}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(mt5, spec),
        }
        return mt5.order_send(request)

    @staticmethod
    def _filling_mode(mt5, spec: SymbolSpec | None) -> int:
        """The symbol's allowed filling mode, defaulting to IOC.

        Brokers differ (many FX/gold symbols reject IOC), so the broker's own
        ``filling_mode`` wins when it is known and recognised.
        """
        mode = getattr(spec, "filling_mode", None)
        return {
            FILLING_FOK: mt5.ORDER_FILLING_FOK,
            FILLING_IOC: mt5.ORDER_FILLING_IOC,
            FILLING_RETURN: mt5.ORDER_FILLING_RETURN,
        }.get(mode, mt5.ORDER_FILLING_IOC)

    # ------------------------------------------------------------------ #
    # Result builders
    # ------------------------------------------------------------------ #
    def _skipped(self, signal: Signal | None, symbol: str | None, lots: float,
                 reason: str, now: datetime) -> ExecutionResult:
        if signal is None:
            raise ValueError("execute() requires a signal (never None when gating)")
        return self._result(signal, symbol, lots, EXECUTION_SKIPPED, reason, now)

    def _result(self, signal: Signal, symbol: str | None, lots: float,
                status: str, reason: str, now: datetime) -> ExecutionResult:
        return ExecutionResult(
            asset=signal.asset,
            symbol=symbol,
            direction=signal.direction,
            signal_fingerprint=signal.fingerprint(),
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            lots=lots,
            status=status,
            reason=reason,
            requested_at_utc=now,
        )
