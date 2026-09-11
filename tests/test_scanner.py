"""Scanner integration tests: warm-up + live replay through the full pipeline.

Uses an in-memory repository, an AI transport stub and a Telegram transport
stub — no MT5, no network. The scenario is the same fully synthetic BUY used by
the strategy/backtest suites.
"""
from dataclasses import replace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scanner as scanner_mod
from ai.analyzer import AiAnalyzer, AI_DISABLED, AI_ANALYZED
from config import get_settings
from database.models import Base
from database.repository import Repository
from notifications.telegram import TelegramNotifier
from trading.asset_manager import Asset
from trading.executor import Executor

from test_strategy import build_scenario

ASSET = Asset(name="TEST", broker_symbol="TEST", enabled=True,
              overrides={"min_history_h1": "5"})


def _repo():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return Repository(session=maker())


def _settings(**kw):
    """Hermetic settings: never inherit the developer's real ``.env``.

    ``get_settings()`` reads the project ``.env``, so a developer who has
    Telegram enabled there would otherwise change what these tests exercise.
    Overrides in ``kw`` are applied on top.
    """
    base = replace(get_settings(), auto_trading=False, ai_enabled=False,
                   telegram_enabled=False, telegram_bot_token="",
                   telegram_chat_id="")
    return replace(base, **kw)


def _make_scanner(repo, settings, ai_transport=None, telegram_transport=None):
    analyzer = AiAnalyzer(settings=settings, transport=ai_transport or (
        lambda p: {"choices": [{"message": {"content":
                                            '{"score": 70, "decision": "BUY", '
                                            '"reasoning": "clean sweep", '
                                            '"strengths": ["PDL"], '
                                            '"risks": ["news"], '
                                            '"confidence": 60}'}}]}))
    notifier = TelegramNotifier(settings=settings, transport=telegram_transport or (
        lambda text: _FakeSend(True)))
    executor = Executor(settings=settings)
    return scanner_mod.AssetScanner(
        ASSET, settings=settings, repo=repo, analyzer=analyzer,
        notifier=notifier, executor=executor)


class _FakeSend:
    ok = True


def test_warmup_ignores_stale_signals_and_live_step_alerts_once():
    """A live signal is stored once, AI-labelled, alerted, and execution recorded."""
    sent = []
    settings = _settings(auto_trading=False, ai_enabled=True,
                         telegram_enabled=True, telegram_bot_token="tok",
                         telegram_chat_id="chat")
    repo = _repo()
    scanner = _make_scanner(repo, settings,
                            telegram_transport=lambda text: sent.append(text) or _FakeSend())

    warm, feed = build_scenario()
    scanner.warm(warm)              # state advanced, no downstream action
    assert repo.count_signals() == 0

    handled = scanner.step(feed)    # 09:00 H1 close -> premise -> entry at 09:10
    assert handled == 1
    assert repo.count_signals() == 1
    row = repo.recent_signals()[0]
    assert row.status == "APPROVED"
    assert row.risk_approved is True
    assert row.ai_status == AI_ANALYZED
    assert row.ai_decision == "BUY"
    assert row.direction == "buy" and row.asset == "TEST"

    # Execution recorded as SKIPPED because AUTO_TRADING is off.
    trades = repo.recent_trades()
    assert len(trades) == 1
    assert trades[0].status == "SKIPPED"
    assert trades[0].reason == "auto_trading_disabled"

    # Exactly one ALERT-ONLY telegram message was produced.
    assert len(sent) == 1
    assert "ALERT ONLY" in sent[0]


def test_duplicate_feed_does_not_duplicate():
    settings = _settings(auto_trading=False)
    repo = _repo()
    scanner = _make_scanner(repo, settings)
    warm, feed = build_scenario()
    scanner.warm(warm)
    assert scanner.step(feed) == 1
    # Re-delivering the same closed candles (broker poll overlap) is a no-op.
    assert scanner.feed_new(feed) == 0
    assert repo.count_signals() == 1
    assert repo.count_trades() == 1


def test_ai_disabled_marks_signal_disabled():
    settings = _settings(auto_trading=False, ai_enabled=False)
    repo = _repo()
    scanner = _make_scanner(repo, settings)
    warm, feed = build_scenario()
    scanner.warm(warm)
    scanner.step(feed)
    row = repo.recent_signals()[0]
    assert row.ai_status == AI_DISABLED
    assert row.ai_score is None


def test_scanner_without_history_no_signals():
    """min_history gate prevents premature signals on a cold start."""
    asset = Asset(name="TEST2", broker_symbol="TEST2", enabled=True,
                  overrides={"min_history_h1": "5000"})
    settings = _settings(auto_trading=False)
    repo = _repo()
    scanner = scanner_mod.AssetScanner(asset, settings=settings, repo=repo)
    warm, feed = build_scenario()
    scanner.warm(warm)
    assert scanner.step(feed) == 0
    assert repo.count_signals() == 0
