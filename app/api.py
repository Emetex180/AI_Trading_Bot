"""Dashboard control endpoints (start/stop live sessions, run backtests).

These are the only state-changing routes in the app. They are deliberately thin:
every rule about MT5 ownership, concurrency and queueing lives in
:class:`runner.JobManager`, and this module only translates HTTP to calls on it
and back.

Two properties matter here:

* :func:`api_status` is polled by the browser every few seconds. It reads the
  in-memory job state plus cheap DB queries and **never touches MT5**, so it
  keeps answering while the terminal is closed or a session is mid-warm-up.
* The master trading switch is operable but not *persisted*. ``AUTO_TRADING`` in
  ``.env`` remains the baseline, and :func:`api_auto_trading` can set a
  session-only override on top of it — so the executor still records
  ``SKIPPED`` for every signal unless the switch is deliberately on, and a
  restart always returns the bot to the ``.env`` value. No route here can reach
  any other gate in :mod:`trading.executor`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import abort, g, jsonify, request, url_for

from config import Settings
from trading import time_utils as tu

# Bounds for the backtest form, so a typo cannot ask MT5 for 10 million bars.
MIN_BACKTEST_BARS = 100
MAX_BACKTEST_BARS = 500_000
MIN_MAX_HOLD_M1 = 1
MAX_MAX_HOLD_M1 = 60 * 24 * 7

# A date range must cover at least this long, so "start == end" cannot produce a
# technically-valid but useless single-minute replay.
MIN_RANGE_DAYS = 1
# ...and at most this long. Also expressed in bars (below) because minute bars
# only exist for market-open minutes, so elapsed days are the looser bound.
MAX_RANGE_DAYS = 365 * 6


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


def _parse_ny_bound(raw: str, *, end_of_day: bool):
    """Parse a dashboard date field into a UTC instant on the project NY clock.

    Accepts ``YYYY-MM-DD`` (a whole day) or ``YYYY-MM-DDTHH:MM`` (a precise
    minute). Both are read as NY-clock values, matching every timestamp the UI
    displays, and converted to UTC through :mod:`trading.time_utils` so no
    conversion happens anywhere else.

    Returns ``(utc_datetime, None)`` on success or ``(None, reason)``.
    """
    text = (raw or "").strip()
    if not text:
        return None, "missing"

    parsed: datetime | None = None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None, "unparseable"

    if len(text) == 10:  # a bare date covers the whole NY day
        if end_of_day:
            parsed = parsed.replace(hour=23, minute=59)
    return tu.ny_to_utc(parsed), None


def _parse_range(data: dict):
    """Validate the optional ``start``/``end`` form pair.

    Returns ``(start_utc, end_utc, None)`` when absent (the caller should fall
    back to the bars path) and ``(None, None, reason)`` when present but bad.
    """
    raw_start = (data.get("start") or "").strip()
    raw_end = (data.get("end") or "").strip()
    if not raw_start and not raw_end:
        return None, None, None

    if not raw_start or not raw_end:
        return None, None, "both_required"

    start_utc, why = _parse_ny_bound(raw_start, end_of_day=False)
    if start_utc is None:
        return None, None, f"start_{why}"
    end_utc, why = _parse_ny_bound(raw_end, end_of_day=True)
    if end_utc is None:
        return None, None, f"end_{why}"

    if end_utc <= start_utc:
        return None, None, "end_before_start"
    if end_utc - start_utc < timedelta(days=MIN_RANGE_DAYS):
        return None, None, "range_too_short"
    if end_utc - start_utc > timedelta(days=MAX_RANGE_DAYS):
        return None, None, "range_too_long"
    # Minute bars only exist for market-open minutes, so an elapsed span is
    # always far more bars than it has data for. Bound the elapsed span well
    # under MAX_BACKTEST_BARS minutes anyway, so a range can never ask for more
    # replay than the bars path allows.
    if (end_utc - start_utc) > timedelta(minutes=MAX_BACKTEST_BARS):
        return None, None, "range_too_long"
    return start_utc, end_utc, None


_RANGE_MESSAGES = {
    "both_required": "Give both a start and an end date.",
    "end_before_start": "The end date must be after the start date.",
    "range_too_short": "Pick a range of at least one day.",
    "range_too_long": "That range is too long. Pick a shorter window.",
}


def _range_error(reason: str) -> str:
    """Human message for a range validation failure."""
    if reason in _RANGE_MESSAGES:
        return _RANGE_MESSAGES[reason]
    if reason.startswith("start_") or reason.startswith("end_"):
        which = "start" if reason.startswith("start_") else "end"
        return (f"Could not read the {which} date. Use YYYY-MM-DD, or "
                "YYYY-MM-DDTHH:MM.")
    return "That date range is not valid."


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

        # A date range and a bar count are two ways to say the same thing; the
        # range is the more specific request, so it wins when both arrive.
        start_utc, end_utc, range_error = _parse_range(data)
        if range_error:
            return jsonify({"ok": False, "reason": "bad_range",
                            "message": _range_error(range_error)}), 400

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

        result = jobs.request_backtest(asset=asset,
                                       bars=None if start_utc else bars,
                                       start_utc=start_utc, end_utc=end_utc,
                                       max_hold_m1=max_hold)
        window = (f"{data.get('start')}..{data.get('end')}" if start_utc
                  else f"last {bars} M1")
        g.repo.log_event("INFO" if result["ok"] else "WARN", "dashboard",
                         f"backtest request ({asset or 'all enabled'}) over "
                         f"{window}: {result.get('message')}")
        return jsonify(result), (200 if result["ok"] else 409)

    @app.post("/api/data/probe")
    def api_data_probe():
        """Report the M1 history the broker holds, so a date range can be picked."""
        if not _origin_allowed():
            abort(403)
        asset = ((_payload().get("asset") or "").strip()
                 or (request.args.get("asset") or "").strip())
        if not asset:
            return jsonify({"ok": False, "reason": "no_asset",
                            "message": "Pick an asset first."}), 400

        from trading.asset_manager import AssetManager, AssetRegistryError

        try:
            AssetManager(settings=cfg).get(asset)
        except AssetRegistryError:
            return jsonify({"ok": False, "reason": "unknown_asset",
                            "message": f"Unknown asset {asset!r}."}), 400

        result = jobs.request_data_probe(asset)
        return jsonify(result), (200 if result["ok"] else 409)

    # ------------------------------------------------------------------ #
    # Asset registry
    #
    # ``assets.json`` is authoritative — it is what the scanner and the
    # backtester read. The ``assets`` DB table is a mirror kept in step here so
    # the two can never drift into offering an asset the engine will refuse.
    # ------------------------------------------------------------------ #
    def _registry():
        from trading.asset_manager import AssetManager, AssetRegistryError

        try:
            return AssetManager(settings=cfg), None
        except AssetRegistryError as exc:
            return None, str(exc)

    def _sync_assets(repo, names: list[str] | None = None) -> None:
        """Mirror registry entries into the DB (best-effort, never fatal).

        ``names=None`` mirrors the whole registry. The DB ``assets`` table backs
        the dashboard's historical views, so a bulk registry change that mirrored
        only the row it touched would leave the UI contradicting the scanner.
        """
        from trading.asset_manager import AssetManager, AssetRegistryError

        try:
            manager = AssetManager(settings=cfg)
        except AssetRegistryError:
            return
        for name in (manager.names() if names is None else names):
            if not manager.has(name):
                continue
            asset = manager.get(name)
            try:
                repo.upsert_asset(asset.name, asset.broker_symbol, asset.enabled,
                                  digits=asset.digits, overrides=asset.overrides)
            except Exception:  # a mirror failure must not undo a registry write
                pass

    @app.post("/api/assets/add")
    def api_assets_add():
        if not _origin_allowed():
            abort(403)
        data = _payload()
        name = (data.get("name") or "").strip().upper()
        symbol = (data.get("broker_symbol") or "").strip()
        if not name or not symbol:
            return jsonify({"ok": False, "reason": "missing_fields",
                            "message": "Both a name and a broker symbol are "
                                       "required."}), 400
        if len(name) > 64 or len(symbol) > 64:
            return jsonify({"ok": False, "reason": "too_long",
                            "message": "Name and symbol must be 64 characters "
                                       "or fewer."}), 400

        digits = _parse_int(data.get("digits"), default=0, minimum=0, maximum=8)
        if digits is None:
            return jsonify({"ok": False, "reason": "bad_digits",
                            "message": "digits must be between 0 and 8."}), 400
        enabled = str(data.get("enabled", "")).strip().lower() in {"1", "true",
                                                                  "yes", "on"}

        manager, error = _registry()
        if manager is None:
            return jsonify({"ok": False, "reason": "registry_unreadable",
                            "message": error}), 500
        if manager.has(name):
            return jsonify({"ok": False, "reason": "already_exists",
                            "message": f"{name} is already in the registry."}), 409

        manager.add_asset(name, symbol, enabled=enabled, digits=digits)
        _sync_assets(g.repo, [name])
        g.repo.log_event("INFO", "dashboard",
                         f"asset added: {name} -> {symbol} "
                         f"({'enabled' if enabled else 'disabled'})")
        return jsonify({"ok": True, "message": f"Added {name} ({symbol}).",
                        "assets": asset_choices(cfg, g.repo)})

    @app.post("/api/assets/toggle")
    def api_assets_toggle():
        if not _origin_allowed():
            abort(403)
        data = _payload()
        name = (data.get("name") or "").strip()
        enabled = str(data.get("enabled", "")).strip().lower() in {"1", "true",
                                                                   "yes", "on"}
        manager, error = _registry()
        if manager is None:
            return jsonify({"ok": False, "reason": "registry_unreadable",
                            "message": error}), 500
        if not manager.has(name):
            return jsonify({"ok": False, "reason": "unknown_asset",
                            "message": f"Unknown asset {name!r}."}), 404

        manager.set_enabled(name, enabled)
        _sync_assets(g.repo, [name])
        g.repo.log_event("INFO", "dashboard",
                         f"asset {name} {'enabled' if enabled else 'disabled'}")
        return jsonify({"ok": True,
                        "message": f"{name} {'enabled' if enabled else 'disabled'}.",
                        "assets": asset_choices(cfg, g.repo)})

    @app.post("/api/assets/remove")
    def api_assets_remove():
        if not _origin_allowed():
            abort(403)
        name = (_payload().get("name") or "").strip()
        manager, error = _registry()
        if manager is None:
            return jsonify({"ok": False, "reason": "registry_unreadable",
                            "message": error}), 500
        if not manager.has(name):
            return jsonify({"ok": False, "reason": "unknown_asset",
                            "message": f"Unknown asset {name!r}."}), 404

        # Removing the last enabled asset would leave the scanner with nothing
        # to scan (see JobManager._live_worker) — refuse rather than break it.
        remaining = [a.name for a in manager.enabled_assets() if a.name != name]
        if manager.get(name).enabled and not remaining:
            return jsonify({"ok": False, "reason": "last_enabled",
                            "message": "That is the last enabled asset. Enable "
                                       "another one before removing it."}), 409

        manager.remove_asset(name)
        g.repo.log_event("INFO", "dashboard", f"asset removed: {name}")
        return jsonify({"ok": True, "message": f"Removed {name}.",
                        "assets": asset_choices(cfg, g.repo)})

    @app.post("/api/assets/set-all")
    def api_assets_set_all():
        """Enable or disable every registry entry in one action.

        Disabling the whole list is allowed: it is a one-click pause, and the
        live worker already reports "No enabled assets" cleanly. Removing the
        last asset stays refused (see ``api_assets_remove``) because that one is
        destructive rather than merely reversible.
        """
        if not _origin_allowed():
            abort(403)
        enabled = str(_payload().get("enabled", "")).strip().lower() in {
            "1", "true", "yes", "on"}

        manager, error = _registry()
        if manager is None:
            return jsonify({"ok": False, "reason": "registry_unreadable",
                            "message": error}), 500

        changed = manager.set_all_enabled(enabled)
        _sync_assets(g.repo)
        verb = "enabled" if enabled else "disabled"
        g.repo.log_event("INFO", "dashboard",
                         f"bulk {verb}: {len(changed)} asset(s)")
        return jsonify({"ok": True,
                        "message": f"{verb.capitalize()} {len(changed)} asset(s).",
                        "assets": asset_choices(cfg, g.repo)})

    # ------------------------------------------------------------------ #
    # Broker symbol catalogue
    #
    # The registry can only ever offer what someone typed into it. These routes
    # let the dashboard show what the *broker* actually offers, so any instrument
    # can be picked rather than only the hand-listed ones.
    # ------------------------------------------------------------------ #
    @app.get("/api/assets/broker")
    def api_broker_catalog():
        """The symbol list from the last scan, plus that scan's state.

        Fetched on demand rather than polled: a full broker catalogue is hundreds
        of rows and would dominate the five-second ``/api/status`` payload.

        ``in_registry`` is resolved here so the browser can mark and filter rows
        without shipping the registry to the client a second time. A registry
        entry claims both its own name and its ``broker_symbol``: ``GOLD``
        pointing at ``XAUUSDm`` means that symbol is already covered, and
        offering to add it again would create a second entry for one instrument.
        """
        from trading.asset_manager import AssetManager, AssetRegistryError

        try:
            entries = AssetManager(settings=cfg).list_assets()
            known = {a.name for a in entries} | {a.broker_symbol for a in entries}
        except AssetRegistryError:
            known = set()

        symbols = []
        for row in jobs.broker_catalog():
            entry = dict(row)
            entry["in_registry"] = entry.get("name") in known
            symbols.append(entry)
        return jsonify({"ok": True, "broker": jobs.broker_state(),
                        "symbols": symbols})

    @app.post("/api/assets/broker/scan")
    def api_broker_scan():
        if not _origin_allowed():
            abort(403)
        result = jobs.request_broker_scan()
        g.repo.log_event("INFO" if result["ok"] else "WARN", "dashboard",
                         f"broker scan: {result.get('reason') or 'started'}")
        return jsonify(result), (200 if result["ok"] else 409)

    @app.post("/api/assets/add-broker")
    def api_assets_add_broker():
        """Add one of the broker's own symbols to the registry.

        Separate from ``/api/assets/add`` because that route upper-cases the name
        — right for a hand-typed label, wrong for ``EURUSD.pro`` — and knows
        nothing about the broker's contract numbers. Here the registry name *is*
        the broker symbol, verbatim, so symbol resolution hits its exact-match
        tier immediately, and the broker's own spec is persisted as the offline
        fallback that ``SymbolSpec.from_asset`` reads when no terminal is present.
        """
        from trading.symbol_spec import SymbolSpec

        if not _origin_allowed():
            abort(403)
        symbol = (_payload().get("symbol") or "").strip()
        if not symbol:
            return jsonify({"ok": False, "reason": "missing_symbol",
                            "message": "Pick a symbol to add."}), 400

        # Validate against the scanned catalogue so a browser page left open
        # cannot inject a symbol the broker was never shown to offer.
        row = next((r for r in jobs.broker_catalog()
                    if str(r.get("name", "")) == symbol), None)
        if row is None:
            return jsonify({"ok": False, "reason": "unknown_symbol",
                            "message": f"{symbol} is not in the scanned symbol "
                                       "list. Re-scan the broker and try again."}), 404

        manager, error = _registry()
        if manager is None:
            return jsonify({"ok": False, "reason": "registry_unreadable",
                            "message": error}), 500

        # Matching the broker symbol as well as the name: ``GOLD`` already
        # pointing at ``XAUUSDm`` means adding ``XAUUSDm`` would put two registry
        # entries on one instrument, and the scanner would trade it twice.
        existing = next((a for a in manager.list_assets()
                         if a.name == symbol or a.broker_symbol == symbol), None)
        if existing is not None:
            return jsonify({"ok": False, "reason": "already_exists",
                            "message": f"{symbol} is already covered by "
                                       f"{existing.name}."}), 409

        spec = SymbolSpec.from_mt5_info(symbol, row)
        manager.add_asset(
            symbol, symbol,
            # Added switched off: the scanner should only pick up an instrument
            # the user deliberately turned on (see the note in assets.json).
            enabled=False,
            digits=spec.digits,
            contract_size=spec.contract_size,
            volume_min=spec.volume_min,
            volume_step=spec.volume_step,
            volume_max=spec.volume_max,
        )
        _sync_assets(g.repo, [symbol])
        g.repo.log_event("INFO", "dashboard",
                         f"asset added from broker: {symbol} (disabled)")
        return jsonify({"ok": True,
                        "message": f"Added {symbol} — enable it to start scanning.",
                        "assets": asset_choices(cfg, g.repo)})

    @app.post("/api/account/refresh")
    def api_account_refresh():
        """Take a fresh account snapshot for the dashboard balance tile.

        Explicitly requested rather than polled. Reading the account means
        holding the MT5 terminal, which the live session and the background jobs
        contend for, so it cannot ride along on the five-second ``/api/status``
        poll. While a live session runs the worker snapshots the account every
        poll of its own, and this route says so rather than failing.
        """
        if not _origin_allowed():
            abort(403)
        result = jobs.request_account_refresh()
        g.repo.log_event("INFO" if result["ok"] else "WARN", "dashboard",
                         f"account refresh: {result.get('reason') or 'started'}")
        return jsonify(result), (200 if result["ok"] else 409)

    @app.post("/api/auto-trading")
    def api_auto_trading():
        """Set or clear the runtime auto-trading override.

        This *operates* the master switch rather than weakening it. Every gate
        below it in :mod:`trading.executor` still applies to every signal, and
        the override is session-only — it is never written to ``.env``, so a
        restart puts the bot back on the ``.env`` baseline and it cannot come
        back up armed.

        Presence carries the tri-state: a body with no ``enabled`` key clears
        the override and hands control back to ``.env``, which is the only way
        to reach that state without restarting.
        """
        if not _origin_allowed():
            abort(403)
        raw = _payload().get("enabled", None)
        enabled = (None if raw is None
                   else str(raw).strip().lower() in {"1", "true", "yes", "on"})

        result = jobs.set_auto_trading(enabled)
        # Arming is the notable event, so it is recorded as a warning; disarming
        # and resetting are routine, and recorded as information.
        g.repo.log_event(
            "WARN" if result["auto_trading"] else "INFO", "dashboard",
            f"auto-trading override: {enabled!r} (effective="
            f"{result['auto_trading']}, baseline={result['baseline']})")
        return jsonify(result)

    @app.get("/api/status")
    def api_status():
        payload = jobs.status()
        live = _add_ny_times(payload["live"], ("started_at_utc", "stopped_at_utc"))
        last_candle = live.get("last_candle_utc")
        live["last_candle_ny"] = (tu.utc_to_ny(last_candle).strftime("%Y-%m-%d %H:%M")
                                  if last_candle else "")
        payload["backtest"] = _add_ny_times(payload["backtest"], ("finished_at_utc",))
        payload["probe"] = _add_ny_times(payload["probe"],
                                         ("oldest_utc", "newest_utc",
                                          "finished_at_utc"))
        # The balance tile is only meaningful with the time it was read, so the NY
        # rendering of the snapshot timestamp travels with the snapshot itself.
        payload["account"] = _add_ny_times(payload["account"], ("fetched_at_utc",))

        after = request.args.get("after_signal_id", type=int)
        payload["signals"] = {
            # ``last_id`` is the polling cursor -- the highest id seen, which the
            # browser echoes back to ask for anything newer. ``total`` is the
            # genuine count, which is what the dashboard tile displays; the two
            # are not interchangeable if rows are ever removed.
            "last_id": g.repo.max_signal_id(),
            "total": g.repo.count_signals(),
            "new": ([_signal_json(r) for r in g.repo.signals_after_id(after)]
                    if after is not None else []),
        }
        payload["telegram"] = {
            "enabled": cfg.telegram_enabled,
            "configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
        }
        # The effective switch and the .env baseline both travel, so the
        # dashboard can show "overridden" rather than silently disagreeing with
        # .env. Only the deliberate route above can move the effective value.
        payload["auto_trading"] = cfg.effective_auto_trading
        payload["auto_trading_baseline"] = cfg.auto_trading
        payload["auto_trading_override"] = cfg.auto_trading_override
        payload["assets"] = asset_choices(cfg, g.repo)
        payload["backtest_defaults"] = {"bars": cfg.backtest_m1_bars}
        payload["ai"] = ai_status(cfg)
        return jsonify(payload)


def ai_status(cfg: Settings) -> dict:
    """Read-only description of the analysis layer for the dashboard.

    Never includes ``llm_api_key`` — the dashboard reports *what* is configured,
    not the credentials. The AI is an advisory overlay that cannot alter entry,
    SL or TP, so this is presentation only; ``AI_ENABLED`` stays a ``.env``
    setting with no route that can change it.
    """
    host = urlparse(cfg.llm_api_url).netloc or ""
    return {
        "enabled": cfg.ai_enabled,
        "model": cfg.llm_model,
        "provider_host": host,
    }


def asset_choices(cfg: Settings, repo=None) -> list[dict]:
    """Assets for the dashboard dropdowns.

    The **registry is authoritative**: it is what the scanner and backtester
    actually read, so a dropdown built from any other source could offer an
    asset the engine will not run. The ``assets`` DB table is only a fallback
    for a database that has rows but no readable registry file.
    """
    rows = registry_assets(cfg)
    if rows:
        return rows
    if repo is not None:
        return [{"name": a.name, "broker_symbol": a.broker_symbol,
                 "enabled": a.enabled, "digits": a.digits}
                for a in repo.list_assets()]
    return []


def registry_assets(cfg: Settings) -> list[dict]:
    """Read the asset registry from ``assets.json`` (no DB, no MT5)."""
    from trading.asset_manager import AssetManager, AssetRegistryError

    try:
        return [{"name": a.name, "broker_symbol": a.broker_symbol,
                 "enabled": a.enabled, "digits": a.digits}
                for a in AssetManager(settings=cfg).list_assets()]
    except AssetRegistryError:
        return []
