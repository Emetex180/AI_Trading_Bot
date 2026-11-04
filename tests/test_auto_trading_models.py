"""Dashboard -> shared settings -> real scanner/executor gates (broker stubbed)."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.web import create_app
from config import get_settings, model_enabled
from runner import JobManager, _default_scanner_factory, _default_model2_scanner_factory
from scanner import AssetScanner
from trading.executor import Executor
from trading.model2_scanner import Model2Scanner
from trading import time_utils as tu
from test_model2 import ASSET, _repo, _settings, history, confirmation
from test_model2_lifecycle import live_ready, quote, enter_quote
from test_executor import _signal, _SendingExecutor
from test_web import _sign_in


def web(settings, repo):
    jobs = JobManager(settings=settings, repo_factory=lambda: repo)
    app = create_app(settings=settings, repository=repo, setup_db=False, jobs=jobs)
    client = app.test_client()
    _sign_in(client, repo)
    return client, jobs


def test_dashboard_master_controls_existing_scanners_of_both_models(monkeypatch):
    settings, repo = _settings(flask_secret_key="test-secret"), _repo()
    client, jobs = web(settings, repo)
    q = quote()
    monkeypatch.setattr(tu, "now_utc", lambda: q["time_utc"])
    calls = []
    monkeypatch.setattr(Executor, "_order_send", lambda self, symbol, signal, lots:
        calls.append((signal.model, lots)) or SimpleNamespace(retcode=10009))
    # Production factories, constructed BEFORE the dashboard override changes.
    scanners = [_default_scanner_factory(ASSET, settings, repo, lambda: 1000),
                _default_model2_scanner_factory(ASSET, settings, repo, lambda: 1000)]
    assert all(sc.executor.settings is jobs.settings for sc in scanners)
    signals = [_signal(), enter_quote(live_ready(), q)[0]]
    for i, enabled in enumerate([False, True, False]):
        response = client.post("/api/auto-trading", json={"enabled": enabled})
        assert response.status_code == 200 and response.json["auto_trading"] is enabled
        for sc, signal in zip(scanners, signals):
            fresh = replace(signal, setup_id=f"{signal.model}-{i}")
            assert sc._on_signal(fresh)
            trade = repo.recent_trades()[0]
            assert trade.status == ("SENT" if enabled else "SKIPPED")
            assert trade.reason == ("" if enabled else "auto_trading_disabled")
        status = client.get("/api/status").json
        assert status["auto_trading"] is enabled
        assert status["model_enablement"] == {"MODEL_1": True, "MODEL_2": True}
        body = client.get("/console").get_data(as_text=True)
        assert ("AUTO-TRADING ENABLED" if enabled else "AUTO-TRADING OFF") in body
    assert [model for model, _ in calls] == ["MODEL_1", "MODEL_2"]
    assert all(lots > 0 for _, lots in calls)
    assert settings.auto_trading is False  # no .env baseline mutation
    assert client.post("/api/auto-trading", json={}).json["auto_trading"] is False


@pytest.mark.parametrize("model", ["MODEL_1", "MODEL_2"])
@pytest.mark.parametrize("global_on,asset_on", [(False, True), (True, False), (False, False)])
def test_model_opt_out_is_not_overridden_by_master(model, global_on, asset_on):
    key = model.lower() + "_enabled"
    settings = _settings(auto_trading_override=True, **{key: global_on})
    asset = replace(ASSET, overrides={key: str(asset_on).lower()})
    ex = _SendingExecutor(settings=settings, asset=asset)
    signal = _signal(model=model)
    result = ex.execute(signal, symbol="TEST", equity=1000)
    assert result.reason == "model_disabled" and ex.calls == []
    repo = _repo()
    cls = AssetScanner if model == "MODEL_1" else Model2Scanner
    sc = cls(asset, settings=settings, repo=repo)
    assert sc._on_signal(signal) is False
    assert repo.count_signals() == 0


@pytest.mark.parametrize("model", ["MODEL_1", "MODEL_2"])
def test_master_does_not_bypass_risk_checks(model):
    ex = _SendingExecutor(settings=_settings(auto_trading_override=True))
    result = ex.execute(_signal(model=model, tp=102), symbol="TEST", equity=1000)
    assert result.reason.startswith("risk_recheck:") and not ex.calls
    result = ex.execute(_signal(model=model), symbol="TEST", equity=0)
    assert result.reason == "invalid_lot_size" and not ex.calls


@pytest.mark.parametrize("model", ["MODEL_1", "MODEL_2"])
def test_disarm_during_sizing_is_rechecked_before_send(model):
    settings = _settings(auto_trading_override=True)
    ex = _SendingExecutor(settings=settings)
    def size(*args):
        settings.auto_trading_override = False
        return 1
    ex.risk.size_position = size
    result = ex.execute(_signal(model=model), symbol="TEST", equity=1000)
    assert result.reason == "auto_trading_disabled" and not ex.calls


def test_future_models_share_master_and_fail_closed_until_enabled():
    settings = _settings(auto_trading_override=True)
    ex = _SendingExecutor(settings=settings)
    signal = _signal(model="MODEL_3")
    assert ex.execute(signal, symbol="TEST", equity=1000).reason == "model_disabled"
    settings.model_3_enabled = True
    assert ex.execute(signal, symbol="TEST", equity=1000).status == "SENT"
    settings.auto_trading_override = False
    assert ex.execute(signal, symbol="TEST", equity=1000).reason == "auto_trading_disabled"
    assert len(ex.calls) == 1


def test_disabled_asset_cannot_execute_even_when_model_enabled():
    settings = _settings(auto_trading_override=True)
    ex = _SendingExecutor(settings=settings, asset=replace(ASSET, enabled=False))
    assert ex.execute(_signal(), symbol="TEST", equity=1000).reason == "model_disabled"


def test_external_engine_toggle_is_refused_and_dashboard_does_not_claim_off(monkeypatch):
    settings, repo = _settings(flask_secret_key="test-secret"), _repo()
    client, jobs = web(settings, repo)
    monkeypatch.setattr(jobs, "engine_lease", lambda: {"alive": True, "mine": False})
    for enabled in [True, False]:
        response = client.post("/api/auto-trading", json={"enabled": enabled})
        assert response.status_code == 409 and not response.json["ok"]
        assert response.json["auto_trading"] is None
    assert settings.auto_trading_override is None
    status = client.get("/api/status").json
    assert status["auto_trading"] is None and not status["auto_trading_controllable"]
    body = client.get("/console").get_data(as_text=True)
    assert "AUTO-TRADING UNKNOWN" in body and "another process" in body
    for path in ("/admin", "/admin/activity"):
        response = client.get(path)
        assert response.status_code == 200
        assert "AUTO-TRADING UNKNOWN" in response.get_data(as_text=True)
    assert status["model_enablement"] == {}


def test_restart_default_is_off_and_model_1_stays_enabled(monkeypatch):
    from config import reload_settings
    monkeypatch.delenv("AUTO_TRADING", raising=False)
    monkeypatch.delenv("MODEL_1_ENABLED", raising=False)
    cfg = reload_settings()
    assert cfg.auto_trading is False and cfg.effective_auto_trading is False
    assert cfg.auto_trading_override is None and cfg.model_1_enabled is True
    cfg.auto_trading_override = True
    assert reload_settings().effective_auto_trading is False


def test_explicit_model_1_env_disable(monkeypatch):
    from config import reload_settings
    monkeypatch.setenv("MODEL_1_ENABLED", "false")
    assert not model_enabled(reload_settings(), "MODEL_1")
