"""CLI / orchestration tests for the non-MT5 entry points.

``assets`` and ``smoke`` require no terminal; ``scan``/``backtest`` need MT5 and
are exercised only through the scanner/engine unit suites.
"""
import json
import os
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
    which would broadcast duplicate Telegram alerts for every setup.

    ``autostart=False`` is not incidental: ``run_web`` now starts the engine
    before it serves, and this test must not spawn a live scanner — a real one
    would call ``mt5.initialize()`` and take the terminal away from any engine
    already running on this machine.
    """
    from flask import Flask

    settings = _settings_with_assets(tmp_path, monkeypatch)
    captured = {}

    def _fake_run(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(Flask, "run", _fake_run)

    assert run_mod.run_web(settings, autostart=False) == 0
    assert captured["use_reloader"] is False
    assert captured["host"] == settings.flask_host
    assert captured["port"] == settings.flask_port


# --------------------------------------------------------------------------- #
# Engine autostart
#
# What "start the web app" has to mean: the engine comes up with it, on the
# very JobManager the dashboard reads. These drive a stand-in manager, because
# a real one would reach MT5.
# --------------------------------------------------------------------------- #
class _FakeJobs:
    """A JobManager that records what it was asked to do and nothing else."""

    def __init__(self, result=None, running=False, forced_result=None):
        self.result = result if result is not None else {
            "ok": True, "message": "Live session starting."}
        #: What a *forced* start returns, when that differs from the refusal an
        #: unforced one gets.
        self.forced_result = forced_result
        self.calls = []
        self.shutdowns = 0
        self._running = running

    def start_live(self, *, force=False):
        self.calls.append({"force": force})
        result = dict(self.result)
        if force and self.forced_result is not None:
            result = dict(self.forced_result)
        if result.get("ok"):
            self._running = True
        return result

    def is_live_running(self):
        return self._running

    def shutdown(self, timeout=None):
        self.shutdowns += 1
        self._running = False


def _lines(capsys) -> str:
    return capsys.readouterr().out


def test_start_engine_starts_the_engine(tmp_path, monkeypatch, capsys):
    settings = _settings_with_assets(tmp_path, monkeypatch)
    jobs = _FakeJobs()

    result = run_mod.start_engine(jobs, settings)

    assert result["ok"] is True
    assert jobs.calls == [{"force": False}]
    assert "starting" in _lines(capsys)


def test_start_engine_never_forces(tmp_path, monkeypatch, capsys):
    """Forcing past the lease guard is how two engines get to broadcast the same
    setup twice. Startup must always take the refusal."""
    settings = _settings_with_assets(tmp_path, monkeypatch)
    jobs = _FakeJobs()

    run_mod.start_engine(jobs, settings)

    assert jobs.calls[0]["force"] is False


def test_start_engine_defers_to_an_engine_in_another_process(tmp_path,
                                                            monkeypatch, capsys):
    """A deployment that runs the scanner as its own task keeps exactly one
    engine; the web app stands down and the dashboard reads the lease."""
    settings = _settings_with_assets(tmp_path, monkeypatch)
    jobs = _FakeJobs({"ok": False, "reason": "engine_elsewhere",
                      "message": "Another process is already running the "
                                 "scanner (pid 4242)."})

    result = run_mod.start_engine(jobs, settings)

    assert result["ok"] is False
    assert jobs.calls == [{"force": False}]
    out = _lines(capsys)
    assert "pid 4242" in out
    assert "show that engine's state" in out


def test_start_engine_defers_when_the_lease_process_is_still_alive(
        tmp_path, monkeypatch, capsys):
    """The lease names a pid, the pid is there: the refusal stands, because a
    forced start would put two engines on the same registry and every setup on
    Telegram twice."""
    settings = _settings_with_assets(tmp_path, monkeypatch)
    jobs = _FakeJobs({"ok": False, "reason": "engine_elsewhere",
                      "message": "Another process is already running the "
                                 "scanner (pid 4242).",
                      "lease": {"pid": 4242, "alive": True}})
    monkeypatch.setattr(run_mod, "_pid_is_running", lambda pid: True)

    run_mod.start_engine(jobs, settings)

    assert jobs.calls == [{"force": False}]
    assert "show that engine's state" in _lines(capsys)


def test_start_engine_takes_over_a_lease_whose_process_is_gone(tmp_path,
                                                               monkeypatch,
                                                               capsys):
    """The trap this closes: an engine killed hard — ``taskkill /F``, a closed
    console — leaves a fresh lease with no process behind it. Deferring would
    leave the dashboard reporting an engine that is not there, and it would
    never try again. ``force=True`` is what the refusal message already tells
    the operator to do by hand."""
    settings = _settings_with_assets(tmp_path, monkeypatch)
    jobs = _FakeJobs(
        {"ok": False, "reason": "engine_elsewhere",
         "message": "Another process is already running the scanner (pid 4242).",
         "lease": {"pid": 4242, "alive": True}},
        forced_result={"ok": True, "message": "Live session starting."})
    monkeypatch.setattr(run_mod, "_pid_is_running", lambda pid: False)

    result = run_mod.start_engine(jobs, settings)

    assert result["ok"] is True
    assert jobs.calls == [{"force": False}, {"force": True}]
    out = _lines(capsys)
    assert "no longer running" in out
    assert "starting" in out


def test_pid_is_running_says_yes_for_this_process(tmp_path, monkeypatch):
    """The probe has to be right about the easy case before its ``False`` is
    allowed to authorise a forced start."""
    assert run_mod._pid_is_running(os.getpid()) is True


def test_pid_is_running_is_cautious_when_there_is_nothing_to_check():
    """No pid, no answer: a missing or unreadable pid must never read as "gone",
    because that is the branch that forces the start."""
    assert run_mod._pid_is_running(None) is True
    assert run_mod._pid_is_running("") is True
    assert run_mod._pid_is_running("not-a-pid") is True
    assert run_mod._pid_is_running(0) is True
    assert run_mod._pid_is_running(-1) is True


def test_start_engine_reports_a_failure_without_raising(tmp_path, monkeypatch,
                                                        capsys):
    settings = _settings_with_assets(tmp_path, monkeypatch)
    jobs = _FakeJobs({"ok": False, "reason": "backtest_running",
                      "message": "A backtest is running — wait for it to "
                                 "finish."})

    assert run_mod.start_engine(jobs, settings)["ok"] is False
    assert "backtest is running" in _lines(capsys)


def test_start_engine_is_off_when_the_setting_is_off(tmp_path, monkeypatch,
                                                     capsys):
    settings = replace(_settings_with_assets(tmp_path, monkeypatch),
                       engine_autostart=False)
    jobs = _FakeJobs()

    assert run_mod.start_engine(jobs, settings)["reason"] == "disabled"
    assert jobs.calls == []
    assert "ENGINE_AUTOSTART is off" in _lines(capsys)


def test_start_engine_explicit_flag_overrides_the_setting(tmp_path, monkeypatch):
    """``--no-engine`` can only suppress a start, never cause one: a deployment
    with ENGINE_AUTOSTART=false must not be startable by an argument."""
    settings = _settings_with_assets(tmp_path, monkeypatch)
    assert settings.engine_autostart is True
    jobs = _FakeJobs()

    run_mod.start_engine(jobs, settings, autostart=False)
    assert jobs.calls == []

    off = replace(settings, engine_autostart=False)
    run_mod.start_engine(jobs, off, autostart=True)
    assert len(jobs.calls) == 1


def test_run_web_starts_engine_on_the_app_own_manager(tmp_path, monkeypatch,
                                                      capsys):
    """The whole point of the wiring: the engine started at launch and the
    engine the dashboard reads are the same object.

    ``start_engine`` is intercepted rather than allowed to run, so this test
    exercises the wiring without spawning a live scanner. ``create_app`` is
    wrapped for the same reason the assertion exists: to hold on to the app and
    ask it which manager it configured.
    """
    from flask import Flask

    import app.web as web_mod

    settings = _settings_with_assets(tmp_path, monkeypatch)
    real_create_app = web_mod.create_app
    seen = {}

    def _create(**kwargs):
        web_app = real_create_app(**kwargs)
        seen["app"] = web_app
        return web_app

    monkeypatch.setattr(web_mod, "create_app", _create)
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: None)

    started = {}

    def _record(jobs, settings, **kwargs):
        started["jobs"] = jobs
        return {"ok": True}

    monkeypatch.setattr(run_mod, "start_engine", _record)

    assert run_mod.run_web(settings) == 0
    assert started["jobs"] is seen["app"].config["JOBS"]


def test_run_web_stops_the_engine_when_the_server_stops(tmp_path, monkeypatch,
                                                        capsys):
    """A daemon thread killed with the process never runs its ``finally``, so
    the engine-state row keeps a fresh lease and the *next* launch reads that as
    a live engine elsewhere and refuses to start. Stopping deliberately writes
    ``stopped`` with a zero lease instead."""
    from flask import Flask

    import app.web as web_mod

    settings = _settings_with_assets(tmp_path, monkeypatch)
    jobs = _FakeJobs()
    monkeypatch.setattr(run_mod, "start_engine",
                        lambda jobs, settings, **kw: jobs.start_live())
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: None)

    real_create_app = web_mod.create_app

    def _create(**kwargs):
        web_app = real_create_app(**kwargs)
        web_app.config["JOBS"] = jobs
        return web_app

    monkeypatch.setattr(web_mod, "create_app", _create)

    assert run_mod.run_web(settings) == 0
    assert jobs.shutdowns == 1


def test_stop_engine_is_a_no_op_for_an_engine_that_never_started(tmp_path,
                                                                monkeypatch):
    """No engine, nothing to stop, no message — the common case for a dashboard
    that deferred to a scanner in another process."""
    jobs = _FakeJobs()

    run_mod.stop_engine(jobs)

    assert jobs.shutdowns == 1     # ``shutdown`` itself is the safe check


def test_stop_engine_survives_a_stand_in_without_shutdown(capsys):
    """``stop_engine`` is called from a ``finally``, so it must not be able to
    raise a new error over the one unwinding the stack."""
    run_mod.stop_engine(object())   # must not raise


def test_main_web_no_engine_skips_the_start(tmp_path, monkeypatch, capsys):
    """``python run.py web --no-engine`` is the opt-out for a machine that runs
    the scanner as its own process."""
    from flask import Flask

    settings = _settings_with_assets(tmp_path, monkeypatch)
    monkeypatch.setattr(run_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: None)

    assert run_mod.main(["web", "--no-engine"]) == 0
    assert "not started by this process" in _lines(capsys)


def test_main_web_starts_the_engine_by_default(tmp_path, monkeypatch, capsys):
    """The command the operator actually types must bring the engine up."""
    from flask import Flask

    settings = _settings_with_assets(tmp_path, monkeypatch)
    monkeypatch.setattr(run_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(Flask, "run", lambda self, **kwargs: None)

    started = {}
    monkeypatch.setattr(run_mod, "start_engine",
                        lambda jobs, settings, **kw: started.setdefault("jobs", jobs))

    assert run_mod.main(["web"]) == 0
    assert started.get("jobs") is not None
    # ...and it was asked to decide for itself, i.e. follow the setting.
    assert "not started by this process" not in _lines(capsys)
