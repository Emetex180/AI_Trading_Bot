"""Safe execution module tests.

The hard safety contract is tested explicitly:
  * With AUTO_TRADING=false (default) order_send is NEVER called.
  * The signal must be APPROVED + risk-approved.
  * Position size always comes from the risk manager.
"""
from datetime import datetime

import pytest

from trading.executor import (EXECUTION_FAILED, EXECUTION_SENT, EXECUTION_SKIPPED,
                              Executor)
from trading.risk_manager import RiskManager
from trading.signal_engine import Signal


def _signal(**kw) -> Signal:
    base = dict(
        asset="TEST",
        direction="buy",
        entry=101.7,
        sl=99.5,
        tp=106.0,
        entry_time_utc=datetime(2026, 1, 6, 13, 10),
        entry_time_ny=datetime(2026, 1, 6, 9, 10),
        session_keys=["ny_am"],
        session_primary="ny_am",
        silver_bullet=None,
        macro=None,
        liquidity_type="PDL",
        liquidity_price=100.0,
        purge_time_ny=datetime(2026, 1, 6, 8, 0),
        cisd_tf="M15",
        cisd_confirm_time_ny=datetime(2026, 1, 6, 8, 30),
        fvg_direction="bullish",
        fvg_lower=101.4,
        fvg_upper=101.55,
        rr=2.0,
        status="APPROVED",
        alert_only=True,
        risk_approved=True,
    )
    base.update(kw)
    return Signal(**base)


def _settings(auto_trading: bool, auto_trading_override=None):
    from types import SimpleNamespace

    # The executor builds its own RiskManager from these when none is injected,
    # and reads order_deviation when composing the order request.
    #
    # ``auto_trading_override`` mirrors the runtime field on the real Settings:
    # ``None`` there means "no override, follow the baseline", which is exactly
    # how the gate must read a stand-in that sets it.
    return SimpleNamespace(auto_trading=auto_trading,
                           auto_trading_override=auto_trading_override,
                           min_rr=1.5, risk_percent=1.0, order_deviation=20)


class _SendingExecutor(Executor):
    """Records the orders it would have placed, instead of placing them."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def _order_send(self, symbol, signal, lots):
        self.calls.append((symbol, signal.entry, lots))
        return type("R", (), {"retcode": 10009})()


class _SpyExecutor(Executor):
    """Records whether order_send would have been invoked."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.send_called = False

    def _order_send(self, symbol, signal, lots):
        self.send_called = True
        raise AssertionError("order_send must never fire when auto-trading is off")
        # pragma: no cover


def test_never_sends_when_auto_trading_off():
    ex = _SpyExecutor(settings=_settings(auto_trading=False),
                      risk_manager=RiskManager(min_rr=1.5))
    sig = _signal()
    res = ex.execute(sig, symbol="USTEC")
    assert res.status == EXECUTION_SKIPPED
    assert res.reason == "auto_trading_disabled"
    assert res.lots == 0.0
    assert ex.send_called is False


def test_no_symbol_and_off_still_disabled_first():
    """Even a malformed request never reaches the broker when disabled."""
    ex = _SpyExecutor(settings=_settings(auto_trading=False),
                      risk_manager=RiskManager(min_rr=1.5))
    res = ex.execute(_signal(), symbol=None)
    assert res.status == EXECUTION_SKIPPED
    assert res.reason == "auto_trading_disabled"


# --------------------------------------------------------------------------- #
# The master switch is an *override* over the .env baseline, not a second one.
# --------------------------------------------------------------------------- #
def test_an_override_arms_the_bot_where_env_says_off():
    ex = _SendingExecutor(settings=_settings(auto_trading=False,
                                             auto_trading_override=True),
                          risk_manager=RiskManager(min_rr=1.5, risk_percent=1.0))
    res = ex.execute(_signal(), symbol="USTEC", equity=1000.0)
    assert res.status == EXECUTION_SENT
    assert len(ex.calls) == 1


def test_an_override_disarms_the_bot_where_env_says_on():
    """Disarming has to work too, or .env would be the floor and not a baseline."""
    ex = _SpyExecutor(settings=_settings(auto_trading=True,
                                         auto_trading_override=False),
                      risk_manager=RiskManager(min_rr=1.5))
    res = ex.execute(_signal(), symbol="USTEC")
    assert res.status == EXECUTION_SKIPPED
    assert res.reason == "auto_trading_disabled"
    assert ex.send_called is False


def test_clearing_the_override_returns_the_env_baseline():
    """What "Follow .env" does, without a restart."""
    settings = _settings(auto_trading=False, auto_trading_override=True)
    ex = _SendingExecutor(settings=settings,
                          risk_manager=RiskManager(min_rr=1.5, risk_percent=1.0))
    assert ex.execute(_signal(), symbol="USTEC", equity=1000.0).status == EXECUTION_SENT

    settings.auto_trading_override = None

    res = ex.execute(_signal(), symbol="USTEC", equity=1000.0)
    assert res.status == EXECUTION_SKIPPED
    assert res.reason == "auto_trading_disabled"


def test_the_switch_reads_a_settings_object_with_no_override_attribute():
    """The gate is duck-typed so a partial settings stand-in still works.

    Guards the reason ``auto_trading_enabled`` uses ``getattr`` rather than
    ``settings.effective_auto_trading``: an AttributeError raised inside the
    execution gate would fall through to the broker path, not away from it.
    """
    from types import SimpleNamespace

    settings = SimpleNamespace(auto_trading=True, min_rr=1.5, risk_percent=1.0,
                               order_deviation=20)
    ex = _SendingExecutor(settings=settings,
                          risk_manager=RiskManager(min_rr=1.5, risk_percent=1.0))
    assert ex.execute(_signal(), symbol="USTEC", equity=1000.0).status == EXECUTION_SENT


def test_rejects_unapproved_signal_even_when_on():
    class OnExecutor(_SpyExecutor):
        def _order_send(self, symbol, signal, lots):
            self.send_called = True
            return type("R", (), {"retcode": 10009})()

    ex = OnExecutor(settings=_settings(auto_trading=True),
                    risk_manager=RiskManager(min_rr=1.5))
    res = ex.execute(_signal(status="PENDING", risk_approved=False), symbol="USTEC")
    assert res.status == EXECUTION_SKIPPED
    assert res.reason == "signal_not_approved"
    assert ex.send_called is False


def test_risk_manager_recheck_blocks_when_geometry_tampered():
    """AI tampering with SL/TP after approval must be caught at execution."""
    class OnExecutor(_SpyExecutor):
        def _order_send(self, symbol, signal, lots):
            self.send_called = True
            return type("R", (), {"retcode": 10009})()

    ex = OnExecutor(settings=_settings(auto_trading=True),
                    risk_manager=RiskManager(min_rr=1.5))
    # tp moved from 106.0 down to 102.0 -> reward 0.3 / risk 2.2 => RR ~0.14,
    # below min_rr even though the signal claims prior approval.
    sig = _signal(tp=102.0, status="APPROVED", risk_approved=True)
    res = ex.execute(sig, symbol="USTEC", equity=1000.0)
    assert res.status == EXECUTION_SKIPPED
    assert res.reason.startswith("risk_recheck:")
    assert ex.send_called is False


def test_full_send_when_on_and_approved():
    sent = []

    class SendingExecutor(Executor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.calls = []

        def _order_send(self, symbol, signal, lots):
            self.calls.append((symbol, signal.entry, lots))
            return type("R", (), {"retcode": 10009})()

    ex = SendingExecutor(settings=_settings(auto_trading=True),
                         risk_manager=RiskManager(min_rr=1.5, risk_percent=1.0))
    res = ex.execute(_signal(), symbol="USTEC", equity=1000.0)
    assert res.status == EXECUTION_SENT
    assert len(ex.calls) == 1
    sym, entry, lots = ex.calls[0]
    assert sym == "USTEC"
    assert lots > 0
    # 1000 equity * 1% = $10 at risk; risk per unit = 2.2 => 4.545 lots
    assert abs(lots - 10.0 / 2.2) < 1e-9


def test_broker_rejection_reported():
    class RejectingExecutor(Executor):
        def _order_send(self, symbol, signal, lots):
            return type("R", (), {"retcode": 10004})()  # requote

    ex = RejectingExecutor(settings=_settings(auto_trading=True),
                           risk_manager=RiskManager(min_rr=1.5))
    res = ex.execute(_signal(), symbol="USTEC", equity=1000.0)
    assert res.status == EXECUTION_FAILED
    assert res.reason.startswith("broker_rejected:")


def test_executor_api_has_no_ai_override_parameter():
    """The executor exposes no way for AI output to reach the broker."""
    import inspect

    params = inspect.signature(Executor.execute).parameters
    assert not any(p.startswith("ai") for p in params)


def test_executor_sizes_by_contract_spec_for_a_non_index_asset():
    """The same dollars buy a different number of lots in every asset class.

    A spec-less executor sizes FX as if 1 lot moved $1 per 1.0 of price, which
    would be off by ~100,000x. With the broker's contract spec this is the real
    lot count.
    """
    from trading.symbol_spec import SymbolSpec

    sent = []

    class SendingExecutor(Executor):
        def _order_send(self, symbol, signal, lots):
            sent.append((symbol, lots))
            return type("R", (), {"retcode": 10009})()

    forex = SymbolSpec(symbol="EURUSDm", contract_size=100000.0, digits=5,
                       volume_min=0.01, volume_step=0.01, volume_max=100.0,
                       tick_size=0.00001, tick_value=1.0)
    ex = SendingExecutor(settings=_settings(auto_trading=True), spec=forex)

    # 20-pip stop on EURUSD: $10 risk at $200 per lot => 0.05 lots.
    res = ex.execute(_signal(entry=1.0020, sl=1.0000, tp=1.0060),
                     symbol="EURUSDm", equity=1000.0)

    assert res.status == EXECUTION_SENT
    assert sent == [("EURUSDm", 0.05)]


def test_executor_without_a_spec_keeps_legacy_index_sizing():
    """No spec must never guess a contract size — it keeps the old behaviour."""
    sent = []

    class SendingExecutor(Executor):
        def _order_send(self, symbol, signal, lots):
            sent.append(lots)
            return type("R", (), {"retcode": 10009})()

    ex = SendingExecutor(settings=_settings(auto_trading=True))
    ex.execute(_signal(), symbol="USTEC", equity=1000.0)
    assert sent == [pytest.approx(10.0 / 2.2)]
