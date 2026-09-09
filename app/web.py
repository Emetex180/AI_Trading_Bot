"""Flask dashboard (read-only monitoring UI).

The dashboard is deliberately **read-only**: it renders signals, trades and
backtests persisted by the scanner, and shows the live safety configuration
(AUTO_TRADING state, risk policy). It performs no MT5, AI or Telegram I/O, so
it runs even when the terminal is closed. Auto-trading can never be enabled
from this UI.

:func:`create_app` accepts an injected :class:`database.repository.Repository`
so tests can drive the full UI against an in-memory SQLite database.
"""
from __future__ import annotations

from math import isinf

from flask import Flask, abort, g, jsonify, render_template, request
from sqlalchemy.orm import sessionmaker

from config import Settings, get_settings
from database.models import Base
from database.repository import Repository, get_engine
from trading import time_utils as tu

_TEMPLATES = "templates"


# --------------------------------------------------------------------------- #
# Template helpers
# --------------------------------------------------------------------------- #
def _ny_str(naive_utc):
    """Render a naive-UTC datetime on the NY (UTC-4) clock."""
    if naive_utc is None:
        return ""
    return tu.utc_to_ny(naive_utc).strftime("%Y-%m-%d %H:%M")


def _num(value, digits: int = 4):
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _status_label(status: str) -> str:
    return {"APPROVED": "Approved", "REJECTED": "Rejected", "PENDING": "Pending",
            "SENT": "Sent", "SKIPPED": "Skipped", "FAILED": "Failed",
            "AI_UNAVAILABLE": "AI Unavailable"}.get(status or "", status or "-")


def _profit_factor(value) -> str:
    """Render a profit factor, collapsing an infinite (no-loss) value to ∞."""
    if value is None:
        return "-"
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return str(value)
    if isinf(fv):
        return "∞"
    return f"{fv:.2f}"


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(settings: Settings | None = None,
               repository: Repository | None = None,
               setup_db: bool = True) -> Flask:
    """Build the dashboard app.

    ``repository`` injects a pre-built Repository (e.g. in-memory for tests);
    when omitted the app opens a fresh SQLite-backed engine per configuration.
    """
    cfg = settings or get_settings()

    if repository is None:
        engine = get_engine(cfg)
        if setup_db:
            Base.metadata.create_all(engine)
        factory: sessionmaker | None = sessionmaker(bind=engine,
                                                    expire_on_commit=False,
                                                    future=True)
    else:
        factory = None

    app = Flask(__name__, template_folder=_TEMPLATES,
                static_folder="static", static_url_path="/static")
    app.config["CFG"] = cfg
    app.config["SESSION_FACTORY"] = factory
    app.config["FIXED_REPO"] = repository

    app.jinja_env.filters["ny"] = _ny_str
    app.jinja_env.filters["num"] = _num
    app.jinja_env.filters["status"] = _status_label
    app.jinja_env.filters["pf"] = _profit_factor

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
    # Routes
    # ------------------------------------------------------------------ #
    @app.get("/")
    def index():
        repo = g.repo
        assets = repo.list_assets()
        stats = {
            "signals_total": repo.count_signals(),
            "signals_approved": repo.count_signals("APPROVED"),
            "signals_rejected": repo.count_signals("REJECTED"),
            "trades_total": repo.count_trades(),
            "trades_sent": repo.count_trades("SENT"),
            "trades_skipped": repo.count_trades("SKIPPED"),
            "backtests": repo.count_backtests(),
            "assets_enabled": sum(1 for a in assets if a.enabled),
            "assets_total": len(assets),
        }
        return render_template(
            "index.html",
            stats=stats,
            recent_signals=repo.recent_signals(12),
            recent_events=repo.recent_events(8),
            assets=[a for a in assets if a.enabled],
            cfg=cfg,
        )

    @app.get("/signals")
    def signals_page():
        repo = g.repo
        asset = (request.args.get("asset") or "").strip() or None
        status = (request.args.get("status") or "").strip() or None
        rows = repo.find_signals(status=status, asset=asset, limit=500)
        return render_template(
            "signals.html",
            rows=rows,
            assets=repo.list_assets(),
            filter_asset=asset,
            filter_status=status,
        )

    @app.get("/signals/<int:signal_id>")
    def signal_detail(signal_id: int):
        repo = g.repo
        row = repo.get_signal(signal_id)
        if row is None:
            abort(404)
        executions = repo.trades_for_fingerprint(row.fingerprint)
        return render_template("signal_detail.html", s=row, executions=executions)

    @app.get("/backtests")
    def backtests_page():
        repo = g.repo
        return render_template("backtests.html", rows=repo.recent_backtests(100))

    @app.get("/backtests/<int:bt_id>")
    def backtest_detail(bt_id: int):
        repo = g.repo
        bt = repo.get_backtest(bt_id)
        if bt is None:
            abort(404)
        trades = repo.backtest_trades(bt_id)
        curve = (bt.summary_json or {}).get("equity_curve") or []
        return render_template(
            "backtest_detail.html",
            bt=bt,
            summary=bt.summary_json or {},
            params=bt.params_json or {},
            trades=trades,
            curve=curve,
        )

    @app.get("/health")
    def health():
        repo = g.repo
        return jsonify({"status": "ok", "signals": repo.count_signals()})

    return app
