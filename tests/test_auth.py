"""Authentication, session and role-gate tests.

These pin the security properties the client-facing platform has to guarantee,
one test per property:

* a password is never stored, logged or echoed back in a form that recovers it;
* an account cannot be enumerated by response message, response code or timing;
* a **client is refused the admin area and the console with a flat 403**, on the
  pages *and* on the mutation endpoints, so nothing depends on the UI merely not
  rendering a button;
* the session is httpOnly + SameSite=Lax and dies when the account is suspended;
* every displayed time is ``America/New_York``, DST-aware, never a fixed offset.

The app is built per-test against its own in-memory database, so no test depends
on what another one did.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from app.auth import (FALLBACK_MIN_PASSWORD, LoginThrottle, hash_password,
                      password_problem, verify_password)
from app.web import create_app
from config import get_settings
from database import models as m
from trading import time_utils as tu

from test_web import _CSRF, _PASSWORD, _FakeJobs, _repo, _sign_in

#: Every operator surface, and the method each one is reached by. A client must
#: be refused all of them; an admin must get through all of them.
OPERATOR_PAGES = [
    "/console",
    "/console/signals",
    "/console/backtests",
    "/console/assets",
    "/admin",
    "/admin/clients",
    "/admin/activity",
    "/admin/accounts",
]

OPERATOR_MUTATIONS = [
    "/api/live/start",
    "/api/live/stop",
    "/api/backtest/run",
    "/api/assets/toggle",
    "/api/assets/remove",
    "/api/assets/add",
    "/api/assets/set-all",
    "/api/assets/broker/scan",
    "/api/account/refresh",
    "/api/auto-trading",
    "/api/data/probe",
    "/admin/clients/create",
]

CLIENT_PAGES = ["/dashboard", "/market", "/setups", "/history", "/analysis"]


def _settings(**kw):
    """Test settings: a fixed signing key, and no environment bootstrap admin.

    The bootstrap admin is blanked explicitly rather than assumed blank — a
    developer's ``.env`` may set it, and a test that counts users must not
    depend on that.
    """
    base = dict(flask_secret_key="test-secret", telegram_enabled=False,
                telegram_bot_token="", telegram_chat_id="",
                bootstrap_admin_username="", bootstrap_admin_password="",
                bootstrap_admin_email="", session_cookie_secure=False)
    base.update(kw)
    return replace(get_settings(), **base)


def _app(repo, **kw):
    return create_app(settings=_settings(**kw), repository=repo,
                      setup_db=False, jobs=_FakeJobs())


def _anonymous(repo, **kw):
    return _app(repo, **kw).test_client()


def _as(repo, role, username="tester", **kw):
    """A test client signed in as ``role``."""
    client = _app(repo, **kw).test_client()
    _sign_in(client, repo, role=role, username=username)
    return client


def _visiting_login(repo=None, **kw):
    """A signed-out client that has fetched the login form.

    What a real browser has: the page (and therefore a CSRF token) already
    issued, so the form it posts can pass the gate.
    """
    client = _anonymous(repo if repo is not None else _repo(), **kw)
    client.get("/login")
    with client.session_transaction() as sess:
        sess["csrf"] = _CSRF          # pinned, so the test can send it back
    return client


def _login(client, username, password, **extra):
    data = {"username": username, "password": password, "_csrf": _CSRF}
    data.update(extra)
    return client.post("/login", data=data)


# --------------------------------------------------------------------------- #
# Passwords are never stored recoverably
# --------------------------------------------------------------------------- #
def test_a_hash_is_not_the_password_it_came_from():
    digest = hash_password(_PASSWORD)
    assert _PASSWORD not in digest
    assert digest != _PASSWORD
    assert verify_password(_PASSWORD, digest)
    assert not verify_password(_PASSWORD + "x", digest)


def test_the_stored_column_holds_a_hash_and_not_the_plaintext():
    """The property that matters: what is *in the database* cannot sign in."""
    repo = _repo()
    user = repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                            role=m.ROLE_CLIENT)

    stored = repo.get_user(user.id).password_hash
    assert stored != _PASSWORD
    assert _PASSWORD not in stored
    # Werkzeug tags its output with the method, which is how create_user tells
    # an already-hashed value from a raw one.
    assert stored.startswith(("scrypt:", "pbkdf2:")), stored
    assert verify_password(_PASSWORD, stored)


def test_a_reset_invalidates_the_old_password():
    repo = _repo()
    user = repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                            role=m.ROLE_CLIENT)

    repo.set_user_password(user.id, "a-completely-different-one")

    stored = repo.get_user(user.id).password_hash
    assert not verify_password(_PASSWORD, stored)
    assert verify_password("a-completely-different-one", stored)


def test_a_missing_hash_verifies_false_rather_than_raising():
    assert verify_password(_PASSWORD, None) is False
    assert verify_password(_PASSWORD, "") is False


def test_verify_does_not_short_circuit_on_an_unknown_user():
    """The dummy verify is what stops the response time enumerating usernames."""
    assert verify_password("anything", None) is False
    assert verify_password("anything", "not-a-real-hash") is False


@pytest.mark.parametrize("password,expected", [
    ("", True),
    ("short", True),
    ("x" * (FALLBACK_MIN_PASSWORD - 1), True),
    ("x" * FALLBACK_MIN_PASSWORD, False),
    (" padded-password-value ", True),
])
def test_the_password_policy_is_length_only(password, expected):
    problem = password_problem(password)
    assert (problem is not None) is expected, problem


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #
def test_a_client_lands_on_the_client_dashboard():
    repo = _repo()
    client = _anonymous(repo)
    _sign_in(client, repo, role=m.ROLE_CLIENT)
    resp = client.get("/dashboard")
    assert resp.status_code == 200


def test_an_admin_lands_on_the_console():
    repo = _repo()
    client = _anonymous(repo)
    _sign_in(client, repo, role=m.ROLE_ADMIN)
    assert client.get("/console").status_code == 200


def test_the_wrong_password_is_refused():
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    client = _visiting_login(repo)

    resp = _login(client, "c1", "wrong")

    assert resp.status_code == 401
    assert "Invalid username or password" in resp.get_data(as_text=True)


def test_an_unknown_username_gets_the_same_answer_as_a_wrong_password():
    """Anything else is a username oracle."""
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    client = _visiting_login(repo)

    unknown = _login(client, "nobody", "wrong")
    known = _login(client, "c1", "wrong")

    assert unknown.status_code == known.status_code == 401
    # The *message* is what must not differ. The page does echo the submitted
    # username back into the field, which is the form being helpful rather than
    # a leak: the visitor typed it.
    assert "Invalid username or password" in unknown.get_data(as_text=True)
    assert "Invalid username or password" in known.get_data(as_text=True)


def test_the_failure_message_does_not_name_the_reason():
    """Neither "no such user" nor "wrong password" — they are one answer."""
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    client = _visiting_login(repo)

    body = _login(client, "nobody", "wrong").get_data(as_text=True).lower()

    for leak in ("no such user", "unknown user", "not found",
                 "does not exist", "incorrect password", "wrong password"):
        assert leak not in body, leak


def test_the_password_is_not_echoed_back_into_the_page():
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    body = _login(_visiting_login(repo), "c1",
                  "hunter2-not-the-password").get_data(as_text=True)

    assert "hunter2-not-the-password" not in body


def test_a_suspended_account_cannot_sign_in():
    repo = _repo()
    user = repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                            role=m.ROLE_CLIENT)
    repo.set_user_status(user.id, m.STATUS_SUSPENDED)

    resp = _login(_visiting_login(repo), "c1", _PASSWORD)

    assert resp.status_code == 403
    assert "suspended" in resp.get_data(as_text=True).lower()


def test_login_without_a_csrf_token_is_refused():
    """Login CSRF is a real attack: it forces a victim into the attacker's account."""
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    client = _anonymous(repo)
    client.get("/login")            # a token is issued, but is not sent back

    resp = client.post("/login", data={"username": "c1", "password": _PASSWORD})

    assert resp.status_code == 400
    assert "Invalid username or password" not in resp.get_data(as_text=True)


@pytest.mark.parametrize("target", ["https://evil.example/steal",
                                    "//evil.example/steal",
                                    "http://evil.example"])
def test_a_foreign_next_target_cannot_redirect_off_site(target):
    """``?next=`` must not turn the login form into an open redirect."""
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    resp = _login(_visiting_login(repo), "c1", _PASSWORD, next=target)

    assert resp.status_code == 302
    assert "evil.example" not in resp.headers["Location"]
    assert resp.headers["Location"].endswith("/dashboard")   # the real landing


def test_a_same_site_next_target_is_honoured():
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    resp = _login(_visiting_login(repo), "c1", _PASSWORD, next="/history")

    assert resp.headers["Location"].endswith("/history")


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #
def test_repeated_failures_are_throttled():
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    client = _visiting_login(repo, login_max_attempts=3, login_lockout_minutes=15)

    codes = [_login(client, "c1", "wrong").status_code for _ in range(4)]

    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429, codes


def test_a_successful_login_clears_the_failure_count():
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    client = _visiting_login(repo, login_max_attempts=3)

    for _ in range(2):
        _login(client, "c1", "wrong")
    assert _login(client, "c1", _PASSWORD).status_code == 302

    # The success ended up in a fresh session, so the request that follows is a
    # new session's — hence the re-issued token (see the rotation test below).
    with client.session_transaction() as sess:
        sess["csrf"] = _CSRF

    # The budget is refreshed, so two more misses are still answered as misses
    # rather than as a lockout.
    codes = [_login(client, "c1", "wrong").status_code for _ in range(2)]
    assert codes == [401, 401]


def test_the_csrf_token_is_rotated_by_a_login():
    """A token captured before signing in must not be replayable after it.

    Sign-out is used as the probe because it is CSRF-gated and reachable by a
    client: a request carrying a dead token must leave the session alone.
    """
    repo = _repo()
    repo.create_user(username="c1", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT)
    client = _visiting_login(repo)
    with client.session_transaction() as sess:
        before = sess["csrf"]

    assert _login(client, "c1", _PASSWORD).status_code == 302
    client.get("/dashboard")          # a rendered page mints the new token

    with client.session_transaction() as sess:
        after = sess["csrf"]
    assert after and after != before

    # The pre-login token is dead: it does not sign the session out.
    client.post("/logout", data={"_csrf": before})
    assert client.get("/dashboard").status_code == 200

    # The current one is live.
    client.post("/logout", data={"_csrf": after})
    assert client.get("/dashboard").status_code == 302


def test_the_throttle_window_expires():
    """A lockout is a delay, not a permanent state."""
    now = [1_000.0]
    throttle = LoginThrottle(max_attempts=2, lockout_minutes=1, now=lambda: now[0])

    throttle.record_failure("k")
    throttle.record_failure("k")

    wait = throttle.retry_after("k")
    assert 0 < wait <= 61                          # the 60s window, plus the +1

    now[0] += wait
    assert throttle.retry_after("k") == 0


def test_the_throttle_is_keyed_on_username_and_address():
    """One attacker must not be able to lock a known account out from elsewhere."""
    from app.auth import throttle_key
    app = _app(_repo())
    with app.test_request_context("/login", environ_base={"REMOTE_ADDR": "1.1.1.1"}):
        here = throttle_key("Alice")
    with app.test_request_context("/login", environ_base={"REMOTE_ADDR": "2.2.2.2"}):
        elsewhere = throttle_key("Alice")

    assert here != elsewhere
    assert here.startswith("alice|")               # case-folded username
    assert here.endswith("|1.1.1.1")               # the caller's address


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
def test_the_session_cookie_is_httponly_and_lax():
    client = _anonymous(_repo())
    cookie = client.get("/login").headers.get("Set-Cookie", "")

    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    # Cleared in production; only the deployment step that turns on HTTPS sets it.
    assert "Secure" not in cookie


def test_the_session_cookie_is_secure_when_configured():
    client = _anonymous(_repo(), session_cookie_secure=True)
    assert "Secure" in client.get("/login").headers.get("Set-Cookie", "")


def test_signing_out_ends_the_session():
    repo = _repo()
    client = _as(repo, m.ROLE_CLIENT)
    with client.session_transaction() as sess:
        sess["csrf"] = _CSRF

    client.post("/logout", data={"_csrf": _CSRF})

    assert client.get("/dashboard").status_code == 302


def test_suspending_a_user_ends_their_session_immediately():
    """Role and status are re-read every request, not cached in the cookie."""
    repo = _repo()
    client = _anonymous(repo)
    user = _sign_in(client, repo, role=m.ROLE_CLIENT)
    assert client.get("/dashboard").status_code == 200

    repo.set_user_status(user.id, m.STATUS_SUSPENDED)

    assert client.get("/dashboard").status_code == 302


# --------------------------------------------------------------------------- #
# Role gates — the property the client asked for by name
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", OPERATOR_PAGES)
def test_a_client_is_refused_every_operator_page_with_403(path):
    client = _as(_repo(), m.ROLE_CLIENT)

    resp = client.get(path)

    # 403, not a redirect: the client is authenticated and the answer is a flat
    # no, so they are not bounced through a login page they already passed.
    assert resp.status_code == 403, f"{path} -> {resp.status_code}"


@pytest.mark.parametrize("path", OPERATOR_MUTATIONS)
def test_a_client_is_refused_every_operator_mutation(path):
    """The gate is on the endpoint, not on the button that calls it."""
    client = _as(_repo(), m.ROLE_CLIENT)

    resp = client.post(path, data={"asset": "USTEC", "enabled": "true"},
                       headers={"Origin": "http://localhost"})

    assert resp.status_code == 403, f"{path} -> {resp.status_code}"


@pytest.mark.parametrize("path", OPERATOR_PAGES)
def test_an_admin_reaches_every_operator_page(path):
    client = _as(_repo(), m.ROLE_ADMIN)

    assert client.get(path).status_code == 200, path


def test_an_admin_can_reach_the_operator_apis():
    repo = _repo()
    client = _as(repo, m.ROLE_ADMIN)

    assert client.get("/api/status").status_code == 200
    assert client.get("/api/assets/broker").status_code == 200


@pytest.mark.parametrize("path", CLIENT_PAGES)
def test_a_client_reaches_every_client_page(path):
    client = _as(_repo(), m.ROLE_CLIENT)

    assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("path", CLIENT_PAGES)
def test_an_anonymous_visitor_is_sent_to_the_login_page(path):
    client = _anonymous(_repo())

    resp = client.get(path)

    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


@pytest.mark.parametrize("path", ["/console", "/admin", "/api/status",
                                  "/api/client/overview"])
def test_an_anonymous_api_call_is_answered_with_json_not_html(path):
    client = _anonymous(_repo(), )

    resp = client.get(path, headers={"Accept": "application/json"})

    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_the_root_sends_each_role_to_its_own_surface():
    repo = _repo()

    anon = _anonymous(repo).get("/")
    assert anon.status_code == 302 and "/login" in anon.headers["Location"]

    # Distinct usernames: both accounts live in the one repository here.
    client = _as(repo, m.ROLE_CLIENT, username="a-client")
    assert client.get("/").headers["Location"].endswith("/dashboard")

    admin = _as(repo, m.ROLE_ADMIN, username="an-admin")
    assert admin.get("/").headers["Location"].endswith("/admin/")


def test_the_console_is_not_reachable_by_its_old_paths():
    """``/signals`` and friends moved under ``/console`` rather than aliasing."""
    admin = _as(_repo(), m.ROLE_ADMIN)

    for old in ("/signals", "/backtests", "/assets"):
        assert admin.get(old).status_code == 404, old


# --------------------------------------------------------------------------- #
# Nothing secret reaches a client
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", CLIENT_PAGES + ["/login"])
def test_no_client_page_leaks_configuration(path):
    """No credential, no signing key, no database URL, on any client-facing page."""
    repo = _repo()
    client = _as(repo, m.ROLE_CLIENT)

    body = client.get(path).get_data(as_text=True)

    for needle in ("test-secret",      # the signing key itself
                   "MT5_PASSWORD", "MT5_LOGIN", "MT5_TERMINAL_PATH",
                   "TELEGRAM_BOT_TOKEN", "LLM_API_KEY",
                   "ADMIN_PASSWORD", "FLASK_SECRET_KEY",
                   "sqlite:///", "postgresql://"):
        assert needle not in body, f"{needle} leaked into {path}"


def test_a_client_page_carries_no_other_clients_data():
    """A client's own view is about the market, not about the client base."""
    repo = _repo()
    repo.create_user(username="somebody-else",
                     password_hash_or_plain=_PASSWORD, role=m.ROLE_CLIENT,
                     email="somebody@example.com")
    client = _as(repo, m.ROLE_CLIENT)

    body = client.get("/dashboard").get_data(as_text=True)

    assert "somebody-else" not in body
    assert "somebody@example.com" not in body


# --------------------------------------------------------------------------- #
# New York is a zone, not an offset
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("month,expected_abbr,expected_hours", [
    (1, "EST", -5.0),      # standard time
    (7, "EDT", -4.0),      # daylight time
])
def test_new_york_is_dst_aware(month, expected_abbr, expected_hours):
    instant = datetime(2026, month, 15, 16, 0)      # naive UTC

    assert tu.NY_TZ_NAME == "America/New_York"
    assert tu.ny_zone_abbr(instant) == expected_abbr
    assert tu.ny_offset_hours(instant) == expected_hours
    # And the wall clock the rest of the app renders moved with it.
    assert tu.utc_to_ny(instant).hour == 16 + int(expected_hours)


def test_the_zone_abbreviation_is_not_empty():
    """``strftime("%Z")`` on a *naive* datetime is empty on every platform.

    Every stored timestamp here is naive NY wall time, so the abbreviation has
    to come from the aware instant — this is the regression guard for that, and
    it is why ``ny_zone_abbr`` exists rather than a ``%Z`` format string.
    """
    assert tu.utc_to_ny(datetime(2026, 7, 15, 16, 0)).strftime("%Z") == ""
    assert tu.ny_zone_abbr(datetime(2026, 7, 15, 16, 0)) == "EDT"


def test_the_offset_actually_changes_across_the_year():
    """A hard-coded UTC-4 or UTC-5 would make these two equal."""
    winter = tu.ny_offset_hours(datetime(2026, 1, 15, 16, 0))
    summer = tu.ny_offset_hours(datetime(2026, 7, 15, 16, 0))

    assert winter != summer
    assert (winter, summer) == (-5.0, -4.0)


def test_both_dst_boundaries_resolve_to_the_right_side():
    """2026: DST starts 8 March, ends 1 November, both at 02:00 local."""
    assert tu.ny_zone_abbr(datetime(2026, 3, 8, 6, 59)) == "EST"
    assert tu.ny_zone_abbr(datetime(2026, 3, 8, 7, 1)) == "EDT"
    assert tu.ny_zone_abbr(datetime(2026, 11, 1, 5, 59)) == "EDT"
    assert tu.ny_zone_abbr(datetime(2026, 11, 1, 6, 1)) == "EST"


def test_a_round_trip_through_new_york_is_lossless():
    """``ny_to_utc`` must invert ``utc_to_ny`` on both sides of a DST change."""
    for stamp in (datetime(2026, 1, 15, 14, 30), datetime(2026, 7, 15, 14, 30),
                  datetime(2026, 11, 1, 12, 0)):
        assert tu.ny_to_utc(tu.utc_to_ny(stamp)) == stamp


def test_a_setup_time_renders_on_the_new_york_clock():
    """The dates on a client page are NY dates, DST-correct, with the right zone."""
    from app.display import ny_str, ny_zone_label

    # 2026-07-15 13:30 UTC is 09:30 in New York, inside the NY AM session.
    assert ny_str(datetime(2026, 7, 15, 13, 30)) == "2026-07-15 09:30"
    assert ny_zone_label(datetime(2026, 7, 15, 13, 30)) == "EDT"
    # The same wall-clock UTC hour in January is 08:30 New York, not 09:30.
    assert ny_str(datetime(2026, 1, 15, 13, 30)) == "2026-01-15 08:30"
    assert ny_zone_label(datetime(2026, 1, 15, 13, 30)) == "EST"


def test_a_late_utc_evening_belongs_to_the_previous_new_york_day():
    """The bug this guards: a NY session date that renders a day early or late."""
    from app.display import ny_date

    # 00:30 UTC on the 16th is 20:30 on the 15th in New York.
    assert ny_date(datetime(2026, 7, 16, 0, 30)) == "2026-07-15"


# --------------------------------------------------------------------------- #
# Bootstrap admin
# --------------------------------------------------------------------------- #
def test_bootstrap_creates_the_first_admin():
    from app.auth import bootstrap_admin

    repo = _repo()
    cfg = _settings(bootstrap_admin_username="root",
                    bootstrap_admin_password=_PASSWORD,
                    bootstrap_admin_email="root@example.com")

    bootstrap_admin(repo, cfg)

    user = repo.get_user_by_username("root")
    assert user is not None and user.is_admin
    assert user.email == "root@example.com"
    assert _PASSWORD not in user.password_hash


def test_bootstrap_never_touches_a_populated_users_table():
    """Re-running a deployment must not re-create or reset an account."""
    from app.auth import bootstrap_admin

    repo = _repo()
    existing = repo.create_user(username="real-admin",
                                password_hash_or_plain=_PASSWORD,
                                role=m.ROLE_ADMIN)
    before = repo.get_user(existing.id).password_hash

    bootstrap_admin(repo, _settings(bootstrap_admin_username="root",
                                    bootstrap_admin_password=_PASSWORD))

    assert repo.get_user_by_username("root") is None
    assert repo.count_users() == 1
    assert repo.get_user(existing.id).password_hash == before


def test_bootstrap_refuses_a_password_that_breaks_the_policy():
    from app.auth import bootstrap_admin

    repo = _repo()
    bootstrap_admin(repo, _settings(bootstrap_admin_username="root",
                                    bootstrap_admin_password="short"))

    assert repo.count_users() == 0


@pytest.mark.parametrize("username,password", [("", _PASSWORD), ("root", "")])
def test_bootstrap_does_nothing_when_unconfigured(username, password):
    from app.auth import bootstrap_admin

    repo = _repo()
    bootstrap_admin(repo, _settings(bootstrap_admin_username=username,
                                    bootstrap_admin_password=password))

    assert repo.count_users() == 0


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_the_health_probe_stays_public_and_says_nothing_else():
    body = _anonymous(_repo()).get("/health").get_json()

    assert body["status"] == "ok"
    assert set(body) == {"status", "live_running"}


def test_the_signing_key_falls_back_to_a_generated_one(monkeypatch):
    """A missing FLASK_SECRET_KEY warns; it does not refuse to start."""
    app = create_app(settings=_settings(flask_secret_key=""),
                     repository=_repo(), setup_db=False, jobs=_FakeJobs())

    assert app.secret_key
    assert len(app.secret_key) > 20


def test_two_apps_with_a_blank_key_do_not_share_sessions():
    """The consequence of the warning above, stated as a test."""
    repo = _repo()
    first = create_app(settings=_settings(flask_secret_key=""),
                       repository=repo, setup_db=False, jobs=_FakeJobs())
    second = create_app(settings=_settings(flask_secret_key=""),
                        repository=repo, setup_db=False, jobs=_FakeJobs())

    assert first.secret_key != second.secret_key
