"""Flask application factory: the operator console, the client platform and
the admin area, all built on one app.

Three surfaces, one process:

* **Client platform** (:mod:`app.client`, ``/dashboard`` and friends) — the
  product. Read-only views of what the engine produced. Any authenticated user.
* **Admin area** (:mod:`app.admin`, ``/admin``) — clients, activity, broker
  accounts. Platform admins only.
* **Operator console** (this module, ``/console``) — the original dashboard:
  signals, trades, backtests, the asset registry, and the control routes in
  :mod:`app.api` that start/stop a live session. Platform admins only.

What the console still cannot do: reach past the master switch. ``AUTO_TRADING``
in ``.env`` is the baseline, and the console can set a session-only override on
top of it (:func:`app.api.api_auto_trading`) — deliberately never persisted, so
a restart returns the bot to the ``.env`` value. No route here can touch any
other gate in :mod:`trading.executor`.

Authentication is wired in :func:`create_app`. The ordering constraint that
matters: :func:`app.auth.register_auth` **must** run after the ``before_request``
hook that opens ``g.repo``, because the session loader reads the user row
through that repository. Registering it earlier leaves ``g.repo`` unset on the
very first request and every logged-in session silently fails to load.

``/`` is role-aware — it sends a client to their dashboard and an admin to the
console — so the domain root is the product rather than an operator tool.

Routes that render pages touch no MT5, AI or Telegram, so they keep working when
the terminal is closed; the control routes start work on a background thread
rather than blocking a request.

:func:`create_app` accepts injected :class:`database.repository.Repository` and
:class:`runner.JobManager` instances so tests can drive the full UI against an
in-memory database with no terminal.
"""
from __future__ import annotations

from flask import (Flask, abort, current_app, g, jsonify, redirect,
                   render_template, request, url_for)
from sqlalchemy.orm import sessionmaker

from backtesting.compare import batch_totals, rank_assets
from backtesting.compare import tidy as tidy_summary
from config import Settings, get_settings
from database.repository import Repository, ensure_schema, get_engine
from trading import time_utils as tu

from .admin import register_admin
from .api import asset_choices, register_api
from .auth import (admin_required, bootstrap_admin, current_user,
                   register_auth)
from .client import register_client
# Shared presentation helpers. These used to live here; they moved to
# ``app/display.py`` when the client platform needed the same price precision
# and New York rendering, so both surfaces format a value identically. Aliased
# to their old private names because the template filters below and a handful of
# call sites still read better that way.
from .display import digits_for as _digits_for
from .display import iso_dt as _iso_dt
from .display import money as _money
from .display import num as _num
from .display import ny_str as _ny_str
from .display import price as _price
from .display import profit_factor as _profit_factor
from .display import span as _span
from .display import status_label as _status_label

#: Console routes are admin-only. Kept as a name so the intent is greppable and
#: a future role (say, a read-only analyst) can be swapped in one place.
_console_only = admin_required

_TEMPLATES = "templates"

#: Human labels for breakdown bucket keys (sessions / Silver Bullet windows).
_WINDOW_LABELS: dict[str, str] = {}


def _window_label(key: str) -> str:
    """Display label for a session or Silver Bullet bucket key."""
    if not _WINDOW_LABELS:
        from trading.sessions import CORE_SESSIONS, SILVER_BULLET_WINDOWS

        for w in (*CORE_SESSIONS, *SILVER_BULLET_WINDOWS):
            _WINDOW_LABELS[w.key] = w.label
        _WINDOW_LABELS["outside"] = "Outside any window"
    return _WINDOW_LABELS.get(key, key)


def _asset_summaries(rows) -> list[dict]:
    """Per-asset summary dicts, with the row's own identity overlaid.

    ``summary_json`` is written by the backtester and normally carries ``asset``
    as well, but the row is the authoritative record of which instrument it is,
    so the comparison never depends on the JSON blob being complete.
    """
    return [{**(r.summary_json or {}), "asset": r.asset, "symbol": r.symbol,
             "backtest_id": r.id} for r in rows]


def _breakdown_matrix(ranked: list[dict], field: str) -> list[dict]:
    """Per-asset cells for one breakdown dimension, pooled and best-first.

    Rows are the bucket keys present anywhere in the batch (so a session that
    only one asset traded still appears); ``cells`` is aligned to ``ranked``,
    with ``None`` where that asset had no trades in the bucket.
    """
    keys = sorted({k for row in ranked for k in (row.get(field) or {})})
    matrix = []
    for key in keys:
        cells = [(row.get(field) or {}).get(key) for row in ranked]
        present = [c for c in cells if c]
        n_trades = sum(c["n_trades"] for c in present)
        total_r = sum(c["total_r"] for c in present)
        matrix.append({
            "key": key,
            "label": _window_label(key),
            "cells": cells,
            "n_trades": n_trades,
            "total_r": round(total_r, 4),
            "expectancy": round(total_r / n_trades, 4) if n_trades else 0.0,
        })
    matrix.sort(key=lambda r: r["expectancy"], reverse=True)
    return matrix


# --------------------------------------------------------------------------- #
# Template helpers
#
# The number, money, price, status, profit-factor, NY-time and span helpers that
# used to sit here now live in ``app/display.py`` — shared with the client and
# admin surfaces so all three format a value the same way. They are imported at
# the top of this module under their original private names.
# --------------------------------------------------------------------------- #


#: The period breakdowns the detail page offers, as
#: ``(toggle key, label, summary attribute, note)``. Ordered coarse to fine, and
#: the first is the default view.
_PERIODS: tuple[tuple[str, str, str, str], ...] = (
    ("month", "Month", "by_month",
     "Calendar months on the NY clock. The bucket is the month the setup "
     "triggered in, not the month it closed in."),
    ("week", "Week", "by_week",
     "ISO weeks (Monday-based). A week belongs to the year holding its "
     "Thursday, so a January date can sit in the previous year's final week."),
    ("day_of_week", "Day of week", "by_day_of_week",
     "Weekday of the entry. A strategy that only pays on two days of the week "
     "is a different proposition from one that pays every day."),
    ("hour", "Hour of day", "by_hour",
     "NY-clock hour the setup triggered in — the same time base as the session "
     "and Silver Bullet breakdowns."),
)


def _period_rows(breakdown: dict) -> list[dict]:
    """One table row per bucket, already ordered by the engine.

    The engine owns the ordering (chronological for months and weeks, calendar
    for weekdays) because it is a statement about the data, not about the
    markup; this only shapes it for the template.
    """
    return [{"bucket": key, **stats} for key, stats in (breakdown or {}).items()]


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(settings: Settings | None = None,
               repository: Repository | None = None,
               setup_db: bool = True,
               jobs=None) -> Flask:
    """Build the dashboard app.

    ``repository`` injects a pre-built Repository (e.g. in-memory for tests);
    when omitted the app opens a fresh SQLite-backed engine per configuration.

    ``jobs`` injects the background job manager. When omitted a real
    :class:`runner.JobManager` is built from the configuration — pass a fake in
    tests so no test can ever reach MT5.
    """
    cfg = settings or get_settings()

    if repository is None:
        engine = get_engine(cfg)
        if setup_db:
            # Full schema setup, not just ``create_all`` — an existing database
            # needs its added columns back-filled too, and the dashboard may be
            # the first thing a user starts. See ``ensure_schema``.
            ensure_schema(engine)
        factory: sessionmaker | None = sessionmaker(bind=engine,
                                                    expire_on_commit=False,
                                                    future=True)
    else:
        factory = None

    if jobs is None:
        from runner import JobManager

        jobs = JobManager(settings=cfg)

    app = Flask(__name__, template_folder=_TEMPLATES,
                static_folder="static", static_url_path="/static")
    app.config["CFG"] = cfg
    app.config["SESSION_FACTORY"] = factory
    app.config["FIXED_REPO"] = repository
    app.config["JOBS"] = jobs

    app.jinja_env.filters["ny"] = _ny_str
    app.jinja_env.filters["num"] = _num
    app.jinja_env.filters["money"] = _money
    app.jinja_env.filters["price"] = _price
    app.jinja_env.filters["status"] = _status_label
    app.jinja_env.filters["pf"] = _profit_factor

    @app.context_processor
    def _inject_settings():
        """Expose the configuration to every template.

        The auto-trading badge in ``base.html`` is a safety affordance: whether
        the bot may place orders must be visible on *every* page, not only on the
        routes that happen to pass ``cfg`` through explicitly.

        ``ny_zone`` / ``ny_offset_hours`` travel for the same reason: the charts
        label their axes in the browser, and the browser must resolve the *same*
        NY clock the server's ``ny`` filter does
        (``trading.time_utils`` → America/New_York). The zone is what the
        browser actually needs; ``ny_offset_hours`` is the current DST-aware
        offset, kept as the fallback for engines without a timezone database.
        """
        return {"cfg": cfg, "ny_zone": tu.NY_TZ_NAME,
                "ny_offset_hours": tu.ny_offset_hours()}

    # ------------------------------------------------------------------ #
    # Repository per request
    # ------------------------------------------------------------------ #
    @app.before_request
    def _open_repo():
        if app.config["FIXED_REPO"] is not None:
            g.repo = app.config["FIXED_REPO"]
            g.owns_repo = False
        else:
            g.repo = Repository(settings=cfg, session=app.config["SESSION_FACTORY"]())
            g.owns_repo = True

    @app.teardown_request
    def _close_repo(exc=None):
        repo = getattr(g, "repo", None)
        if repo is not None and getattr(g, "owns_repo", False):
            repo.close()

    # ------------------------------------------------------------------ #
    # Authentication
    #
    # Strictly *after* ``_open_repo`` above: the session loader reads the user
    # row through ``g.repo``, so registering this first would leave every
    # logged-in session unable to resolve its user.
    # ------------------------------------------------------------------ #
    register_auth(app)

    # ------------------------------------------------------------------ #
    # Root
    # ------------------------------------------------------------------ #
    @app.get("/")
    def root():
        """The domain root, resolved by who is asking.

        A logged-out visitor gets the login page; a client gets the product; an
        admin gets the console. Deliberately a redirect rather than a rendered
        page so there is exactly one canonical URL per surface — a client can
        bookmark ``/dashboard`` and never see a second copy of it at ``/``.
        """
        user = current_user()
        if user is None:
            return redirect(url_for("auth.login"))
        if user.is_admin:
            return redirect(url_for("index"))
        return redirect(url_for("client.overview"))

    # ------------------------------------------------------------------ #
    # Operator console
    #
    # These routes keep the endpoint *names* they have always had, so every
    # ``url_for()`` in the existing templates keeps working; only the URLs move
    # under ``/console`` and gain the admin guard. The console can start and
    # stop a live trading session, so it is not client-facing.
    # ------------------------------------------------------------------ #
    @app.get("/console")
    @_console_only
    def index():
        repo = g.repo
        # The registry, not the DB mirror. ``assets.json`` is what the scanner and
        # the backtester actually read, so these counts must describe that list —
        # otherwise the dashboard reports assets the engine will never run.
        assets = asset_choices(cfg, repo)
        stats = {
            "signals_total": repo.count_signals(),
            "signals_approved": repo.count_signals("APPROVED"),
            "signals_rejected": repo.count_signals("REJECTED"),
            "trades_total": repo.count_trades(),
            "trades_sent": repo.count_trades("SENT"),
            "trades_skipped": repo.count_trades("SKIPPED"),
            "backtests": repo.count_backtests(),
            "assets_enabled": sum(1 for a in assets if a["enabled"]),
            "assets_total": len(assets),
        }
        # Rendered once so the page is useful before the first poll returns.
        job_state = jobs.status()
        return render_template(
            "index.html",
            stats=stats,
            job_state=job_state,
            recent_signals=repo.recent_signals(12),
            recent_events=repo.recent_events(8),
            # The whole registry, not just the enabled slice: the card marks each
            # entry on/off, so a disabled asset is visibly present rather than
            # silently missing. ``job_state`` already carries the last account
            # snapshot, so the balance tile paints on the first byte too.
            assets=assets,
            telegram_ready=(cfg.telegram_enabled and cfg.telegram_bot_token
                            and cfg.telegram_chat_id),
        )

    @app.get("/console/signals")
    @_console_only
    def signals_page():
        repo = g.repo
        asset = (request.args.get("asset") or "").strip() or None
        status = (request.args.get("status") or "").strip() or None
        rows = repo.find_signals(status=status, asset=asset, limit=500)
        return render_template(
            "signals.html",
            rows=rows,
            assets=asset_choices(cfg, repo),
            filter_asset=asset,
            filter_status=status,
        )

    @app.get("/console/signals/<int:signal_id>")
    @_console_only
    def signal_detail(signal_id: int):
        repo = g.repo
        row = repo.get_signal(signal_id)
        if row is None:
            abort(404)
        executions = repo.trades_for_fingerprint(row.fingerprint)
        return render_template("signal_detail.html", s=row, executions=executions)

    @app.get("/console/backtests")
    @_console_only
    def backtests_page():
        repo = g.repo
        batches = []
        for entry in repo.recent_batches(limit=20):
            summaries = _asset_summaries(entry["rows"])
            ranked = rank_assets(summaries)
            batches.append({
                **entry,
                "totals": batch_totals(summaries),
                "best": ranked[0] if ranked else None,
                "is_single": entry["batch_id"].startswith("single:"),
                "duration": _span(entry.get("start_utc"), entry.get("end_utc")),
            })
        return render_template(
            "backtests.html",
            batches=batches,
            assets=asset_choices(cfg, repo),
            job_state=jobs.status(),
            default_bars=cfg.backtest_m1_bars,
        )

    @app.get("/console/backtests/batch/<batch_id>")
    @_console_only
    def backtest_batch_page(batch_id: str):
        """Cross-asset comparison for one backtest campaign."""
        repo = g.repo
        if batch_id.startswith("single:"):
            # Legacy/pre-batching row, surfaced as a one-asset batch so old
            # history stays reachable through the same route.
            raw = batch_id.split(":", 1)[1]
            row = repo.get_backtest(int(raw)) if raw.isdigit() else None
            rows = [row] if row is not None else []
        else:
            rows = repo.backtest_batch(batch_id)
        if not rows:
            abort(404)

        summaries = _asset_summaries(rows)
        ranked = rank_assets(summaries)
        comparison = ranked
        start_utc = min((r.start_utc for r in rows if r.start_utc), default=None)
        end_utc = max((r.end_utc for r in rows if r.end_utc), default=None)

        return render_template(
            "backtests_batch.html",
            batch_id=batch_id,
            comparison=comparison,
            totals=batch_totals(summaries),
            best=comparison[0] if comparison else None,
            session_matrix=_breakdown_matrix(ranked, "by_session"),
            silver_bullet_matrix=_breakdown_matrix(ranked, "by_silver_bullet"),
            start_utc=start_utc,
            end_utc=end_utc,
            duration=_span(start_utc, end_utc),
        )

    @app.get("/console/backtests/<int:bt_id>")
    @_console_only
    def backtest_detail(bt_id: int):
        repo = g.repo
        bt = repo.get_backtest(bt_id)
        if bt is None:
            abort(404)
        trades = repo.backtest_trades(bt_id)
        sm = tidy_summary(bt.summary_json)
        curve = sm.get("equity_curve") or []

        # Stated explicitly rather than left for the reader to subtract: the
        # replayed window and the window actually traded in are different spans,
        # and a run whose first signal arrives three weeks in should say so.
        window = {
            "start_utc": bt.start_utc,
            "end_utc": bt.end_utc,
            "duration": _span(bt.start_utc, bt.end_utc),
            "n_bars": sm.get("n_bars") or 0,
            "first_entry_utc": _iso_dt(sm.get("first_entry_utc")),
            "last_exit_utc": _iso_dt(sm.get("last_exit_utc")),
        }
        return render_template(
            "backtest_detail.html",
            bt=bt,
            summary=sm,
            params=bt.params_json or {},
            trades=trades,
            curve=curve,
            window=window,
            periods=[{"key": key, "label": label, "note": note,
                      "rows": _period_rows(sm.get(attr))}
                     for key, label, attr, note in _PERIODS],
        )

    @app.get("/console/assets")
    @_console_only
    def assets_page():
        """Manage the registry, and browse what the broker actually offers.

        Renders ``assets.json`` (authoritative) plus whatever the last broker scan
        produced. No MT5 is touched here, so the page still loads with the
        terminal closed — the scan itself is kicked from the browser, and its
        result arrives through ``/api/assets/broker``.
        """
        registry = asset_choices(cfg, g.repo)
        # An entry claims its own name and its broker symbol, so a symbol already
        # covered by a differently-named entry is still shown as taken.
        known = ({a["name"] for a in registry}
                 | {a["broker_symbol"] for a in registry})
        catalog = [dict(row, in_registry=row.get("name") in known)
                   for row in jobs.broker_catalog()]
        return render_template(
            "assets.html",
            assets=registry,
            catalog=catalog,
            broker=jobs.broker_state(),
            assets_enabled=sum(1 for a in registry if a["enabled"]),
        )

    @app.get("/health")
    def health():
        """Liveness probe. Deliberately public and deliberately thin.

        A health check is polled by whatever is watching the process — a reverse
        proxy, an uptime monitor, ``run.py smoke`` — none of which can hold a
        session. So it answers the one question that needs no identity: is this
        process serving? It reports no counts and no account state, because
        those are operator data and this route has no way to check who is
        asking.
        """
        return jsonify({"status": "ok", "live_running": jobs.is_live_running()})

    # Control endpoints (start/stop live, run backtest, poll status).
    register_api(app)
    # Client-facing product, and the admin area.
    register_client(app)
    register_admin(app)

    # First-run admin, from ADMIN_USERNAME/ADMIN_PASSWORD. Only ever fires while
    # the users table is empty, so it cannot touch a live installation's
    # accounts. Wrapped because a database that is unreachable must not stop the
    # app from starting — an operator can still fix it and restart.
    try:
        with _repo_scope(app, cfg, repository) as repo:
            bootstrap_admin(repo, cfg)
    except Exception:  # pragma: no cover - startup best-effort
        import logging
        logging.getLogger(__name__).exception(
            "Could not bootstrap the admin account; continuing without it.")

    return app


class _repo_scope:
    """A repository for one startup task, closed on exit.

    Only used by the bootstrap above, which runs outside any request and so
    cannot use the ``g.repo`` the request hooks provide.
    """

    def __init__(self, app, cfg, injected):
        self._injected = injected
        self._cfg = cfg
        self._factory = app.config["SESSION_FACTORY"]

    def __enter__(self) -> Repository:
        self._repo = (self._injected if self._injected is not None
                      else Repository(settings=self._cfg, session=self._factory()))
        return self._repo

    def __exit__(self, *exc):
        # An injected repo belongs to the caller, so it is never closed here.
        if self._injected is None:
            self._repo.close()
        return False
