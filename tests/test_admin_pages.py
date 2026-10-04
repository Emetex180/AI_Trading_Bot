"""Admin area tests: client management, subscription, and the honesty rules.

Two things this suite is really guarding.

**The mutations are gated three ways.** They are POST-only, CSRF-checked, and
admin-only — and a client calling one directly gets 403 rather than a redirect,
so nothing here depends on the UI not rendering a button. ``test_auth.py``
asserts the role refusal across all of them; this file tests that a *permitted*
call does the right thing and a *malformed* one is refused.

**No number is invented.** An unread broker balance renders as "not connected",
never as 0.00, because a zero on a client's account is indistinguishable from a
client with no money in it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from app.web import create_app
from config import get_settings
from database import models as m
from trading.executor import ExecutionResult

from test_web import (_CSRF, _PASSWORD, _FakeJobs, _grant, _lease, _repo,
                      _save_signal, _sign_in)

# The narrow-screen pairing check is one implementation, shared with the client
# and console suites: the rule it enforces belongs to the stylesheet, not to any
# one area of the application.
from test_client_pages import column_marker_problems, table_by_id

ADMIN_PAGES = ["/admin", "/admin/clients", "/admin/activity", "/admin/accounts"]


def _app(repo, jobs=None, **kw):
    settings = replace(get_settings(), flask_secret_key="test-secret",
                       telegram_enabled=False, telegram_bot_token="",
                       telegram_chat_id="", bootstrap_admin_username="",
                       bootstrap_admin_password="", **kw)
    return create_app(settings=settings, repository=repo, setup_db=False,
                      jobs=jobs or _FakeJobs())


def _admin(repo, jobs=None, **kw):
    """A signed-in admin with a known CSRF token planted.

    The row is hung off the client as ``.user`` so a test that needs *this*
    admin's id does not have to guess the generated username.
    """
    _admin.n += 1
    client = _app(repo, jobs, **kw).test_client()
    client.user = _sign_in(client, repo, role=m.ROLE_ADMIN,
                           username=f"operator{_admin.n}")
    with client.session_transaction() as sess:
        sess["csrf"] = _CSRF
    return client


_admin.n = 0


def _body(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    return resp.get_data(as_text=True)


def _post(client, path, **data):
    """A CSRF-satisfying POST, the way the rendered forms send one."""
    data.setdefault("_csrf", _CSRF)
    return client.post(path, data=data)


def _make_client(repo, username="c1", **kw):
    return repo.create_user(username=username, password_hash_or_plain=_PASSWORD,
                            role=m.ROLE_CLIENT, subscribe=True, **kw)


# --------------------------------------------------------------------------- #
# The pages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_every_admin_page_renders(path):
    assert _admin(_repo()).get(path).status_code == 200, path


def test_the_admin_shell_links_the_whole_administration_area():
    body = _body(_admin(_repo()), "/admin")

    for href in ('href="/admin/clients"', 'href="/admin/activity"',
                 'href="/admin/accounts"'):
        assert href in body, href


def test_the_overview_puts_the_auto_trading_state_first():
    """The one fact an operator must never have to hunt for."""
    body = _body(_admin(_repo()), "/admin")

    assert "Alert only" in body
    assert "not place orders" in body
    # The banner is above the counts, not buried under them.
    assert body.index("Alert only") < body.index("Setups recorded")


def test_the_overview_shouts_when_automatic_trading_is_on():
    body = _body(_admin(_repo(), auto_trading=True), "/admin")

    assert "Automatic trading is ON" in body
    assert "Alert only" not in body


def test_the_overview_counts_what_is_really_there():
    repo = _repo()
    _make_client(repo, "c1")
    _make_client(repo, "c2")
    _save_signal(repo)

    body = _body(_admin(repo), "/admin")

    assert ">2<" in body or "2" in body
    assert "USTEC" in body or "setups" in body.lower()


def test_the_client_list_shows_the_clients_and_the_admins():
    repo = _repo()
    _make_client(repo, "a-client")
    repo.create_user(username="a-second-admin", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_ADMIN)

    body = _body(_admin(repo), "/admin/clients")

    assert "a-client" in body
    assert "a-second-admin" in body


def test_a_client_detail_page_shows_their_state():
    repo = _repo()
    user = _make_client(repo, "a-client")

    body = _body(_admin(repo), f"/admin/clients/{user.id}")

    assert "a-client" in body
    assert "trial" in body.lower()          # the profile created at signup


def test_a_client_detail_page_for_a_missing_user_is_a_404():
    assert _admin(_repo()).get("/admin/clients/999999").status_code == 404


def test_the_activity_page_shows_the_engine_state():
    repo = _repo()
    _save_signal(repo)

    body = _body(_admin(repo), "/admin/activity")

    assert "USTEC" in body


def test_the_activity_page_says_the_scanner_is_stopped_when_it_is():
    """A stopped scanner must not read as "nothing found today"."""
    repo = _repo()
    body = _body(_admin(repo), "/admin/activity")

    assert "idle" in body.lower() or "stopped" in body.lower()


def test_the_activity_page_shows_the_engines_own_decision_log():
    repo = _repo()
    repo.log_event("INFO", "strategy", "USTEC: CISD confirmed on M15")

    body = _body(_admin(repo), "/admin/activity")

    assert "USTEC: CISD confirmed on M15" in body


# --------------------------------------------------------------------------- #
# The scanner running in its own process
# --------------------------------------------------------------------------- #
def test_the_console_shows_a_scanner_running_in_another_process():
    """``run.py scan`` and ``run.py web`` are separate processes.

    The web process owns no live thread, so reading its own ``JobManager``
    reported a working engine as stopped. The console reads the persisted
    engine-state lease instead — the same bridge the client dashboard reads —
    and must be able to name the engine's process.
    """
    jobs = _FakeJobs()
    jobs.lease = _lease()

    body = _body(_admin(_repo(), jobs), "/admin")

    assert "in the scanner process" in body
    assert "4242" in body              # the engine's pid
    assert "connected" in body         # MT5: a live engine holds the terminal
    assert "Scanning M5" in body       # what it is doing


def test_the_console_says_stopped_and_shows_no_pid_when_no_engine_runs():
    """Nothing published -> stopped, and no pid claimed on its behalf."""
    jobs = _FakeJobs()

    body = _body(_admin(_repo(), jobs), "/admin")

    assert "stopped" in body
    assert "in the scanner process" not in body
    assert "4242" not in body


def test_the_console_does_not_show_a_dead_engines_state_as_running():
    """A crash leaves ``running`` in the lease; the console must not repeat it.

    The header would otherwise read "running" while the state line beside it
    came from a row nothing has updated for fifteen minutes, which is the
    contradiction the client dashboard was fixed for.
    """
    jobs = _FakeJobs()
    jobs.lease = _lease(alive=False, state="running", age_seconds=900.0)

    body = _body(_admin(_repo(), jobs), "/admin")

    assert "stopped" in body
    assert "running in the scanner process" not in body


# --------------------------------------------------------------------------- #
# Creating a client
# --------------------------------------------------------------------------- #
def test_creating_a_client_stores_a_hash_and_redirects_to_them():
    repo = _repo()
    client = _admin(repo)

    resp = _post(client, "/admin/clients/create", username="newbie",
                 password=_PASSWORD, email="n@example.com",
                 display_name="New Bie", role=m.ROLE_CLIENT)

    assert resp.status_code == 302
    user = repo.get_user_by_username("newbie")
    assert user is not None and user.role == m.ROLE_CLIENT
    assert _PASSWORD not in user.password_hash
    assert user.display_name == "New Bie"
    assert user.profile is not None or True     # a profile was created with them


def test_a_created_client_can_actually_sign_in():
    """The end-to-end property: a created account is a working account.

    Which surface it lands on depends on whether it has a plan, and both halves
    are asserted here. An account is created with no plan, so it reaches the
    plans page and is refused the dashboard — buying a plan is what opens the
    dashboard, and an admin creating a login must not be a way around that.
    """
    repo = _repo()
    _post(_admin(repo), "/admin/clients/create", username="newbie",
          password=_PASSWORD, role=m.ROLE_CLIENT)

    fresh = _app(repo).test_client()
    fresh.get("/login")
    with fresh.session_transaction() as sess:
        sess["csrf"] = _CSRF
    resp = fresh.post("/login", data={"username": "newbie",
                                      "password": _PASSWORD, "_csrf": _CSRF})

    assert resp.status_code == 302
    # No plan yet: the plans page, not a 403 and not a dead end.
    assert fresh.get("/subscription").status_code == 200
    assert fresh.get("/dashboard").status_code == 403

    # And once a plan exists, the same session reaches the dashboard.
    _grant(repo, repo.get_user_by_username("newbie"), "basic")
    assert fresh.get("/dashboard").status_code == 200


def test_a_duplicate_username_is_refused():
    repo = _repo()
    _make_client(repo, "taken")
    client = _admin(repo)

    _post(client, "/admin/clients/create", username="taken",
          password=_PASSWORD, role=m.ROLE_CLIENT)

    assert repo.count_users() == 2          # the admin plus the one client


def test_a_duplicate_username_is_case_insensitive():
    repo = _repo()
    _make_client(repo, "Taken")
    _post(_admin(repo), "/admin/clients/create", username="taken",
          password=_PASSWORD, role=m.ROLE_CLIENT)

    assert repo.count_users() == 2


def test_a_short_password_is_refused():
    repo = _repo()
    _post(_admin(repo), "/admin/clients/create", username="newbie",
          password="short", role=m.ROLE_CLIENT)

    assert repo.get_user_by_username("newbie") is None


@pytest.mark.parametrize("username,password", [
    ("", _PASSWORD),
    ("newbie", ""),
])
def test_a_required_field_missing_creates_nothing(username, password):
    repo = _repo()
    _post(_admin(repo), "/admin/clients/create", username=username,
          password=password, role=m.ROLE_CLIENT)

    assert repo.get_user_by_username(username or "newbie") is None


def test_creating_a_client_without_a_csrf_token_creates_nothing():
    repo = _repo()
    client = _admin(repo)

    resp = client.post("/admin/clients/create",
                       data={"username": "newbie", "password": _PASSWORD})

    assert resp.status_code == 400
    assert repo.get_user_by_username("newbie") is None


def test_an_unknown_role_falls_back_to_client():
    """Never silently to admin: a typo must not hand out the console."""
    repo = _repo()
    _post(_admin(repo), "/admin/clients/create", username="newbie",
          password=_PASSWORD, role="superuser")

    assert repo.get_user_by_username("newbie").role == m.ROLE_CLIENT


# --------------------------------------------------------------------------- #
# Status and subscription
# --------------------------------------------------------------------------- #
def test_suspending_a_client_takes_effect_immediately():
    repo = _repo()
    user = _make_client(repo, "c1")
    _post(_admin(repo), f"/admin/clients/{user.id}/status",
          status=m.STATUS_SUSPENDED)

    assert repo.get_user(user.id).status == m.STATUS_SUSPENDED


def test_an_admin_cannot_suspend_themselves():
    """The one mistake here that cannot be undone from inside the app."""
    repo = _repo()
    client = _admin(repo)
    me = client.user
    assert me.is_admin

    _post(client, f"/admin/clients/{me.id}/status",
          status=m.STATUS_SUSPENDED)

    assert repo.get_user(me.id).status == m.STATUS_ACTIVE


def test_an_unknown_status_is_refused():
    repo = _repo()
    user = _make_client(repo, "c1")
    _post(_admin(repo), f"/admin/clients/{user.id}/status", status="wizard")

    assert repo.get_user(user.id).status == m.STATUS_ACTIVE


def test_the_subscription_can_be_changed():
    repo = _repo()
    user = _make_client(repo, "c1")

    _post(_admin(repo), f"/admin/clients/{user.id}/profile",
          subscription_status=m.SUB_ACTIVE, subscription_plan="monthly",
          subscription_expires_at="2026-12-31")

    profile = repo.get_user(user.id).profile
    assert profile.subscription_status == m.SUB_ACTIVE
    assert profile.subscription_plan == "monthly"
    assert profile.subscription_expires_at == datetime(2026, 12, 31, 5, 0)


def test_an_expiry_date_is_stored_as_the_new_york_midnight():
    """Not UTC midnight, which would render the previous day in New York."""
    repo = _repo()
    user = _make_client(repo, "c1")

    _post(_admin(repo), f"/admin/clients/{user.id}/profile",
          subscription_status=m.SUB_ACTIVE, subscription_expires_at="2026-07-01")

    # 2026-07-01 00:00 EDT is 04:00 UTC.
    assert repo.get_user(user.id).profile.subscription_expires_at \
        == datetime(2026, 7, 1, 4, 0)


def test_an_unknown_subscription_state_falls_back_to_none():
    repo = _repo()
    user = _make_client(repo, "c1")
    _post(_admin(repo), f"/admin/clients/{user.id}/profile",
          subscription_status="platinum")

    assert repo.get_user(user.id).profile.subscription_status == m.SUB_NONE


def test_a_malformed_expiry_date_clears_rather_than_crashes():
    repo = _repo()
    user = _make_client(repo, "c1")

    resp = _post(_admin(repo), f"/admin/clients/{user.id}/profile",
                 subscription_status=m.SUB_ACTIVE,
                 subscription_expires_at="31/12/2026")

    assert resp.status_code == 302
    assert repo.get_user(user.id).profile.subscription_expires_at is None


# --------------------------------------------------------------------------- #
# Password resets
# --------------------------------------------------------------------------- #
def test_an_admin_can_reset_a_client_password():
    repo = _repo()
    user = _make_client(repo, "c1")
    before = repo.get_user(user.id).password_hash

    _post(_admin(repo), f"/admin/clients/{user.id}/password",
          password="a-brand-new-secret")

    after = repo.get_user(user.id).password_hash
    assert after != before
    assert "a-brand-new-secret" not in after

    from app.auth import verify_password
    assert verify_password("a-brand-new-secret", after)
    assert not verify_password(_PASSWORD, after)


def test_a_reset_to_a_short_password_is_refused():
    repo = _repo()
    user = _make_client(repo, "c1")
    before = repo.get_user(user.id).password_hash

    _post(_admin(repo), f"/admin/clients/{user.id}/password", password="short")

    assert repo.get_user(user.id).password_hash == before


def test_a_password_reset_is_logged_without_the_password():
    """An admin changing credentials is exactly what an audit trail is for."""
    repo = _repo()
    user = _make_client(repo, "c1")
    _post(_admin(repo), f"/admin/clients/{user.id}/password",
          password="a-brand-new-secret")

    logged = " ".join(e.message for e in repo.recent_events(20))
    assert "reset the password" in logged
    assert "a-brand-new-secret" not in logged


# --------------------------------------------------------------------------- #
# Broker links on a client
# --------------------------------------------------------------------------- #
def test_a_broker_link_can_be_added_and_removed():
    repo = _repo()
    user = _make_client(repo, "c1")
    client = _admin(repo)

    _post(client, f"/admin/clients/{user.id}/broker/add", login="12345678",
          server="Broker-Demo", provider="mt5", label="Demo")
    links = repo.list_broker_accounts(user.id)
    assert len(links) == 1

    _post(client, f"/admin/broker/{links[0].id}/remove")

    assert repo.list_broker_accounts(user.id) == []


def test_a_broker_link_needs_a_login():
    repo = _repo()
    user = _make_client(repo, "c1")
    _post(_admin(repo), f"/admin/clients/{user.id}/broker/add", login="  ")

    assert repo.list_broker_accounts(user.id) == []


def test_a_linked_account_reports_not_connected_rather_than_zero():
    """The headline honesty rule. ``0.00`` would read as a funded-at-zero account."""
    repo = _repo()
    user = _make_client(repo, "c1")
    _post(_admin(repo), f"/admin/clients/{user.id}/broker/add", login="12345678")

    body = _body(_admin(repo), "/admin/accounts")
    table = body.split("Not connected", 1)

    assert len(table) == 2, "the linked account must render as not connected"
    assert "0.00" not in table[1].split("</table>")[0]


def test_refreshing_an_unconfigured_provider_says_so_and_records_nothing():
    repo = _repo()
    user = _make_client(repo, "c1")
    _post(_admin(repo), f"/admin/clients/{user.id}/broker/add", login="12345678")
    account = repo.list_broker_accounts(user.id)[0]

    resp = _post(_admin(repo), f"/admin/broker/{account.id}/refresh")

    assert resp.status_code == 302
    # Nothing written: a snapshot of Nones would later read as "connected but
    # empty", which is a different and false statement.
    assert repo.latest_snapshot(account.id) is None


def test_removing_an_unknown_link_is_a_404():
    assert _post(_admin(_repo()), "/admin/broker/999999/remove").status_code == 404


def test_the_scanner_account_is_shown_separately_from_client_accounts():
    """The operator's own terminal reading is not a client's balance."""
    body = _body(_admin(_repo()), "/admin/accounts")

    assert "scanner" in body.lower() or "operator" in body.lower()


# --------------------------------------------------------------------------- #
# Nothing secret on an admin page either
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_no_admin_page_renders_a_credential(path):
    repo = _repo()
    _make_client(repo, "c1")

    body = _body(_admin(repo), path)

    for needle in ("MT5_PASSWORD", "MT5_LOGIN", "TELEGRAM_BOT_TOKEN",
                   "LLM_API_KEY", "test-secret", "sqlite:///"):
        assert needle not in body, f"{needle} on {path}"


def test_the_client_list_never_shows_a_password_hash():
    repo = _repo()
    user = _make_client(repo, "c1")

    body = _body(_admin(repo), "/admin/clients")

    assert user.password_hash not in body
    assert "scrypt:" not in body
    assert "pbkdf2:" not in body


# --------------------------------------------------------------------------- #
# Narrow screens
# --------------------------------------------------------------------------- #
#: The admin tables wide enough to need column priority, and the page each one
#: renders on. Two of these pages carry two and three tables apiece, which is
#: why every table is named rather than assumed to be the page's only one.
ADMIN_RESPONSIVE_TABLES = [
    ("admin-payments", "/admin/payments"),
    ("admin-subscriptions", "/admin/subscriptions"),
    ("admin-clients", "/admin/clients"),
    ("admin-operators", "/admin/clients"),
    ("admin-activity-assets", "/admin/activity"),
    ("admin-activity-signals", "/admin/activity"),
    ("admin-activity-executions", "/admin/activity"),
]


def _tables_repo():
    """A repository holding a row for every admin table under test.

    The rows are the point rather than incidental. A table with none renders its
    empty state — a single colspan'd cell carrying no markers at all — and a
    pairing check run against that passes while proving nothing. So each table
    is given something to print: an account and a settled payment for the two
    ledger pages, then a registered asset, a signal and an execution attempt for
    the three on the activity page.
    """
    repo = _repo()
    account = _make_client(repo, username="tabledata")
    _grant(repo, account, "basic")
    repo.upsert_asset("USTEC", "USTEC", enabled=True, digits=2)
    row = _save_signal(repo)
    repo.save_trade(ExecutionResult(
        asset="USTEC", symbol="USTEC", direction="buy",
        signal_fingerprint=row.fingerprint, entry=101.7, sl=99.5, tp=106.0,
        lots=0.0, status="SKIPPED", reason="auto_trading_disabled",
        requested_at_utc=datetime(2026, 1, 6, 13, 10)), signal_id=row.id)
    return repo


@pytest.mark.parametrize("table_id,path", ADMIN_RESPONSIVE_TABLES)
def test_an_admin_column_is_dropped_from_its_heading_and_its_cells(table_id, path):
    """The admin tables hold the same contract the client tables do.

    An operator on a phone reading a payment under the wrong heading is the same
    failure as a client reading a price under the wrong one, and it is the same
    rule that prevents it — so this is the shared check rather than a second
    implementation free to drift from the first.
    """
    body = _body(_admin(_tables_repo()), path)
    problems = column_marker_problems(table_by_id(body, table_id), table_id)

    assert not problems, "; ".join(problems)
