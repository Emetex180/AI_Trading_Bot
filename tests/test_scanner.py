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


# --------------------------------------------------------------------------- #
# Strategy visibility: the decision log and the setup state machine
# --------------------------------------------------------------------------- #
def test_strategy_decisions_reach_the_console_and_the_event_log():
    """§19: the engine's ``[SYMBOL] ...`` lines are visible to both sinks."""
    repo = _repo()
    settings = _settings()
    console = []
    scanner = scanner_mod.AssetScanner(
        ASSET, settings=settings, repo=repo,
        analyzer=AiAnalyzer(settings=settings, transport=lambda p: None),
        notifier=TelegramNotifier(settings=settings, transport=lambda t: None),
        executor=Executor(settings=settings), on_event=console.append)

    warm, feed = build_scenario()
    scanner.warm(warm)
    scanner.step(feed)

    assert console, "nothing reached the console sink"
    assert all(m.startswith("[scan] [TEST]") for m in console), console
    assert any("purged" in m for m in console)
    assert any("CISD" in m for m in console)
    assert any("FVG" in m for m in console)

    # ``recent_events`` is newest-first; the console sink is chronological.
    logged = [e.message for e in reversed(repo.recent_events(limit=500))
              if e.source == "strategy"]
    assert logged == [m[len("[scan] "):] for m in console], \
        "the console and the event log disagree about what happened"
    assert all(m.startswith("[TEST]") for m in logged)


def test_a_rejected_setup_says_why_in_the_log():
    """A refusal must be explainable, not silent."""
    repo = _repo()
    settings = _settings()
    console = []
    asset = replace(ASSET, overrides={"min_history_h1": "5", "min_rr": "99",
                                      "valid_entry_sessions":
                                          "london_open,ny_premarket,ny_am,"
                                          "london_close,ny_pm"})
    scanner = scanner_mod.AssetScanner(
        asset, settings=settings, repo=repo,
        analyzer=AiAnalyzer(settings=settings, transport=lambda p: None),
        notifier=TelegramNotifier(settings=settings, transport=lambda t: None),
        executor=Executor(settings=settings), on_event=console.append)

    warm, feed = build_scenario()
    scanner.warm(warm)
    assert scanner.step(feed) == 0        # the RR gate refuses the entry

    rejections = [m for m in console if "rejected" in m.lower()]
    assert rejections, f"the refusal was not logged: {console}"
    assert "RR below minimum" in rejections[-1]


def test_setup_states_report_the_machine_that_produced_the_signal():
    repo = _repo()
    settings = _settings()
    scanner = scanner_mod.AssetScanner(
        ASSET, settings=settings, repo=repo,
        analyzer=AiAnalyzer(settings=settings, transport=lambda p: None),
        notifier=TelegramNotifier(settings=settings, transport=lambda t: None),
        executor=Executor(settings=settings))

    warm, feed = build_scenario()
    scanner.warm(warm)

    # Held one minute short of the entry: the buy side is waiting on its retrace.
    scanner.step(feed[:-1])
    states = scanner.setup_states()
    assert states["buy"] == "WAITING_FOR_FVG_RETRACE"

    scanner.step(feed[-1:])
    assert scanner.setup_states()["buy"] == "TRADE_CONFIRMED"


def test_setup_states_is_read_only():
    """Reporting state must never advance it."""
    repo = _repo()
    settings = _settings()
    scanner = scanner_mod.AssetScanner(
        ASSET, settings=settings, repo=repo,
        analyzer=AiAnalyzer(settings=settings, transport=lambda p: None),
        notifier=TelegramNotifier(settings=settings, transport=lambda t: None),
        executor=Executor(settings=settings))

    warm, feed = build_scenario()
    scanner.warm(warm)
    scanner.step(feed[:-1])

    before = scanner.setup_states()
    for _ in range(5):
        scanner.setup_states()
    assert scanner.setup_states() == before


def test_a_scanner_with_a_broken_engine_still_reports_state():
    """A poll must never raise just because the engine cannot answer."""
    scanner = scanner_mod.AssetScanner(
        ASSET, settings=_settings(), repo=_repo(),
        analyzer=AiAnalyzer(settings=_settings(), transport=lambda p: None),
        notifier=TelegramNotifier(settings=_settings(), transport=lambda t: None),
        executor=Executor(settings=_settings()))
    scanner.engine = object()             # no .states() at all
    assert scanner.setup_states() == {}
