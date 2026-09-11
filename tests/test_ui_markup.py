"""UI contract tests: the templates and the poller must agree.

``dashboard.js`` reaches into the rendered page by id, and silently does nothing
when an id is absent — a renamed element degrades to a control that quietly
stops working, with no error anywhere. These tests pin that contract, plus the
presentation vocabulary, so a restyle cannot break the live controls.

Deliberately built on rendered HTML rather than on template source: what matters
is what the browser receives.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from app.web import create_app
from trading.executor import ExecutionResult

from test_database import _signal
from test_web import _FakeJobs, _repo


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
#: Every id ``dashboard.js`` looks up, grouped by the page that must provide it.
#: A control only works on a page whose markup carries it.
POLLER_IDS = {
    "dashboard": ["live-pill", "live-pill-text", "live-state-badge",
                  "live-started", "live-assets", "live-last-candle",
                  "live-signals", "live-start", "live-stop", "live-message",
                  "live-error", "new-signals", "stat-signals-total"],
    "backtests": ["backtest-form", "bt-asset", "bt-bars", "bt-hold",
                  "bt-start", "bt-end", "bt-run", "backtest-message",
                  "backtest-state-badge", "backtest-progress",
                  "backtest-progress-bar", "backtest-error"],
}

#: Provided by the navbar, so present whatever the route.
GLOBAL_IDS = ["live-pill", "live-pill-text", "auto-trading-pill"]


@pytest.fixture
def populated_repo():
    """One signal, one execution, one event, one one-asset batch."""
    repo = _repo()
    row = repo.save_signal(_signal(
        ai_status="AI_ANALYZED", ai_decision="agree", ai_score=72.0,
        ai_confidence=0.8, ai_reasoning="clean sweep of PDL",
        ai_strengths=["PDL"], ai_risks=["news risk"]))
    repo.save_trade(ExecutionResult(
        asset="USTEC", symbol="USTEC", direction="buy",
        signal_fingerprint=row.fingerprint, entry=101.7, sl=99.5, tp=106.0,
        lots=0.0, status="SKIPPED", reason="auto_trading_disabled",
        requested_at_utc=datetime(2026, 1, 6, 13, 10)), signal_id=row.id)
    repo.log_event("INFO", "scanner", "warmed 1 asset")
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    repo.save_backtest(
        name="daily", asset="USTEC", symbol="USTEC", batch_id="batch-1",
        start_utc=datetime(2025, 1, 1), end_utc=datetime(2025, 6, 30),
        params={"min_rr": 1.5},
        summary={"n_signals": 40, "n_trades": 12, "n_open": 0, "n_wins": 7,
                 "n_losses": 5, "win_rate": 7 / 12, "profit_factor": 1.8,
                 "total_r": 6.4, "max_drawdown_r": -1.5,
                 "equity_curve": [["2025-01-02T14:00:00", 1.0]],
                 "by_session": {"ny_am": {"n_trades": 8, "total_r": 5.0,
                                          "expectancy": 0.63}}},
        trades=[dict(asset="USTEC", direction="buy", entry=101.7, sl=99.5,
                     tp=106.0, exit_price=106.0,
                     entry_time_utc=datetime(2025, 1, 2, 14, 0),
                     exit_time_utc=datetime(2025, 1, 2, 15, 30),
                     outcome="WIN", pnl=2.0, rr=2.0, bars_held=90, reason="tp")])
    return repo


@pytest.fixture
def dashboard(populated_repo):
    """Every page rendered once, keyed by a stable name, plus the test client."""
    app = create_app(repository=populated_repo, setup_db=False, jobs=_FakeJobs())
    client = app.test_client()
    signal_id = populated_repo.recent_signals(1)[0].id
    bt_id = populated_repo.recent_backtests()[0].id

    paths = {
        "dashboard": "/",
        "signals": "/signals",
        "signal_detail": f"/signals/{signal_id}",
        "backtests": "/backtests",
        "backtest_detail": f"/backtests/{bt_id}",
        "batch": "/backtests/batch/batch-1",
    }
    html = {}
    for name, path in paths.items():
        resp = client.get(path)
        assert resp.status_code == 200, f"{name} ({path}) -> {resp.status_code}"
        html[name] = resp.get_data(as_text=True)
    return {"html": html, "client": client, "paths": paths}


# --------------------------------------------------------------------------- #
# The poller's DOM contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("page", ["dashboard", "signals", "backtests"])
def test_the_topbar_status_ids_exist_on_every_page(dashboard, page):
    """The live pill and the auto-trading warning are global, not per-route."""
    html = dashboard["html"][page]
    for element in GLOBAL_IDS:
        assert f'id="{element}"' in html, f"{element} missing from {page}"


@pytest.mark.parametrize("page,ids", sorted(POLLER_IDS.items()))
def test_the_poller_finds_every_element_it_looks_up(dashboard, page, ids):
    html = dashboard["html"][page]
    for element in ids:
        assert f'id="{element}"' in html, f"{element} missing from {page}"


@pytest.mark.parametrize("page", ["dashboard", "signals", "backtests"])
def test_the_auto_trading_state_is_visible_on_every_page(dashboard, page):
    """A safety indicator shown only on the dashboard is a liability."""
    assert "AUTO-TRADING" in dashboard["html"][page]


def test_the_chart_hosts_and_their_data_scripts_are_paired(dashboard):
    """Each canvas must ship alongside the JSON its inline script reads."""
    detail = dashboard["html"]["backtest_detail"]
    assert 'id="equityChart"' in detail and 'id="equity-data"' in detail

    batch = dashboard["html"]["batch"]
    assert 'id="batchCurves"' in batch and 'id="batch-curves"' in batch


def test_each_chart_dataset_is_valid_json(dashboard):
    """The batch payload is assembled by a loop in the template, not a dump."""
    import json

    html = dashboard["html"]["batch"]
    payload = re.search(r'<script id="batch-curves"[^>]*>(.*?)</script>',
                        html, re.S).group(1)
    parsed = json.loads(payload)
    assert isinstance(parsed, list) and parsed and "asset" in parsed[0]

    equity = re.search(r'<script id="equity-data"[^>]*>(.*?)</script>',
                       dashboard["html"]["backtest_detail"], re.S).group(1)
    assert isinstance(json.loads(equity), list)


# --------------------------------------------------------------------------- #
# Presentation vocabulary
# --------------------------------------------------------------------------- #
def test_no_page_still_wears_the_retired_classes(dashboard):
    """Guards against a half-finished restyle: the old vocabulary is gone."""
    retired = {
        "Bootstrap badge utilities": r"text-bg-(?:success|danger|warning|info|secondary|light|primary)",
        "the old compact-table class": r"table-compact",
    }
    for page, html in dashboard["html"].items():
        for label, pattern in retired.items():
            assert not re.search(pattern, html), f"{label} still in {page}"


def test_no_page_leaves_unrendered_jinja(dashboard):
    for page, html in dashboard["html"].items():
        assert "{{" not in html and "{%" not in html, f"unrendered Jinja in {page}"


def test_templates_carry_no_one_off_styling(dashboard):
    """Styling lives in app.css, so no page can drift out of the design system.

    The single exception is the progress bar, whose width is *data* the poller
    rewrites rather than presentation.
    """
    for page, html in dashboard["html"].items():
        for attr in re.findall(r'style="[^"]*"', html):
            assert attr == 'style="width: 0%"', f"inline style in {page}: {attr}"


def test_the_design_system_components_render(dashboard):
    """The pieces the redesign introduced must actually reach the browser."""
    html = dashboard["html"]
    expected = {
        "topbar-link": "dashboard",
        "page-head": "dashboard",
        "stat-grid": "dashboard",
        "meta-grid": "dashboard",
        "tone-": "signals",
        "filter-bar": "signals",
        "kv-table": "signal_detail",
        "ai-box": "signal_detail",
        "headline": "batch",
        "row-highlight": "batch",
        "card-note": "batch",
        "chart-box": "backtest_detail",
    }
    for token, page in expected.items():
        assert token in html[page], f"{token} missing from {page}"


def test_prices_render_at_the_registry_precision(dashboard):
    """USTEC is a 2-digit instrument; the restyle must not have widened it."""
    signals = dashboard["html"]["signals"]
    assert "101.70" in signals
    assert "101.7000" not in signals


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("page", ["backtest_detail", "batch"])
def test_charts_inherit_a_shared_palette(dashboard, page):
    """Each chart script used to hard-code its own border colour."""
    assert "window.ictChartTheme" in dashboard["html"][page]


# --------------------------------------------------------------------------- #
# API fields the poller depends on
# --------------------------------------------------------------------------- #
def test_status_reports_the_signal_total_the_tile_displays(dashboard):
    payload = dashboard["client"].get("/api/status").get_json()
    # A polling cursor and a count are different numbers; the tile needs the count.
    assert payload["signals"]["total"] == 1
    assert payload["signals"]["last_id"] == 1


def test_status_exposes_the_asset_digits_the_poller_formats_with(dashboard):
    assets = dashboard["client"].get("/api/status").get_json()["assets"]
    assert assets, "no assets in the status payload"
    assert all("digits" in a for a in assets)


# --------------------------------------------------------------------------- #
# The stylesheet
# --------------------------------------------------------------------------- #
#: The design system lives in one file; nothing else defines a token.
STYLESHEET = Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "app.css"


@pytest.fixture(scope="module")
def css():
    return STYLESHEET.read_text(encoding="utf-8")


def test_the_stylesheet_is_brace_balanced(css):
    assert css.count("{") == css.count("}"), "unbalanced braces in app.css"


def test_every_custom_property_used_is_defined(css):
    """A mistyped token fails silently in CSS — the rule just renders as nothing.

    Bootstrap's own ``--bs-*`` variables are defined by the CDN stylesheet, so
    they are exempt; everything this file uses must be declared in this file.
    """
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", css, re.M))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    undefined = sorted(t for t in used - defined if not t.startswith("--bs-"))
    assert not undefined, f"undefined custom properties: {undefined}"


def test_the_stylesheet_is_linked_after_bootstrap(dashboard):
    """app.css re-points Bootstrap's variables, so it must load after it."""
    html = dashboard["html"]["dashboard"]
    assert html.index("bootstrap.min.css") < html.index("css/app.css")

