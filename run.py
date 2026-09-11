"""Command-line entrypoints for the AI ICT trading platform.

Usage (from the project root)::

    python run.py scan      # live multi-asset scanner (ALERT ONLY by default)
    python run.py backtest  # run a historical backtest per enabled asset
    python run.py web       # dashboard + control panel
    python run.py assets    # list the asset registry
    python run.py smoke     # quick no-MT5 self-check

The scanner never executes orders unless AUTO_TRADING=true is set explicitly in
``.env`` (default false). All safety gating lives in ``trading.executor``.

``scan`` and ``backtest`` are thin wrappers around :mod:`runner`, which the
dashboard also drives — so the browser and the CLI run identical code.
"""
from __future__ import annotations

import argparse
import sys
import time

from config import Settings, get_settings
from database.repository import Repository, init_db

# --------------------------------------------------------------------------- #
# Live scanner
# --------------------------------------------------------------------------- #
def run_scan(settings: Settings) -> int:
    """Live multi-asset scanner, run by the shared :class:`runner.JobManager`.

    The loop itself lives in ``runner.py`` so the CLI and the dashboard drive
    exactly the same implementation. This function only starts it, blocks until
    Ctrl+C or a fatal error, then stops it.
    """
    from runner import LIVE_ERROR, JobManager

    jobs = JobManager(settings=settings, on_event=print)
    started = jobs.start_live()
    if not started["ok"]:
        print(f"[scan] {started['message']}")
        return 2

    try:
        while jobs.is_live_running():
            time.sleep(0.25)
    except KeyboardInterrupt:  # pragma: no cover - user stop
        print("\n[scan] stopping...")
        jobs.stop_live()
        return 0

    state = jobs.live_state()
    if state["state"] == LIVE_ERROR:
        print(f"[scan] {state['last_error']}")
        return 2
    return 0


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def run_backtest(settings: Settings, asset: str | None = None) -> int:
    """Historical backtest per asset, run by the shared :class:`runner.JobManager`."""
    from runner import BT_ERROR, JobManager

    jobs = JobManager(settings=settings, on_event=print)
    requested = jobs.request_backtest(asset=asset)
    if not requested["ok"]:
        print(f"[backtest] {requested['message']}")
        return 2

    state = jobs.wait_for_backtest()
    if state["state"] == BT_ERROR:
        print(f"[backtest] {state['last_error']}")
        return 2
    return 0


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def run_web(settings: Settings) -> int:
    from app.web import create_app

    web_app = create_app(settings=settings)
    print(f"[web] dashboard at http://{settings.flask_host}:{settings.flask_port}")
    # The reloader MUST stay off. It forks a second process, and because the
    # dashboard can now start a live scanner that process would warm up its own
    # engine and broadcast DUPLICATE Telegram alerts for every setup.
    web_app.run(host=settings.flask_host, port=settings.flask_port,
                debug=settings.flask_debug, use_reloader=False)
    return 0


# --------------------------------------------------------------------------- #
# Asset registry listing / smoke check
# --------------------------------------------------------------------------- #
def run_assets(settings: Settings) -> int:
    from trading.asset_manager import AssetManager

    manager = AssetManager(settings=settings)
    for asset in manager.list_assets():
        state = "ENABLED" if asset.enabled else "disabled"
        print(f"{asset.name:10s} -> {asset.broker_symbol:12s} [{state}] "
              f"digits={asset.digits}")
    return 0


def run_smoke(settings: Settings) -> int:
    """No-MT5 self-check: imports, DB init, dashboard health, backtest round-trip."""
    print("[smoke] imports OK")
    init_db(settings)
    with Repository(settings=settings) as repo:
        repo.log_event("INFO", "smoke", "self-check ok")
        repo.upsert_asset("SMOKE", "SMOKE", enabled=False)
    print("[smoke] database OK")

    from app.web import create_app

    web_app = create_app(settings=settings)
    client = web_app.test_client()
    status = client.get("/health").status_code
    assert status == 200, f"dashboard /health returned {status}"
    print("[smoke] dashboard OK")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    parser.add_argument("command", choices=["scan", "backtest", "web", "assets",
                                           "smoke"],
                        help="which subsystem to run")
    args = parser.parse_args(argv)

    settings = get_settings()
    runner = {
        "scan": run_scan,
        "backtest": run_backtest,
        "web": run_web,
        "assets": run_assets,
        "smoke": run_smoke,
    }[args.command]
    return runner(settings)


if __name__ == "__main__":
    sys.exit(main())
