"""CLI / orchestration tests for the non-MT5 entry points.

``assets`` and ``smoke`` require no terminal; ``scan``/``backtest`` need MT5 and
are exercised only through the scanner/engine unit suites.
"""
import json
from dataclasses import replace

import run as run_mod
from config import get_settings


def _settings_with_assets(tmp_path, monkeypatch):
    """Return settings whose registry is an isolated temp file + temp DB."""
    reg = tmp_path / "assets.json"
    reg.write_text(json.dumps({"assets": [
        {"name": "BTC", "broker_symbol": "BTCUSD", "enabled": True, "digits": 2},
    ]}), encoding="utf-8")
    db = tmp_path / "trading.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    return replace(get_settings(), assets_file=reg)


def test_assets_lists_registry(tmp_path, monkeypatch, capsys):
    settings = _settings_with_assets(tmp_path, monkeypatch)
    assert run_mod.run_assets(settings) == 0
    out = capsys.readouterr().out
    assert "BTC" in out and "BTCUSD" in out and "ENABLED" in out


def test_main_assets_subcommand(tmp_path, monkeypatch, capsys):
    settings = _settings_with_assets(tmp_path, monkeypatch)
    monkeypatch.setattr(run_mod, "get_settings", lambda: settings)
    assert run_mod.main(["assets"]) == 0
    assert "BTCUSD" in capsys.readouterr().out


def test_main_smoke_self_check(tmp_path, monkeypatch):
    settings = _settings_with_assets(tmp_path, monkeypatch)
    monkeypatch.setattr(run_mod, "get_settings", lambda: settings)
    assert run_mod.main(["smoke"]) == 0

    from database.repository import Repository

    with Repository(settings=settings) as repo:
        assert any(e.message == "self-check ok" for e in repo.recent_events())
        assert any(a.name == "SMOKE" for a in repo.list_assets())


def test_invalid_command_exits():
    import pytest

    with pytest.raises(SystemExit):
        run_mod.main(["not-a-command"])


def test_run_web_disables_the_reloader(tmp_path, monkeypatch):
    """The Werkzeug reloader forks a second process -> a second live scanner,
    which would broadcast duplicate Telegram alerts for every setup."""
    from flask import Flask

    settings = _settings_with_assets(tmp_path, monkeypatch)
    captured = {}

    def _fake_run(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(Flask, "run", _fake_run)

    assert run_mod.run_web(settings) == 0
    assert captured["use_reloader"] is False
    assert captured["host"] == settings.flask_host
    assert captured["port"] == settings.flask_port
