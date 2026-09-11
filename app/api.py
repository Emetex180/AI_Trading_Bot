"""Dashboard control endpoints (start/stop live sessions, run backtests).

These are the only state-changing routes in the app. They are deliberately thin:
every rule about MT5 ownership, concurrency and queueing lives in
:class:`runner.JobManager`, and this module only translates HTTP to calls on it
and back.

Two properties matter here:

* :func:`api_status` is polled by the browser every few seconds. It reads the
  in-memory job state plus cheap DB queries and **never touches MT5**, so it
  keeps answering while the terminal is closed or a session is mid-warm-up.
* Auto-trading is not exposed. ``AUTO_TRADING`` remains a ``.env`` setting; there
  is no route here that can enable it. Signals are alerts, and the executor
  records ``SKIPPED`` for each one.
"""
from __future__ import annotations

from urllib.parse import urlparse

from flask import abort, g, jsonify, request, url_for

from config import Settings
from trading import time_utils as tu

# Bounds for the backtest form, so a typo cannot ask MT5 for 10 million bars.
MIN_BACKTEST_BARS = 100
MAX_BACKTEST_BARS = 500_000
MIN_MAX_HOLD_M1 = 1
MAX_MAX_HOLD_M1 = 60 * 24 * 7


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _origin_allowed() -> bool:
    """True when a state-changing request came from this dashboard's own origin.

    The server binds ``127.0.0.1``, but *any* page the browser loads can POST to
    localhost — and these endpoints start background jobs. Browsers always send
    ``Origin`` (or at least ``Referer``) on such a request, so a mismatched value
    is refused. A missing header means a non-browser client (curl, the test
    suite), which is not a CSRF vector.
    """
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return True
    return urlparse(origin).netloc == request.host


def _payload() -> dict:
    """Form fields or a JSON body, whichever the caller sent."""
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body
    return request.form.to_dict()


def _parse_int(raw, *, default, minimum: int, maximum: int) -> int | None:
    """Parse and bound an integer field; ``None`` means the caller sent rubbish."""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not minimum <= value <= maximum:
        return None
    return value


def _signal_json(row) -> dict:
    """Compact signal representation for the live feed."""
    return {
        "id": row.id,
        "asset": row.asset,
        "direction": row.direction,
        "entry": row.entry,
        "sl": row.sl,
        "tp": row.tp,
        "rr": row.rr,
        "status": row.status,
        "entry_time_ny": (tu.utc_to_ny(row.entry_time_utc).strftime("%Y-%m-%d %H:%M")
                          if row.entry_time_utc else ""),
        "session": row.session_primary or "",
        "cisd_tf": row.cisd_tf or "",
        "url": url_for("signal_detail", signal_id=row.id),
    }


def _add_ny_times(job: dict, keys: tuple[str, ...]) -> dict:
    """Add NY-clock renderings of the job's UTC timestamps for the UI."""
    for key in keys:
        value = job.get(key)
        job[key.replace("_utc", "_ny")] = (
            tu.utc_to_ny(value).strftime("%Y-%m-%d %H:%M") if value else "")
    return job


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register_api(app) -> None:
    """Attach the control endpoints to ``app`` (called from ``create_app``)."""
    jobs = app.config["JOBS"]
    cfg: Settings = app.config["CFG"]

    @app.post("/api/live/start")
    def api_live_start():
        if not _origin_allowed():
            abort(403)
        result = jobs.start_live()
        g.repo.log_event("INFO" if result["ok"] else "WARN", "dashboard",
                         f"live start: {result.get('reason') or 'started'}")
        return jsonify(result), (200 if result["ok"] else 409)

    @app.post("/api/live/stop")
    def api_live_stop():
        if not _origin_allowed():
            abort(403)
        result = jobs.stop_live()
        g.repo.log_event("INFO" if result["ok"] else "WARN", "dashboard",
                         f"live stop: {result.get('reason') or 'stopped'}")
        return jsonify(result), (200 if result["ok"] else 409)

    @app.post("/api/backtest/run")
    def api_backtest_run():
        if not _origin_allowed():
            abort(403)
        data = _payload()
        asset = (data.get("asset") or "").strip()

        # Validate before queueing: a bad name should fail loudly now, not
        # surface as an async job error minutes later.
        if asset:
            from trading.asset_manager import AssetManager, AssetRegistryError

            try:
                AssetManager(settings=cfg).get(asset)
            except AssetRegistryError:
                return jsonify({"ok": False, "reason": "unknown_asset",
                                "message": f"Unknown asset {asset!r}."}), 400

        bars = _parse_int(data.get("bars"), default=cfg.backtest_m1_bars,
                          minimum=MIN_BACKTEST_BARS, maximum=MAX_BACKTEST_BARS)
        if bars is None:
            return jsonify({"ok": False, "reason": "bad_bars",
                            "message": f"bars must be between {MIN_BACKTEST_BARS} "
                                       f"and {MAX_BACKTEST_BARS}."}), 400

        max_hold = _parse_int(data.get("max_hold_m1"), default=None,
                              minimum=MIN_MAX_HOLD_M1, maximum=MAX_MAX_HOLD_M1)
        if max_hold is None and str(data.get("max_hold_m1") or "").strip():
            return jsonify({"ok": False, "reason": "bad_max_hold",
                            "message": f"max_hold_m1 must be between "
                                       f"{MIN_MAX_HOLD_M1} and {MAX_MAX_HOLD_M1}."}), 400

        result = jobs.request_backtest(asset=asset, bars=bars, max_hold_m1=max_hold)
        g.repo.log_event("INFO" if result["ok"] else "WARN", "dashboard",
                         f"backtest request ({asset or 'all enabled'}): "
                         f"{result.get('message')}")
        return jsonify(result), (200 if result["ok"] else 409)

    @app.get("/api/status")
    def api_status():
        payload = jobs.status()
        live = _add_ny_times(payload["live"], ("started_at_utc", "stopped_at_utc"))
        last_candle = live.get("last_candle_utc")
        live["last_candle_ny"] = (tu.utc_to_ny(last_candle).strftime("%Y-%m-%d %H:%M")
                                  if last_candle else "")
        payload["backtest"] = _add_ny_times(payload["backtest"], ("finished_at_utc",))

        after = request.args.get("after_signal_id", type=int)
        payload["signals"] = {
            "last_id": g.repo.max_signal_id(),
            "new": ([_signal_json(r) for r in g.repo.signals_after_id(after)]
                    if after is not None else []),
        }
        payload["telegram"] = {
            "enabled": cfg.telegram_enabled,
            "configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
        }
        # Surfaced read-only: the dashboard displays this, and no route can set it.
        payload["auto_trading"] = cfg.auto_trading
        payload["assets"] = asset_choices(cfg, g.repo)
        payload["backtest_defaults"] = {"bars": cfg.backtest_m1_bars}
        return jsonify(payload)


def asset_choices(cfg: Settings, repo=None) -> list[dict]:
    """Assets for the dashboard dropdowns (DB table first, registry as fallback)."""
    rows = repo.list_assets() if repo is not None else []
    if rows:
        return [{"name": a.name, "broker_symbol": a.broker_symbol, "enabled": a.enabled}
                for a in rows]
    return registry_assets(cfg)


def registry_assets(cfg: Settings) -> list[dict]:
    """Read the asset registry from ``assets.json`` (no DB, no MT5)."""
    from trading.asset_manager import AssetManager, AssetRegistryError

    try:
        return [{"name": a.name, "broker_symbol": a.broker_symbol, "enabled": a.enabled}
                for a in AssetManager(settings=cfg).list_assets()]
    except AssetRegistryError:
        return []
