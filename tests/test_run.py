"""CLI / orchestration tests for the non-MT5 entry points.

``assets`` and ``smoke`` require no terminal; ``scan``/``backtest`` need MT5 and
are exercised only through the scanner/engine unit suites.
"""
import json
from dataclasses import replace

import run as run_mod
from config import reload_settings


def _settings_with_assets(tmp_path, monkeypatch):
    """Return settings whose registry is an isolated temp file + temp DB."""
    reg = tmp_path / "assets.json"
    reg.write_text(json.dumps({"assets": [
        {"name": "BTC", "broker_symbol": "BTCUSD", "enabled": True, "digits": 2},
    ]}), encoding="utf-8")
    db = tmp_path / "trading.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    # Settings is a snapshot — the database URL included — so it has to be
    # rebuilt for the patched environment to be picked up.
    return replace(reload_settings(), assets_file=reg)


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


def test_assets_enable_all_opts_the_whole_watchlist_in(tmp_path, monkeypatch,
                                                       capsys):
    """The registry ships a broad list mostly off; --enable-all is the one-command
    way to opt it in, and it must persist and report the count."""
    from trading.asset_manager import AssetManager

    settings = _settings_with_assets(tmp_path, monkeypatch)
    reg = settings.assets_file

    def _write(enabled_flag: bool):
        reg.write_text(json.dumps({"assets": [
            {"name": "USTEC", "broker_symbol": "USTEC", "enabled": True},
            {"name": "EURUSD", "broker_symbol": "EURUSD", "enabled": enabled_flag},
            {"name": "XAUUSD", "broker_symbol": "XAUUSD", "enabled": enabled_flag},
        ]}), encoding="utf-8")

    _write(False)
    assert run_mod.run_assets(settings, enable_all=True) == 0
    out = capsys.readouterr().out
    assert "3 of 3 enabled" in out
    assert len(AssetManager(settings=settings).enabled_assets()) == 3

    _write(True)   # back to mostly-off, then disable everything
    assert run_mod.run_assets(settings, disable_all=True) == 0
    assert "0 of 3 enabled" in capsys.readouterr().out
    assert AssetManager(settings=settings).enabled_assets() == []


def test_main_assets_enable_all_flag(tmp_path, monkeypatch, capsys):
    from trading.asset_manager import AssetManager

    settings = _settings_with_assets(tmp_path, monkeypatch)
    monkeypatch.setattr(run_mod, "get_settings", lambda: settings)

    assert run_mod.main(["assets", "--enable-all"]) == 0
    assert "BTC" in capsys.readouterr().out
    assert AssetManager(settings=settings).get("BTC").enabled is True


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
