"""Client platform page tests.

The product surface: what a paying client sees. Two properties run through all
of it.

**Nothing is invented.** Every figure on these pages is either a persisted
``signals`` row the engine stood behind, or a value the running engine published
through ``JobManager.live_state()``. Where the engine has produced nothing, the
page says so — an em dash or an explicit "nothing yet", never a zero that would
read as a real reading.

**Nothing operator-facing leaks across.** No credential, no console link for a
client, no other client's username.

The engine is stubbed (``_FakeJobs``) so no test can reach MT5.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from app.display import ny_str
from app.web import create_app
from config import get_settings
from database import models as m

from test_web import (_PASSWORD, _FakeJobs, _lease, _repo, _save_signal,
                      _sign_in)

CLIENT_PAGES = ["/dashboard", "/market", "/setups", "/history", "/analysis"]

#: The JSON the poller reads, and the page that polls it.
POLL_ENDPOINTS = ["/api/client/overview", "/api/client/market",
                  "/api/client/setups", "/api/client/feed", "/api/client/overview"]


def _app(repo, jobs=None, **kw):
    settings = replace(get_settings(), flask_secret_key="test-secret",
                       telegram_enabled=False, telegram_bot_token="",
                       telegram_chat_id="", bootstrap_admin_username="",
                       bootstrap_admin_password="", **kw)
    return create_app(settings=settings, repository=repo, setup_db=False,
                      jobs=jobs or _FakeJobs())


def _client(repo, jobs=None, role=m.ROLE_CLIENT, **kw):
    """A signed-in client. Each call gets its own account.

    The username is generated rather than fixed because a test may sign two
    clients into the same repository, and “the user already exists” is
    not what such a test is about.
    """
    _client.n += 1
    app = _app(repo, jobs, **kw)
    client = app.test_client()
    _sign_in(client, repo, role=role, username=f"viewer{_client.n}")
    return client


_client.n = 0


def _seeded(jobs=None, **signal_kw):
    """A repository holding one approved, fully-populated setup."""
    repo = _repo()
    _save_signal(repo, **signal_kw)
    return repo


def _body(client, path="/dashboard"):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    return resp.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Every page renders for the role it belongs to
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", CLIENT_PAGES)
def test_every_client_page_renders(path):
    assert _client(_repo()).get(path).status_code == 200, path


@pytest.mark.parametrize("path", CLIENT_PAGES)
def test_an_admin_can_see_what_a_client_sees(path):
    """An admin needs the client view; they are not locked out of it."""
    assert _client(_repo(), role=m.ROLE_ADMIN).get(path).status_code == 200


@pytest.mark.parametrize("path", POLL_ENDPOINTS)
def test_every_poll_endpoint_answers_json(path):
    payload = _client(_repo()).get(path).get_json()

    assert payload is not None, path
    assert "status" in payload
    assert payload["status"]["ny_zone"], "the poller needs a zone to label with"


@pytest.mark.parametrize("path,kind", [
    ("/dashboard", "overview"),
    ("/market", "market"),
    ("/setups", "setups"),
    ("/analysis", "analysis"),
])
def test_each_page_declares_the_poller_it_needs(path, kind):
    """``window.clientPoll`` is how the page tells client.js what to fetch.

    A page that declares nothing silently polls the status endpoint only, which
    looks like a live page that never updates.
    """
    body = _body(_client(_repo()), path)

    assert f'kind: "{kind}"' in body
    assert "js/client.js" in body


def test_every_client_page_carries_the_new_york_clock():
    body = _body(_client(_repo()))

    assert 'id="ny-clock"' in body
    assert 'id="ny-zone"' in body
    assert "America/New_York" in body


def test_the_client_shell_offers_no_operator_links():
    """A client gets the product, not the control panel — not even a dead link."""
    body = _body(_client(_repo()))

    assert 'href="/console' not in body
    assert 'href="/admin' not in body
    assert "Operator" not in body


def test_an_admin_sees_the_operator_section():
    body = _body(_client(_repo(), role=m.ROLE_ADMIN))

    # Prefix-matched: ``url_for("admin.index")`` renders the trailing-slash
    # form, and the intent here is the destination rather than the spelling.
    assert 'href="/console"' in body
    assert 'href="/admin' in body


def test_the_shell_reports_the_auto_trading_state():
    """The safety affordance: whether the bot may place orders, on every page.

    The pill's *text* is written by client.js from the status poll, so the
    server-rendered half of the contract is the element and its starting tone.
    """
    body = _body(_client(_repo()))

    assert 'id="nav-auto"' in body
    assert 'id="nav-scanner"' in body
    assert "tone-danger" not in body          # AUTO_TRADING is off


def test_the_auto_trading_pill_turns_dangerous_when_it_is_on():
    body = _body(_client(_repo(), auto_trading=True))

    assert "tone-danger" in body


# --------------------------------------------------------------------------- #
# A setup the engine produced is rendered from its own columns
# --------------------------------------------------------------------------- #
def test_the_dashboard_shows_a_persisted_setup():
    """The card carries the levels and the clock, straight from the row."""
    repo = _seeded()
    body = _body(_client(repo))

    assert 'data-asset="USTEC"' in body
    assert "101.70" in body                 # entry
    assert "99.50" in body                  # stop, from the M5 anchor
    assert "106.00" in body                 # target, at the liquidity pull
    assert "NY" in body                     # the clock the entry time is shown on


def test_the_setups_page_shows_the_condition_checklist():
    """The card lists each step of the model and how many cleared.

    The *evidence* for each step (which level, which gap bounds) is on the
    detail page: the card's list is deliberately compact, since a grid of
    twelve setups each carrying seven paragraphs is unreadable.
    """
    repo = _seeded(state="TRADE_CONFIRMED", structure_extreme_price=99.5,
                   target_kind="PDH", target_price=106.0)
    body = _body(_client(repo), "/setups")

    assert "1H liquidity purge" in body
    assert "Conditions cleared" in body
    assert "7/7" in body                    # every column is populated
    assert 'class="condition met"' in body
    assert 'class="condition unmet"' not in body


def test_the_setups_page_lists_the_approved_setup():
    repo = _seeded()
    body = _body(_client(repo), "/setups")

    assert "USTEC" in body and 'id="setups-grid"' in body


def test_the_setup_detail_page_walks_the_model():
    """One page per setup, showing each step of the strategy and its evidence."""
    repo = _seeded()
    row = repo.recent_signals(1)[0]
    body = _body(_client(repo), f"/setups/{row.id}")

    for step in ("1H liquidity purge", "CISD", "1M FVG", "FVG retracement",
                 "SL from the M5 liquidity-taking candle",
                 "TP at the nearest liquidity pull", "Risk/reward requirement"):
        assert step in body, step


def test_the_setup_detail_page_shows_the_recorded_values_not_placeholders():
    repo = _seeded()
    row = repo.recent_signals(1)[0]
    body = _body(_client(repo), f"/setups/{row.id}")

    assert "101.70" in body          # entry, at the asset's precision
    assert "99.50" in body           # stop, from the M5 anchor
    assert "106.00" in body          # take profit, at the liquidity pull
    assert "2.00" in body            # rr

    # The detail page is the one that carries each step's evidence.
    assert "PDL" in body                          # the level that was purged
    assert "bullish gap" in body                  # the FVG bounds
    assert "101.40" in body and "101.55" in body


def test_the_checklist_reports_a_step_the_engine_never_reached():
    """A half-formed setup must show its gaps, not a row of ticks."""
    # No structure anchor and no target: the SL and TP steps never happened.
    repo = _seeded(state="CISD_CONFIRMED", structure_extreme_price=0.0,
                   target_kind="", target_price=0.0)
    body = _body(_client(repo), "/setups")

    assert 'class="condition unmet"' in body
    # Purge, CISD, FVG and the RR gate are recorded; the SL anchor, the target
    # and the retracement never happened.
    assert "4/7" in body


def test_a_setup_with_no_analysis_shows_no_analysis():
    """AI commentary is never fabricated to fill the panel."""
    repo = _seeded(ai_status="", ai_decision="", ai_reasoning="")
    row = repo.recent_signals(1)[0]
    body = _body(_client(repo), f"/setups/{row.id}")

    assert "no analysis" in body.lower() or "AI" not in body


def test_a_setup_that_does_not_exist_is_a_404():
    assert _client(_repo()).get("/setups/999999").status_code == 404


# --------------------------------------------------------------------------- #
# The market table
# --------------------------------------------------------------------------- #
def test_the_market_table_lists_the_registry():
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    body = _body(_client(repo), "/market")

    assert "USTEC" in body and 'id="market-table"' in body


def test_the_market_table_shows_an_unread_price_as_unknown():
    """With no session running there is no price, and zero would be a lie."""
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    body = _body(_client(repo), "/market")

    assert "0.00" not in body
    assert "—" in body or "not read" in body.lower() or "Watching" in body


def test_the_market_table_publishes_the_engines_live_state():
    """An asset mid-setup is shown, which only live engine state can know."""
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.live = {**jobs.live, "assets": ["USTEC"], "state": "running",
                 "setups": {"USTEC": {"buy": "WAITING_FOR_FVG_RETRACE",
                                      "sell": "NO_SETUP"}},
                 "prices": {"USTEC": 101.75},
                 "price_times": {"USTEC": datetime(2026, 1, 6, 18, 32)}}

    body = _body(_client(repo, jobs), "/market")

    assert "101.75" in body
    assert "FVG retracement" in body or "Waiting for FVG" in body


def test_the_market_page_never_claims_a_price_when_the_stream_is_empty():
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.live = {**jobs.live, "assets": ["USTEC"], "state": "running",
                 "setups": {}, "prices": {}, "price_times": {}}

    body = _body(_client(repo, jobs), "/market")

    assert "101.75" not in body


# --------------------------------------------------------------------------- #
# The engine running in another process
# --------------------------------------------------------------------------- #
def test_the_market_page_reads_the_engine_from_another_process():
    """The reason the bridge exists: the scanner is a separate process.

    Started by ``run.py scan``, its live state is in that process's memory, so
    the persisted lease is the only way this page can know a quote at all.
    """
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.lease = _lease()

    body = _body(_client(repo, jobs), "/market")

    assert "101.70" in body and "101.74" in body   # bid and ask
    assert "101.75" in body                        # the closed M1 close
    assert "ACTIVE" in body
    assert "running in the scanner process" in body
    assert "4242" in body                          # the engine's pid


def test_an_expired_lease_is_offline_even_though_flask_answered():
    """A web server that answers a request is not evidence of a running engine.

    This is the state the page used to be stuck in: Flask up, header reading
    "Scanner stopped", every price an em dash, while the engine scanned and
    alerted perfectly well in its own process. The inverse must hold too — once
    the lease lapses, the last quote is not re-served as though it were live.
    """
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.lease = _lease(alive=False, state="stopped", age_seconds=None,
                        last_error="Live session stopped.")

    body = _body(_client(repo, jobs), "/market")

    assert "OFFLINE" in body
    assert "101.70" not in body
    assert "101.75" not in body


def test_a_dead_engine_does_not_report_itself_as_running():
    """The state and the liveness flag must not contradict each other.

    A crashed engine stops heartbeating without writing anything on its way out,
    so its last published row still says ``running``. Reporting that next to
    ``engine_alive: false`` is the contradiction this removes: a reader looking
    at the state alone (the dashboard header, an API client, the admin console)
    would be told the engine was up when nothing is publishing.
    """
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.lease = _lease(alive=False, state="running", age_seconds=900.0,
                        last_error="")

    status = _client(repo, jobs).get("/api/client/market").get_json()["status"]

    assert status["scanner_running"] is False
    assert status["engine_alive"] is False
    assert status["scanner_state"] == "stopped"
    assert status["scanner_state"] != "running"


def test_a_dead_engine_still_reports_the_error_it_died_with():
    """Rewriting a stale "running" must not erase the reason it stopped.

    ``stopped`` and ``error`` are not claims of liveness, so they are passed
    through exactly as the engine wrote them — "it crashed with this error" is
    the one thing an operator needs from a dead engine.
    """
    jobs = _FakeJobs()
    jobs.lease = _lease(alive=False, state="error", age_seconds=900.0,
                        last_error="MT5 terminal not running")

    status = _client(_repo(), jobs).get("/api/client/market").get_json()["status"]

    assert status["scanner_state"] == "error"
    assert status["engine_last_error"] == "MT5 terminal not running"


def test_the_market_api_formats_the_lease_quotes_for_the_poller():
    """The polled payload carries the same labels the page was painted with."""
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.lease = _lease()

    payload = _client(repo, jobs).get("/api/client/market").get_json()

    assert payload["status"]["engine_alive"] is True
    assert payload["status"]["engine_source"] == "scanner process"
    # By name, not by position: the registry ships a full asset list and the
    # poller keys its rows on data-asset for the same reason.
    row = next(r for r in payload["rows"] if r["name"] == "USTEC")
    assert row["bid_label"] == "101.70"
    assert row["ask_label"] == "101.74"
    assert row["spread_label"] == "0.04"
    assert row["spread_points_label"] == "4"


def test_the_api_says_no_engine_when_only_the_web_server_is_up():
    """``engine_alive`` distinguishes the engine from the process serving it."""
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()

    payload = _client(repo, jobs).get("/api/client/market").get_json()

    assert payload["status"]["engine_alive"] is False
    assert payload["status"]["engine_source"] == "none"
    assert payload["status"]["mt5_connected"] is None   # unknown, not "no"


# --------------------------------------------------------------------------- #
# The named components on the engine card
#
# The card lists the backend, MT5, market data, the scanner, the setup pipeline
# and the asset watch separately, because those are the questions an operator
# actually asks. They are derived from the one engine state rather than read
# independently, so a component can never be reported as up while the engine is
# down — and, the whole point of the exercise, "up but asleep" is never
# flattened into "stopped".
# --------------------------------------------------------------------------- #
def _status_of(client):
    return client.get("/api/client/market").get_json()["status"]


def test_no_engine_reports_every_component_as_stopped():
    """Nothing on the card may claim to be working without an engine behind it.

    ``web_app_running`` is the deliberate exception and is labelled as such: it
    is true by construction, because this page is being served.
    """
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)

    status = _status_of(_client(repo, _FakeJobs()))

    assert status["web_app_running"] is True
    assert status["backend_running"] is False
    assert status["market_data_live"] is False
    assert status["scanner_status"] == "STOPPED"
    assert status["setup_scanner_status"] == "STOPPED"
    assert status["assets_status"] == "STOPPED"
    assert status["assets_count"] == 0


def test_an_asleep_engine_is_waiting_not_stopped():
    """The distinction the whole page exists for.

    Outside a session the live thread is up and correctly polling nothing. It
    has not stopped, it will wake by itself at the next window, and calling that
    "stopped" is what sends an operator to restart a perfectly healthy engine.
    """
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.lease = _lease(active=False, activity="outside_session",
                        age_seconds=2.0)

    status = _status_of(_client(repo, jobs))

    assert status["backend_running"] is True
    assert status["scanner_status"] == "WAITING"
    assert status["setup_scanner_status"] == "WAITING"
    assert status["assets_status"] == "WAITING"
    assert status["scanner_running"] is True     # alive...
    assert status["scanner_active"] is False     # ...and awake? no. Both true.
    # No candle is being read while it sleeps, so the feed is not live — the
    # figures on the page stay the last ones it read, with their time.
    assert status["market_data_live"] is False


def test_an_awake_engine_reports_every_component_as_active():
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.lease = _lease(active=True, activity="london_open")

    status = _status_of(_client(repo, jobs))

    assert status["backend_running"] is True
    assert status["mt5_connected"] is True
    assert status["market_data_live"] is True
    assert status["scanner_status"] == "ACTIVE"
    assert status["setup_scanner_status"] == "ACTIVE"
    assert status["assets_status"] == "ACTIVE"
    assert status["assets_count"] == 1


def test_an_engine_in_this_process_reports_its_own_heartbeat():
    """The startup path puts the engine in the web process, so the card must be
    able to describe that case — including the heartbeat, which used to be
    blanked to ``None`` for an in-process engine and left the tile reading
    "none" beside a scanner that was demonstrably beating."""
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.live = {**jobs.live, "state": "running", "active": True,
                 "activity": "london_open", "assets": ["USTEC"],
                 "last_candle_utc": datetime(2026, 1, 6, 18, 32),
                 "heartbeat_utc": datetime(2026, 1, 6, 18, 33)}

    status = _status_of(_client(repo, jobs))

    assert status["engine_source"] == "this process"
    assert status["engine_alive"] is True
    assert status["engine_heartbeat_ny"] != ""
    assert status["engine_age_seconds"] is not None
    assert status["market_data_live"] is True


def test_the_engine_card_renders_the_named_components():
    """The words on the card come from the payload, so a reader sees the same
    vocabulary the poller will keep writing into those cells."""
    repo = _repo()
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    jobs = _FakeJobs()
    jobs.lease = _lease(active=True, activity="london_open")

    body = _body(_client(repo, jobs), "/market")

    for label in ("Web app:", "Backend:", "Market data:", "Setup scanner:",
                  "Session status:", "Assets:"):
        assert label in body, label
    assert "Setup scanner: <span id=\"mk-setups\">ACTIVE</span>" in body
    assert "Market data: <span id=\"mk-data\">LIVE</span>" in body


def test_the_session_status_is_read_from_the_ny_clock(monkeypatch):
    """Session status is a statement about the clock, not about the engine: a
    window covers right now, or it does not. Read on the same DST-aware NY
    clock the strategy uses, never a UTC offset."""
    from trading import time_utils

    # 2026-07-15 13:30 UTC = 09:30 NY, inside NY AM.
    monkeypatch.setattr(time_utils, "now_ny", lambda: datetime(2026, 7, 15, 9, 30))
    open_now = _status_of(_client(_seeded()))

    assert open_now["session_open"] is True
    assert open_now["session_status"] == "ACTIVE"
    assert open_now["session_label"] == "NY AM"

    # 2026-07-15 17:30 UTC = 13:30 NY: NY PM, the lunch gap is 12:00-13:00.
    monkeypatch.setattr(time_utils, "now_ny", lambda: datetime(2026, 7, 15, 12, 30))
    closed_now = _status_of(_client(_seeded()))

    assert closed_now["session_open"] is False
    assert closed_now["session_status"] == "CLOSED"


# --------------------------------------------------------------------------- #
# History and its filters
# --------------------------------------------------------------------------- #
def test_history_lists_the_setup():
    repo = _seeded()
    body = _body(_client(repo), "/history")

    assert "USTEC" in body and 'id="history-table"' in body


def test_history_filters_render_their_controls():
    repo = _seeded()
    body = _body(_client(repo), "/history")

    for control in ('id="f-asset"', 'id="f-direction"', 'id="f-session"',
                    'id="f-status"', 'id="f-from"', 'id="f-to"'):
        assert control in body, control


def test_a_filter_narrows_the_listing():
    repo = _repo()
    _save_signal(repo, asset="USTEC")
    _save_signal(repo, asset="EURUSD")

    only = _body(_client(repo), "/history?asset=EURUSD")

    assert "EURUSD" in only


def test_a_filter_that_matches_nothing_says_so_rather_than_showing_everything():
    """The dropdown still lists every asset; the *table* must be empty.

    Scoped to the table body, because ``USTEC`` legitimately appears in the
    filter's own ``<option>`` list.
    """
    repo = _seeded()
    body = _body(_client(repo), "/history?asset=NOSUCHASSET")
    table = body.split('id="history-table"', 1)[1]

    assert "No setups match these filters." in table
    assert "USTEC" not in table


def test_a_malformed_date_filter_is_ignored_rather_than_crashing():
    repo = _seeded()

    assert _client(repo).get("/history?from=not-a-date").status_code == 200


def test_a_date_filter_is_read_on_the_new_york_clock():
    """A filter of "5 January" must mean the NY day, not the UTC one.

    The setup is stored at 02:00 UTC on the 6th, which is 21:00 on the 5th in
    New York. A NY-day filter for the 5th therefore includes it and one for the
    6th excludes it; computing the bound in UTC would swap those two answers.
    """
    repo = _seeded()

    def table(html):
        """Just the results table: the asset dropdown lists USTEC regardless."""
        return html.split('id="history-table"', 1)[1]

    # The seeded signal is 13:10 UTC on the 6th: 08:10 NY, the same NY day.
    assert "USTEC" in table(_body(_client(repo), "/history?from=2026-01-06&to=2026-01-06"))

    # 02:00 UTC on the 6th is 21:00 on the 5th in New York.
    late = _repo()
    _save_signal(late, entry_time_utc=datetime(2026, 1, 6, 2, 0),
                 entry_time_ny=datetime(2026, 1, 5, 21, 0))
    assert "USTEC" in table(_body(_client(late), "/history?from=2026-01-05&to=2026-01-05"))
    assert "USTEC" not in table(_body(_client(late), "/history?from=2026-01-06&to=2026-01-06"))


# --------------------------------------------------------------------------- #
# The analysis feed is the engine narrating itself
# --------------------------------------------------------------------------- #
def test_the_feed_renders_engine_events():
    repo = _repo()
    repo.log_event("INFO", "strategy", "USTEC: 1H liquidity purged at PDL 100.00")
    repo.log_event("WARN", "scanner", "warm-up discarded 3 signals")

    body = _body(_client(repo), "/analysis")

    assert "1H liquidity purged at PDL 100.00" in body
    assert "warm-up discarded 3 signals" in body


def test_the_feed_says_so_when_the_engine_has_said_nothing():
    body = _body(_client(_repo()), "/analysis")

    assert "nothing" in body.lower() or "no " in body.lower()


def test_the_feed_carries_a_utc_timestamp_it_does_not_invent():
    """Every line is a stored event with its own time, shown on the NY clock."""
    repo = _repo()
    repo.log_event("INFO", "strategy", "a real decision line")
    row = repo.recent_events(1)[0]

    payload = _client(repo).get("/api/client/feed").get_json()
    line = next(f for f in payload["feed"] if f["message"] == "a real decision line")

    assert line["id"] == row.id
    # The rendered strings are derived from the stored instant, not from "now".
    assert line["time_short"] == ny_str(row.event_time_utc, "%H:%M:%S")
    assert line["date_ny"] == ny_str(row.event_time_utc, "%Y-%m-%d")


def test_the_feed_hides_non_strategy_sources_from_the_strategy_view():
    """Auth and admin lines are not market analysis."""
    repo = _repo()
    repo.log_event("WARN", "auth", "failed login for 'x' from 1.2.3.4")

    assert "failed login" not in _body(_client(repo), "/analysis")


# --------------------------------------------------------------------------- #
# The poller contract
# --------------------------------------------------------------------------- #
def test_the_setups_cursor_only_returns_what_is_newer():
    repo = _seeded()
    first = repo.max_signal_id()
    # A distinct instant, because save_signal keys on the setup fingerprint and
    # an identical row would update the first one rather than add a second.
    _save_signal(repo, entry_time_utc=datetime(2026, 1, 6, 14, 10),
                 entry_time_ny=datetime(2026, 1, 6, 9, 10))
    assert repo.max_signal_id() > first

    payload = _client(repo).get(f"/api/client/setups?after_id={first}").get_json()

    assert payload["last_signal_id"] > first
    assert len(payload["setups"]) == 1


def test_the_setups_cursor_carries_the_fields_the_poller_paints():
    repo = _seeded()
    setup = _client(repo).get("/api/client/setups").get_json()["setups"][0]

    for key in ("id", "asset", "direction", "entry", "sl", "tp", "rr_label",
                "state_info", "conditions", "conditions_met", "conditions_total",
                "url", "entry_time_ny"):
        assert key in setup, key


def test_a_rejected_setup_is_not_offered_as_a_new_setup():
    """"A new setup appeared" is the only thing the cursor means."""
    repo = _repo()
    _save_signal(repo, status="REJECTED", reason="rr below minimum")
    first = repo.max_signal_id()
    _save_signal(repo, status="REJECTED", reason="rr below minimum",
                 entry_time_utc=datetime(2026, 1, 6, 14, 10),
                 entry_time_ny=datetime(2026, 1, 6, 9, 10))

    payload = _client(repo).get(f"/api/client/setups?after_id={first}").get_json()

    assert payload["setups"] == []


def test_the_overview_payload_counts_what_the_page_shows():
    repo = _seeded()
    payload = _client(repo).get("/api/client/overview").get_json()

    assert payload["counts"]["setups_total"] == repo.count_signals()
    assert payload["last_signal_id"] == repo.max_signal_id()
    assert payload["status"]["ny_zone"]


def test_the_scanner_state_is_reported_separately_from_the_session():
    """A session can be running and asleep; the platform must be able to say so."""
    jobs = _FakeJobs()
    jobs.live = {**jobs.live, "state": "running", "active": False,
                 "activity": "outside_session"}
    payload = _client(_repo(), jobs).get("/api/client/overview").get_json()

    assert payload["status"]["scanner_running"] is True
    assert payload["status"]["scanner_active"] is False


def test_the_session_window_matches_the_strategy_definition():
    """The five windows are read from trading/sessions.py, not restated."""
    from trading import sessions as sess

    body = _body(_client(_seeded()), "/analysis")

    for window in sess.CORE_SESSIONS:
        assert window.label in body, window.label


def test_the_platform_reports_the_ny_session_for_a_known_clock(monkeypatch):
    """The session shown is the one the strategy would use at that instant."""
    from trading import sessions as sess
    from trading import time_utils

    # 2026-07-15 13:30 UTC = 09:30 NY, inside NY AM.
    monkeypatch.setattr(time_utils, "now_ny", lambda: datetime(2026, 7, 15, 9, 30))

    status = _client(_seeded()).get("/api/client/overview").get_json()["status"]

    assert status["session_key"] == sess.get_trading_session(
        datetime(2026, 7, 15, 9, 30)).key
    assert status["ny_time"].startswith("09:30")
    assert status["ny_date"] == "Wednesday 15 July 2026"
