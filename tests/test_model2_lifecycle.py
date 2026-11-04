"""Model 2 live/replay parity, safety, persistence and pipeline tests; no MT5."""
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from test_model2 import (ASSET, TUE, _feed, _q, _repo, _settings, _strategy,
                         history, confirmation, ready, mirror)
from trading import time_utils as tu
from trading.executor import Executor
from trading.model2 import MODEL_2, ENTRY_TRIGGERED, EXPIRED, INVALIDATED, model_meta
from trading.model2_scanner import Model2Scanner
from backtesting.engine import BacktestRunner
from notifications.telegram import TelegramNotifier, SendResult, format_signal_alert


def scanner(repo, **kwargs):
    return Model2Scanner(ASSET, settings=_settings(), repo=repo, **kwargs)


def quote(direction="buy", seconds=2):
    stamp = tu.ny_to_utc(datetime(2026, 1, TUE, 7, 20)) + timedelta(seconds=seconds)
    bid, ask = ((75.1, 75.2) if direction == "buy" else (124.8, 124.9))
    return dict(time_utc=stamp, bid=bid, ask=ask)


def live_ready(direction="buy", target=105):
    engine = ready(direction, target)
    engine.quote_entries = True
    assert _feed(engine, confirmation(direction)) == []
    return engine


def enter_quote(engine, q):
    return engine.feed_quote(t_utc=q["time_utc"], bid=q["bid"], ask=q["ask"])


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_quote_cannot_confirm_and_live_entry_uses_executable_price(direction):
    engine = ready(direction, 105)
    q = quote(direction)
    # Price beyond the old M15 boundary still cannot substitute for CISD.
    assert enter_quote(engine, dict(q, bid=81, ask=81.1)) == []
    engine = live_ready(direction)
    signal, = enter_quote(engine, q)
    assert signal.entry == (q["ask"] if direction == "buy" else q["bid"])
    assert signal.rr == pytest.approx(29.8 / 16.2)
    assert signal.entry_time_utc == q["time_utc"]
    assert model_meta(signal)["entry_basis"] == "live_quote"
    assert enter_quote(engine, q) == []
    assert engine.state(direction) == ENTRY_TRIGGERED


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_quote_spread_or_slippage_can_fail_rr(direction):
    engine = live_ready(direction, target=99)
    assert enter_quote(engine, quote(direction)) == []
    assert engine.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_no_quote_after_confirmation_expires_without_later_entry(direction):
    engine = live_ready(direction)
    assert enter_quote(engine, quote(direction, seconds=11)) == []
    assert engine.state(direction) == EXPIRED


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("sl", [75, 76, float("nan"), float("inf"), -1])
def test_invalid_sl_rejected_at_entry(direction, sl):
    engine = live_ready(direction)
    engine._setups[direction].stop_price = sl if direction == "buy" else 200 - sl
    assert enter_quote(engine, quote(direction)) == []
    assert engine.state(direction) == INVALIDATED


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("outcome", ["expired", "outside", "triggered", "invalidated"])
def test_restart_never_restores_spent_purge(direction, outcome):
    repo = _repo()
    first = scanner(repo)
    rows = history(direction, 105)
    if outcome == "outside":
        first.warm(rows[:-15])
        first.step([mirror(_q(TUE, 6, 30, 65, 75, 59, 65), direction)])
    else:
        first.warm(rows)
        if outcome == "expired":
            first.expire_at(tu.ny_to_utc(datetime(2026, 1, TUE, 11)))
        elif outcome == "invalidated":
            assert first.step(confirmation(direction, l=58)) == 0
        else:
            assert first.step(confirmation(direction)) == 1
    state = json.loads(repo.recent_events_by_source(first._state_source, limit=1)[0].message)
    assert len(state["consumed"]) == 1
    second = scanner(repo)
    second.warm(rows)
    assert second.step(confirmation(direction)) == 0
    assert second.engine.pending_setups == 0
    assert repo.count_signals() == (1 if outcome == "triggered" else 0)


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_live_scanner_off_persists_signal_but_never_sends(direction, monkeypatch):
    repo = _repo()
    sc = scanner(repo, live_quotes=True)
    monkeypatch.setattr(Executor, "_order_send", lambda *a: pytest.fail("real order"))
    sc.warm(history(direction, 105))
    assert sc.step(confirmation(direction)) == 0
    q = quote(direction)
    assert sc.feed_quote(q, now_utc=q["time_utc"]) == 1
    assert repo.count_signals() == 1
    assert repo.recent_trades()[0].reason == "auto_trading_disabled"
    assert sc.feed_quote(q, now_utc=q["time_utc"]) == 0
    replay = enter_quote(live_ready(direction), q)[0]
    assert scanner(repo)._on_signal(replay) is False
    assert len(repo.recent_trades()) == 1


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_warmup_and_stale_backfill_cannot_execute(direction):
    repo = _repo()
    sc = scanner(repo, live_quotes=True)
    sc.warm(history(direction, 105) + confirmation(direction))
    assert sc.engine.pending_setups == 0
    q = quote(direction)
    assert sc.feed_quote(q, now_utc=q["time_utc"]) == 0
    sc = scanner(_repo(), live_quotes=True)
    sc.warm(history(direction, 105))
    assert sc.step(confirmation(direction)) == 0
    sc.expire_at(q["time_utc"] + timedelta(minutes=5))
    assert sc.engine.pending_setups == 0
    assert sc.repo.count_signals() == 0


@pytest.mark.parametrize("age", [-1, 30])
def test_future_or_stale_quote_does_not_authorize_entry(age):
    sc = scanner(_repo(), live_quotes=True)
    sc.warm(history(target=105))
    sc.step(confirmation())
    q = quote()
    assert sc.feed_quote(q, now_utc=q["time_utc"] + timedelta(seconds=age)) == 0
    assert sc.repo.count_signals() == 0


def test_persistence_failure_blocks_pipeline(monkeypatch):
    repo = _repo()
    sc = scanner(repo)
    sc.warm(history()[:-15])
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(repo, "log_event", fail)
    with pytest.raises(OSError, match="disk full"):
        sc.step(history()[-15:])
    assert repo.count_signals() == 0 and repo.recent_trades() == []


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_exact_decimal_rr_also_passes_executor_recheck(direction):
    def scale(bar):
        return replace(bar, open=bar.open / 100, high=bar.high / 100,
                       low=bar.low / 100, close=bar.close / 100)
    asset = replace(ASSET, digits=4)
    engine = _strategy(asset=asset)
    engine.warm([scale(bar) for bar in history(direction)])
    signal, = _feed(engine, [scale(bar) for bar in confirmation(direction)])
    assert signal.rr == 1.5
    sc = Model2Scanner(asset, settings=_settings(), repo=_repo(), engine=engine)
    assert sc.executor.risk.approve(signal.entry, signal.sl, signal.tp, direction).approved


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("delay,reason", [(0, ""), (30, "model2_quote_expired"),
                                         (4 * 3600, "model2_session_expired")])
def test_final_send_checks_quote_age_and_session(direction, delay, reason, monkeypatch):
    engine = live_ready(direction)
    q = quote(direction)
    signal = enter_quote(engine, q)[0]
    sc = scanner(_repo(), engine=engine)
    now = q["time_utc"] + timedelta(seconds=delay)
    monkeypatch.setattr(tu, "now_utc", lambda: now)
    reached = []
    monkeypatch.setattr(Executor, "_send", lambda *a: reached.append(True) or "mock_send")
    result = sc.executor._send(signal, "TEST", 1, now)
    if reason:
        assert result.reason == reason and reached == []
    else:
        assert result == "mock_send" and reached == [True]


def test_historical_signal_cannot_reach_order_funnel(monkeypatch):
    engine = ready(target=105)
    signal = _feed(engine, confirmation())[0]
    sc = scanner(_repo(), engine=engine)
    monkeypatch.setattr(Executor, "_send", lambda *a: pytest.fail("historical send"))
    assert sc.executor._send(signal, "TEST", 1, signal.entry_time_utc).reason == "model2_requires_live_quote"


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_telegram_real_signal_and_channel_identical(direction):
    signal = _feed(ready(direction), confirmation(direction))[0]
    text = format_signal_alert(signal)
    assert text.splitlines()[1] == "Model:               MODEL 2"
    assert "M5 CISD" in text and "closed 7:20 AM NY" in text
    assert "Purge (M15)" in text and "Stop anchor:" in text
    assert "FVG" not in text and "trades above" not in text and "trades below" not in text
    chat, channel = [], []
    notifier = TelegramNotifier(settings=_settings(telegram_enabled=True, telegram_bot_token="test",
        telegram_chat_id="chat", telegram_channel_id="channel"),
        transport=lambda body: chat.append(body) or SendResult(True),
        channel_transport=lambda body: channel.append(body) or SendResult(True))
    assert notifier.send_signal(signal).ok
    assert chat == channel == [text]


@pytest.mark.parametrize("direction", ["buy", "sell"])
@pytest.mark.parametrize("exit_kind", ["end", "tp", "sl", "both", "gap"])
def test_backtest_closed_cisd_uses_only_post_confirmation_ranges(direction, exit_kind):
    runner = BacktestRunner(ASSET, model=MODEL_2,
        strategy_factory=lambda asset: _strategy(asset=asset))
    bars = history(direction) + confirmation(direction)
    if exit_kind != "end":
        hh, mm = (7, 25) if exit_kind == "gap" else (7, 20)
        bar = _q(TUE, hh, mm, 75, 100 if exit_kind in ("tp", "both", "gap") else 80,
                 58 if exit_kind in ("sl", "both") else 65, 75)
        bars.append(mirror(bar, direction))
    summary, trades = runner.run(bars)
    assert summary.n_signals == 1 and len(trades) == 1
    assert summary.params["model"] == MODEL_2
    trade = trades[0]
    assert trade.entry_time_utc == tu.ny_to_utc(datetime(2026, 1, TUE, 7, 20))
    assert trade.entry == (75 if direction == "buy" else 125)
    assert trade.outcome == {"end": "OPEN", "tp": "WIN", "sl": "LOSS", "both": "LOSS", "gap": "WIN"}[exit_kind]
    assert trade.bars_held == (0 if exit_kind == "end" else 1)


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_future_append_cannot_change_prior_signal(direction):
    bars = history(direction) + confirmation(direction)
    prefix = _feed(_strategy(), bars)
    longer = _feed(_strategy(), bars + [mirror(_q(TUE, 7, 20, 75, 100, 58, 90), direction)])
    assert len(prefix) == len(longer) == 1
    assert prefix[0].__dict__ == longer[0].__dict__


def test_runner_existing_poll_routes_closed_cisd_then_quote_to_model2(tmp_path, monkeypatch):
    from test_runner import _env, _jobs, _FakeClient, _FakeMarket, _FakeScanner, _wait_for
    settings, maker = _env(tmp_path, monkeypatch)
    settings = replace(settings, model_2_enabled=True, auto_trading=False,
        auto_trading_override=None, ai_enabled=False, telegram_enabled=False,
        scanner_poll_interval_ms=50, min_rr=1.5, sl_min_offset_ticks=2)
    q = quote()
    monkeypatch.setattr(tu, "now_utc", lambda: q["time_utc"])
    monkeypatch.setattr(tu, "now_ny", lambda: tu.utc_to_ny(q["time_utc"]))
    monkeypatch.setattr(Executor, "_order_send", lambda *a: pytest.fail("live order"))
    class QuoteClient(_FakeClient):
        def symbol_tick(self, symbol):
            self.tick_calls.append(symbol)
            return q
    class Market(_FakeMarket):
        def poll_closed_candles(self, symbol, lookback=3):
            super().poll_closed_candles(symbol, lookback)
            return confirmation()
    client = QuoteClient()
    market = Market(candles=history(target=105))
    jobs = _jobs(settings, maker, assets=(ASSET,), client_factory=lambda s: client,
                 market_factory=lambda c, s: market)
    try:
        assert jobs.start_live()["ok"]
        assert _wait_for(lambda: jobs.live_state()["signals_session"] == 1)
    finally:
        jobs.stop_live(timeout=5)
    assert len(_FakeScanner.instances) == 1
    assert _FakeScanner.instances[0].steps == market.polls
    assert len(client.tick_calls) == market.polls
    from database.repository import Repository
    with Repository(session=maker()) as repo:
        assert repo.count_signals() == 1
        assert repo.recent_trades()[0].reason == "auto_trading_disabled"
