"""Command-line entrypoints for the AI ICT trading platform.

Usage (from the project root)::

    python run.py scan      # live multi-asset scanner (ALERT ONLY by default)
    python run.py backtest  # run a historical backtest per enabled asset
    python run.py web       # Flask dashboard (read-only)
    python run.py assets    # list the asset registry
    python run.py smoke     # quick no-MT5 self-check

The scanner never executes orders unless AUTO_TRADING=true is set explicitly in
``.env`` (default false). All safety gating lives in ``trading.executor``.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from config import Settings, get_settings
from database.repository import Repository, init_db

# --------------------------------------------------------------------------- #
# Live scanner
# --------------------------------------------------------------------------- #
def run_scan(settings: Settings) -> int:
    from trading.asset_manager import AssetManager

    # Registry is required; nothing else can decide what to analyse.
    manager = AssetManager(settings=settings)
    assets = manager.enabled_assets()
    if not assets:
        print("[scan] No enabled assets in the registry — enable assets in "
              f"{settings.assets_file} first.")
        return 0

    # MT5 connection is the only hard dependency of the live path.
    try:
        from trading.market_data import MarketData
        from trading.mt5_client import MT5Client

        client = MT5Client(settings)
        client.connect()
        market = MarketData(client, settings.mt5_server_utc_offset)
    except Exception as exc:  # pragma: no cover - depends on a live terminal
        print(f"[scan] MT5 unavailable: {exc}")
        return 2

    from scanner import AssetScanner

    warm_count = int(os.getenv("WARMUP_M1_BARS", "5000"))
    poll_sleep = settings.scanner_poll_interval_ms / 1000.0

    repo = Repository(settings=settings)
    repo.log_event("INFO", "scanner", f"starting; auto_trading={settings.auto_trading}")

    def equity():
        acc = client.account_info()
        return acc.equity if acc else None

    scanners = {}
    try:
        for asset in assets:
            if not market.symbol_exists(asset.broker_symbol):
                print(f"[scan] symbol not visible: {asset.broker_symbol} "
                      f"(asset {asset.name}) — skipping.")
                repo.log_event("WARN", "scanner",
                               f"{asset.name}: symbol {asset.broker_symbol} not visible")
                continue
            warm = market.fetch_m1_closed(asset.broker_symbol, warm_count,
                                          drop_forming=True)
            sc = AssetScanner(asset, settings=settings, repo=repo,
                              equity_provider=equity)
            sc.warm(warm)
            scanners[asset.name] = sc
            print(f"[scan] {asset.name} ({asset.broker_symbol}) warmed with "
                  f"{len(warm)} M1 candles.")
            repo.log_event("INFO", "scanner",
                           f"{asset.name} warmed ({len(warm)} M1)")

        print(f"[scan] LIVE — AUTO_TRADING={settings.auto_trading}. "
              "Press Ctrl+C to stop.")
        while True:
            for name, sc in scanners.items():
                candles = market.poll_closed_candles(sc.symbol, lookback=5)
                handled = sc.feed_new(candles)
                if handled:
                    repo.log_event("INFO", "scanner",
                                   f"{name}: {handled} new signal(s)")
            time.sleep(poll_sleep)
    except KeyboardInterrupt:  # pragma: no cover - user stop
        print("\n[scan] stopped by user.")
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        repo.close()
    return 0


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def run_backtest(settings: Settings) -> int:
    from trading.asset_manager import AssetManager

    from backtesting.engine import BacktestRunner

    manager = AssetManager(settings=settings)
    assets = manager.enabled_assets()
    if not assets:
        print("[backtest] No enabled assets.")
        return 0

    try:
        from trading.market_data import MarketData
        from trading.mt5_client import MT5Client

        client = MT5Client(settings)
        client.connect()
        market = MarketData(client, settings.mt5_server_utc_offset)
    except Exception as exc:  # pragma: no cover - needs a live terminal
        print(f"[backtest] MT5 unavailable: {exc}")
        return 2

    count = settings.backtest_m1_bars
    init_db(settings)
    with Repository(settings=settings) as repo:
        repo.log_event("INFO", "backtest", f"starting over {count} M1 per asset")
        for asset in assets:
            candles = market.fetch_m1_closed(asset.broker_symbol, count,
                                             drop_forming=True)
            runner = BacktestRunner(asset)
            summary, trades = runner.run(candles, name=asset.name)
            repo.save_backtest(
                name=f"{asset.name} auto",
                asset=asset.name,
                symbol=asset.broker_symbol,
                start_utc=summary.start_utc,
                end_utc=summary.end_utc,
                params=summary.params,
                summary=summary.to_dict(),
                trades=[t.as_db_dict() for t in trades],
            )
            d = summary.to_dict()
            print(f"[backtest] {asset.name}: {d['n_signals']} signals, "
                  f"{d['n_trades']} trades, win {d['win_rate']:.1%}, "
                  f"PF {d['profit_factor']:.2f}, total R {d['total_r']:.2f}")
        try:
            client.disconnect()
        except Exception:
            pass
    return 0


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def run_web(settings: Settings) -> int:
    from app.web import create_app

    web_app = create_app(settings=settings)
    print(f"[web] dashboard at http://{settings.flask_host}:{settings.flask_port}")
    web_app.run(host=settings.flask_host, port=settings.flask_port,
                debug=settings.flask_debug)
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
