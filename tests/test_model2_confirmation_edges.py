"""Edge cases around poll order, model configuration and Model 2 replay."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from backtesting.engine import BacktestRunner
from config import get_settings, reload_settings, model_enabled
from trading import time_utils as tu
from trading.model2 import Model2Strategy, EXPIRED, INVALIDATED, MODEL_2
from trading.model2_scanner import Model2Scanner
from test_model2 import ASSET, TUE, _settings, _strategy, _repo, _feed, history, confirmation, ready
from test_model2_lifecycle import live_ready, enter_quote, quote


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_runner_clock_before_poll_does_not_expire_last_eligible_cisd(direction):
    settings = _settings(max_cisd_candles=1)
    sc = Model2Scanner(ASSET, settings=settings, repo=_repo(), live_quotes=True)
    sc.warm(history(direction, target=105))
    q = quote(direction)
    # Real runner order: wall-clock expiry, closed-bar poll, then quote.
    sc.expire_at(q["time_utc"])
    assert sc.engine.pending_setups == 1
    assert sc.step(confirmation(direction)) == 0
    assert sc.feed_quote(q, now_utc=q["time_utc"]) == 1
    assert sc.repo.recent_trades()[0].reason == "auto_trading_disabled"


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_missing_final_cisd_expires_after_poll_grace(direction):
    engine = ready(direction, max_cisd_candles=1)
    engine.expire_at(quote(direction, seconds=11)["time_utc"])
    assert engine.state(direction) == EXPIRED
    assert _feed(engine, confirmation(direction)) == []


def test_live_sell_invalidates_if_ask_hits_stop_even_when_bid_does_not():
    engine = live_ready("sell")
    q = dict(quote("sell"), bid=140.5, ask=141)
    assert enter_quote(engine, q) == []
    assert engine.state("sell") == INVALIDATED


@pytest.mark.parametrize("model", ["MODEL_1", "MODEL_2"])
@pytest.mark.parametrize("value", ["false", "0", "off", "no"])
def test_per_asset_env_opt_out_is_applied_to_every_model(model, value):
    key = f"ASSET_TEST_{model}_ENABLED"
    settings = _settings(asset_env={key: value}, auto_trading_override=True)
    assert not model_enabled(settings, model, ASSET)
    if model == "MODEL_2":
        engine = Model2Strategy(ASSET, settings=settings)
        assert not engine.enabled
        assert _feed(engine, history() + confirmation()) == []


@pytest.mark.parametrize("model", ["MODEL_1", "MODEL_2"])
def test_registry_opt_out_cannot_be_reenabled_by_asset_env(model):
    asset = replace(ASSET, overrides={model.lower() + "_enabled": False})
    cfg = _settings(asset_env={f"ASSET_TEST_{model}_ENABLED": "true"})
    assert not model_enabled(cfg, model, asset)


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_explicit_backtest_model_selection_does_not_mutate_live_settings(direction, monkeypatch):
    monkeypatch.setenv("MODEL_2_ENABLED", "false")
    monkeypatch.setenv("AUTO_TRADING", "false")
    monkeypatch.setenv("MIN_RR", "1.5")
    monkeypatch.setenv("SL_MIN_OFFSET_TICKS", "2")
    monkeypatch.setenv("MAX_CISD_CANDLES", "8")
    cfg = reload_settings()
    runner = BacktestRunner(ASSET, model=MODEL_2)
    assert isinstance(runner.strategy, Model2Strategy)
    summary, trades = runner.run(history(direction) + confirmation(direction))
    assert summary.n_signals == len(trades) == 1
    assert trades[0].direction == direction
    assert cfg is get_settings() and not cfg.model_2_enabled
    assert not cfg.effective_auto_trading and cfg.auto_trading_override is None


def test_backtest_preserves_model2_broker_tick_spec():
    from trading.symbol_spec import SymbolSpec
    asset = replace(ASSET, spec=SymbolSpec("TEST", digits=2, tick_size=0.25))
    runner = BacktestRunner(asset, model=MODEL_2,
                           strategy_factory=lambda a: _strategy(asset=a))
    _, trades = runner.run(history(target=105) + confirmation())
    assert trades[0].sl == 58.5


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_model2_cisd_always_m5_even_if_model1_timeframe_is_changed(direction):
    engine = ready(direction, cisd_timeframe="M15")
    signal, = _feed(engine, confirmation(direction))
    assert signal.cisd_tf == "M5"


@pytest.mark.parametrize("hour,session", [(1, "m2_london"), (7, "m2_ny_am"),
                                         (11, "m2_lunch"), (13, "m2_ny_pm"),
                                         (20, "m2_asian")])
def test_purge_and_confirmation_work_in_each_existing_session(hour, session):
    rows = history()
    delta = timedelta(hours=hour - 7)
    rows[-15:] = [replace(bar, t_utc=bar.t_utc + delta, t_ny=bar.t_ny + delta)
                  for bar in rows[-15:]]
    engine = _strategy()
    engine.warm(rows)
    signal, = _feed(engine, confirmation(hh=hour))
    assert signal.session_primary == session
    assert signal.entry_time_ny == datetime(2026, 1, TUE, hour, 20)
