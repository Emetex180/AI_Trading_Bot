"""Absolute URLs must name the public site, not the loopback the app binds.

IIS terminates TLS and proxies to Waitress on ``127.0.0.1:5000``, so a request
as Flask sees it carries a loopback host and the ``http`` scheme. Every
``_external=True`` URL is built from that — including the ``redirect_url`` the
checkout hands Flutterwave, which is where a customer's bank sends them back to
when a payment finishes. Built from the loopback address it sends the customer
to an address only the proxy can reach, and the payment flow ends on nothing.

The fix is to honour the ``X-Forwarded-*`` headers the IIS rewrite rule sets,
and the property these tests pin is the *allowlist* on top of it: a forwarded
host is only believed when it names a host this deployment actually serves, so
the header can correct the URL without becoming a way to point it somewhere
else. A deployment behind no proxy must be able to leave the whole mechanism
off.

The app is built per-test against its own in-memory database, and the requests
go through ``app.wsgi_app`` — the middleware is part of the WSGI stack, so a
test that called ``url_for`` in a bare request context would exercise none of
what is being tested here.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from flask import url_for

from app.web import create_app
from config import Settings, bare_host, get_settings
from database.models import ROLE_CLIENT

from test_web import _repo, _sign_in

#: The host the site is publicly served on, and the address IIS forwards to.
_PUBLIC_HOST = "3rader.com"
_LOOPBACK_URL = "http://127.0.0.1:5000"
_CALLBACK = "/subscription/callback"


def _settings(**overrides):
    return replace(get_settings(), flask_secret_key="test-secret", **overrides)


def _app(repo, **overrides):
    return create_app(settings=_settings(**overrides), repository=repo,
                      setup_db=False)


@pytest.fixture
def probe():
    """Build an app exposing the callback's absolute URL as a page body.

    The URL has to be produced *inside* a request for ``_external=True`` to
    mean anything, and the request has to go through the WSGI stack for the
    proxy middleware to have run. A route that returns it is the smallest thing
    that gives both.
    """
    def build(**overrides):
        app = _app(_repo(), **overrides)

        @app.get("/_probe/callback")
        def _callback_url():
            return url_for("subscription.callback", _external=True)

        return app

    return build


def _through_proxy(app, *, proto="https", host=_PUBLIC_HOST):
    """A request shaped the way IIS delivers it to Waitress."""
    headers = {}
    if proto is not None:
        headers["X-Forwarded-Proto"] = proto
    if host is not None:
        headers["X-Forwarded-Host"] = host
    return app.test_client().get("/_probe/callback", base_url=_LOOPBACK_URL,
                                 headers=headers)


def _body(response) -> str:
    assert response.status_code == 200, response.status_code
    return response.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# The URL the customer is sent back to
# --------------------------------------------------------------------------- #
def test_the_callback_url_is_the_public_https_address(probe):
    """The regression: the host Flutterwave is told to send the browser to."""
    app = probe(trust_proxy=True, public_host=_PUBLIC_HOST)
    body = _body(_through_proxy(app))

    assert body == f"https://{_PUBLIC_HOST}{_CALLBACK}"
    assert "127.0.0.1" not in body


def test_without_trusting_the_proxy_the_loopback_address_is_still_generated(probe):
    """What the deployment did before, pinned so the headers stay the fix.

    Not a bug being preserved — a bug being *documented*. If this ever starts
    returning the public address without ``TRUST_PROXY``, the app has begun
    guessing at a proxy it was not told about, and the same guess would apply
    to a forged header.
    """
    app = probe(trust_proxy=False, public_host=_PUBLIC_HOST)
    assert _body(_through_proxy(app)) == f"{_LOOPBACK_URL}{_CALLBACK}"


def test_the_checkout_hands_flutterwave_the_public_callback_url(monkeypatch):
    """The real call site, not a stand-in for it.

    ``app/subscription.py`` builds the ``redirect_url`` it passes to the
    provider, so the test drives that route for real — signed in, past CSRF —
    and reads back what the provider was given. Everything except the provider
    itself is the production path.
    """
    repo = _repo()
    app = _app(repo, trust_proxy=True, public_host=_PUBLIC_HOST)
    client = app.test_client()
    _sign_in(client, repo, role=ROLE_CLIENT, plan="basic")
    # Signing in clears the session, so the token is read back from a page that
    # minted a fresh one — the same pattern the profile and admin tests use.
    client.get("/subscription")
    with client.session_transaction() as sess:
        token = sess["csrf"]

    seen: dict = {}

    class _Provider:
        configured = True

        def initialize(self, **kwargs):
            seen.update(kwargs)
            return "https://checkout.flutterwave.com/v3/hosted/pay/stub"

    monkeypatch.setattr("app.subscription.client_for", lambda cfg: _Provider())

    # No ``base_url`` here. The session cookie is host-only, so signing in on
    # the client's default host and then posting to loopback would simply arrive
    # logged out; the forwarded headers replace the host outright either way, so
    # the URL asserted below is still one only a working proxy fix produces.
    response = client.post(
        "/subscription/checkout/basic",
        headers={"X-Forwarded-Proto": "https",
                 "X-Forwarded-Host": _PUBLIC_HOST,
                 "X-CSRF-Token": token},
        data={"_csrf": token})

    assert response.status_code == 302, response.status_code
    assert seen["redirect_url"] == f"https://{_PUBLIC_HOST}{_CALLBACK}"


# --------------------------------------------------------------------------- #
# The allowlist on the forwarded host
# --------------------------------------------------------------------------- #
def test_a_forwarded_host_outside_the_allowlist_is_ignored(probe):
    """The header corrects the URL; it does not decide the domain.

    Everything else the proxy said still applies, which is what makes the
    rejection a targeted one rather than the whole mechanism falling over.
    """
    app = probe(trust_proxy=True, public_host=_PUBLIC_HOST)
    body = _body(_through_proxy(app, host="evil.example"))

    assert body == f"https://127.0.0.1:5000{_CALLBACK}"
    assert "evil.example" not in body


def test_only_the_value_the_trusted_proxy_appended_is_read(probe):
    """A client can prepend to the header; only the proxy can append to it.

    One trusted proxy means the last value in the list is the one IIS wrote,
    and that is the value the allowlist is applied to. Reading the first would
    hand the decision straight back to the client.
    """
    app = probe(trust_proxy=True, public_host=_PUBLIC_HOST)

    ours_last = _body(_through_proxy(app, host=f"evil.example, {_PUBLIC_HOST}"))
    assert ours_last == f"https://{_PUBLIC_HOST}{_CALLBACK}"

    theirs_last = _body(_through_proxy(app, host=f"{_PUBLIC_HOST}, evil.example"))
    assert theirs_last == f"https://127.0.0.1:5000{_CALLBACK}"


def test_a_blank_public_host_trusts_no_forwarded_host(probe):
    """The safe reading of "nothing configured" is "trust nothing".

    Absent an allowlist the request keeps the host IIS sent, so a deployment
    that turns ``TRUST_PROXY`` on and forgets ``PUBLIC_HOST`` gets a URL that is
    wrong in the same way as before rather than one an attacker chose.
    """
    app = probe(trust_proxy=True, public_host="")
    body = _body(_through_proxy(app))

    assert body == f"https://127.0.0.1:5000{_CALLBACK}"
    assert _PUBLIC_HOST not in body


def test_every_name_in_the_allowlist_is_accepted(probe):
    """A deployment can answer on more than one name — apex and ``www``."""
    app = probe(trust_proxy=True, public_host=f"{_PUBLIC_HOST},www.{_PUBLIC_HOST}")
    body = _body(_through_proxy(app, host=f"www.{_PUBLIC_HOST}"))

    assert body == f"https://www.{_PUBLIC_HOST}{_CALLBACK}"


# --------------------------------------------------------------------------- #
# Parsing the allowlist
# --------------------------------------------------------------------------- #
def test_the_public_hosts_are_split_trimmed_and_case_folded():
    settings = _settings(public_host=" 3Rader.COM , www.3rader.com:443 ,")
    assert settings.public_hosts == frozenset({"3rader.com", "www.3rader.com"})


def test_no_public_host_is_configured_by_default():
    assert Settings.__dataclass_fields__["public_host"].default == ""


@pytest.mark.parametrize("value, expected", [
    ("3rader.com", "3rader.com"),
    ("3rader.com:443", "3rader.com"),
    ("3Rader.COM:8443", "3rader.com"),
    ("[::1]:5000", "[::1]"),        # IPv6 literal, port after the bracket
    # Not a port, so not stripped — otherwise this would compare equal to the
    # trusted name and let a crafted host through.
    ("3rader.com:evil", "3rader.com:evil"),
])
def test_bare_host_strips_only_a_numeric_port(value, expected):
    assert bare_host(value) == expected
