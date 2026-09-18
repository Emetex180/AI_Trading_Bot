"""Section 23 acceptance check: does the wiring actually do what it claims?

Exercises the real AssetScanner + real ICTStrategy + real Repository (in-memory)
and a real Flask render of the dashboard index — no mocks of the parts under
test. Run: python _verify_wiring.py
"""
import sys
from dataclasses import replace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, "tests")

import scanner as scanner_mod
from ai.analyzer import AiAnalyzer
from config import get_settings
from database.models import Base
from database.repository import Repository, ensure_schema, get_session
from notifications.telegram import TelegramNotifier
from trading.asset_manager import Asset
from trading.executor import Executor

from test_strategy import build_scenario

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _repo():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return Repository(session=maker())


def _settings():
    base = replace(get_settings(), auto_trading=False, ai_enabled=False,
                   telegram_enabled=False, telegram_bot_token="",
                   telegram_chat_id="")
    return base


print("\n=== 1. Scanner wires the strategy log to both sinks ===")
asset = Asset(name="US100", broker_symbol="US100", enabled=True, digits=2,
              overrides={"min_history_h1": "5",
                         "valid_entry_sessions": "london_open,ny_premarket,"
                                                 "ny_am,london_close,ny_pm"})
repo = _repo()
console = []
settings = _settings()
sc = scanner_mod.AssetScanner(
    asset, settings=settings, repo=repo,
    analyzer=AiAnalyzer(settings=settings,
                        transport=lambda p: {"choices": [{"message": {
                            "content": '{"score": 70, "decision": "BUY", '
                                       '"reasoning": "x", "strengths": [], '
                                       '"risks": [], "confidence": 60}'}}]}),
    notifier=TelegramNotifier(settings=settings, transport=lambda t: None),
    executor=Executor(settings=settings),
    on_event=console.append)

warm, feed = build_scenario()
sc.warm(warm)
handled = sc.step(feed)

check("a signal was produced end to end", handled == 1, f"handled={handled}")
check("engine took the log sink", sc.engine.log is not None)

strategic = [m for m in console if "US100" in m]
check("decision lines reached the console sink", bool(strategic),
      f"{len(strategic)} lines")
check("console lines carry the [scan] tag",
      all(m.startswith("[scan] [US100]") for m in strategic))
check("lines carry the [SYMBOL] prefix", all("[US100]" in m for m in strategic))

events = [e for e in repo.recent_events(limit=200) if e.source == "strategy"]
check("decision lines persisted to the event log", bool(events),
      f"{len(events)} rows")
check("event rows match the console lines",
      [e.message for e in reversed(events)] == [m[len("[scan] "):] for m in strategic])

print("\n  --- the decisions actually recorded ---")
for e in reversed(events):
    print(f"      {e.message}")

check("the purge is logged",
      any("urge" in e.message for e in events), "")
check("the CISD is logged",
      any("CISD" in e.message for e in events), "")
check("the FVG is logged", any("FVG" in e.message for e in events), "")

print("\n=== 2. Setup state is exposed per asset ===")
states = sc.setup_states()
check("setup_states returns both directions", set(states) == {"buy", "sell"}, str(states))
check("the completed direction reads TRADE_CONFIRMED",
      states.get("buy") == "TRADE_CONFIRMED", str(states))
check("every reported state is one of the defined states",
      all(v in {"NO_SETUP", "LIQUIDITY_PURGED", "CISD_CONFIRMED", "FVG_FOUND",
                "WAITING_FOR_FVG_RETRACE", "RETRACE_CONFIRMED",
                "TRADE_CONFIRMED", "INVALIDATED"}
          for v in states.values()), str(states))
print(f"      (this scenario also sweeps sell-side, so sell ended at "
      f"{states.get('sell')} — the two directions are independent)")

print("\n=== 3. Rejection reasons are logged, not swallowed ===")
# A second engine, same scenario but with the RR gate set impossibly high, must
# say *why* it refused rather than going quiet.
repo2 = _repo()
asset2 = Asset(name="US100", broker_symbol="US100", enabled=True, digits=2,
               overrides={"min_history_h1": "5", "min_rr": "99",
                          "valid_entry_sessions": "london_open,ny_premarket,"
                                                  "ny_am,london_close,ny_pm"})
console2 = []
sc2 = scanner_mod.AssetScanner(
    asset2, settings=settings, repo=repo2,
    analyzer=AiAnalyzer(settings=settings, transport=lambda p: None),
    notifier=TelegramNotifier(settings=settings, transport=lambda t: None),
    executor=Executor(settings=settings), on_event=console2.append)
warm2, feed2 = build_scenario()
sc2.warm(warm2)
handled2 = sc2.step(feed2)
check("no signal emitted under an impossible RR gate", handled2 == 0,
      f"handled={handled2}")
rejected = [m for m in console2 if "reject" in m.lower()]
check("the rejection was logged with a reason", bool(rejected),
      f"{len(rejected)} lines")
if rejected:
    print(f"      {rejected[-1]}")

print("\n=== 4. The dashboard renders the setup state ===")
from dataclasses import asdict

from app.web import create_app
from runner import (AccountState, BacktestState, BrokerState, LiveState,
                    ProbeState)


class _Jobs:
    """The real job-state shapes, so this stub cannot drift from the app.

    Everything is built from ``runner``'s own dataclasses (the same ones
    ``JobManager`` serialises) rather than hand-written dicts, so a field added
    to one of them shows up here automatically.
    """

    def __init__(self):
        self._live = LiveState(
            state="running", assets=["US100", "XAUUSD"], signals_session=1,
            setups={"US100": {"buy": "TRADE_CONFIRMED", "sell": "NO_SETUP"},
                    "XAUUSD": {"buy": "NO_SETUP",
                               "sell": "WAITING_FOR_FVG_RETRACE"}})
        self._bt, self._probe = BacktestState(), ProbeState()
        self._broker, self._acct = BrokerState(), AccountState()

    def live_state(self):
        return asdict(self._live)

    def backtest_state(self):
        return asdict(self._bt)

    def probe_state(self):
        return asdict(self._probe)

    def broker_state(self):
        return asdict(self._broker)

    def account_state(self):
        return asdict(self._acct)

    def status(self):
        return {"live": self.live_state(), "backtest": self.backtest_state(),
                "probe": self.probe_state(), "broker": self.broker_state(),
                "account": self.account_state(), "live_running": True}


app = create_app(repository=repo, setup_db=False, jobs=_Jobs())
resp = app.test_client().get("/")
check("the dashboard renders", resp.status_code == 200, f"HTTP {resp.status_code}")
html = resp.get_data(as_text=True)
check("the setup-states element is present", 'id="live-setups"' in html)
check("an active setup state is rendered server-side",
      "US100: buy TRADE_CONFIRMED" in html)
check("an idle direction is omitted", "sell NO_SETUP" not in html)
check("a waiting setup is rendered", "XAUUSD: sell WAITING_FOR_FVG_RETRACE" in html)

print("\n=== 5. ensure_schema is idempotent and the app path migrates ===")
live = get_session(settings)
try:
    from database.models import Signal
    cols = {c.name for c in Signal.__table__.columns}
    check("the model declares the new signal columns",
          {"purge_grade", "target_kind", "target_price", "target_grade",
           "risk_points", "reward_points", "efficiency_score", "setup_id",
           "state", "digits", "structure_time_ny"} <= cols,
          f"{len(cols)} columns")
finally:
    live.close()

eng = create_engine(f"sqlite:///{settings.db_url.split('///')[-1]}", future=True)
ensure_schema(eng)
ensure_schema(eng)          # second call must be a no-op, not an error
from sqlalchemy import inspect as _inspect
with eng.connect() as conn:
    have = {c["name"] for c in _inspect(eng).get_columns("signals")}
check("the on-disk database has every new column",
      {"purge_grade", "target_kind", "target_price", "target_grade",
       "risk_points", "reward_points", "efficiency_score", "setup_id",
       "state", "digits", "structure_time_ny"} <= have,
      f"{len(have)} columns on disk")
eng.dispose()
check("re-running ensure_schema twice raised nothing", True)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
    sys.exit(1)
print("ALL CHECKS PASSED")
