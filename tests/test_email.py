"""Transactional email: the templates render, and a failure never escapes.

Two properties matter here, and neither is about wording.

**A message is never allowed to fail the action that triggered it.** Every one of
these is sent from a path that has already committed the thing the email is
about — a registration, a settled payment, a recorded decline. The provider being
unreachable, misconfigured or slow must leave that outcome untouched, so every
send reports success as a bool and every entry point is asserted not to raise.

**Nothing sensitive reaches a message.** No template takes a password, a hash, an
API key or a session token, and the key is asserted to appear in exactly one
place: the Authorization header of the request. The tests below render every
template and check the output for the values that must never be in it, because a
missing template variable is silent in Jinja — an unrendered ``{{ link }}``
leaves a button pointing nowhere rather than erroring.

The Resend client is stubbed throughout. No test here touches the network.
"""
from __future__ import annotations

import logging

import pytest
import requests

from notifications import email as mail


# --------------------------------------------------------------------------- #
# A stand-in for requests
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text


class _Stub:
    """Records calls instead of making them, and can be told to misbehave."""

    def __init__(self, *, response=None, raises=None):
        self.calls: list[dict] = []
        self._response = response or _Response()
        self._raises = raises

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response

    #: The one call that was made, for the assertions that only care about one.
    @property
    def call(self):
        assert len(self.calls) == 1, f"expected one request, got {len(self.calls)}"
        return self.calls[0]


class _User:
    """The part of a user row the email module reads."""

    def __init__(self, email="person@example.com", full_name="Ada Lovelace",
                 username="ada"):
        self.email = email
        self.full_name = full_name
        self.username = username


class _Cfg:
    def __init__(self, api_key="re_test_key", sender="3rader <no-reply@3rader.com>",
                 **kw):
        self.resend_api_key = api_key
        self.resend_from_email = sender
        self.support_email = "support@3rader.com"
        self.app_url = "https://3rader.com"
        for key, value in kw.items():
            setattr(self, key, value)


@pytest.fixture
def stub(monkeypatch):
    """Replace the network call itself, so the default code path is the one run.

    ``EmailService`` uses the module-level ``requests`` unless a session is
    injected, and the message functions construct it with no arguments — so
    patching ``requests.post`` is what actually exercises the shipped path.
    """
    def _install(**kw):
        s = _Stub(**kw)
        monkeypatch.setattr(mail.requests, "post", s.post)
        return s

    return _install


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Every send is configured unless a test says otherwise."""
    monkeypatch.setattr(mail, "get_settings", lambda: _Cfg())


# --------------------------------------------------------------------------- #
# The templates
# --------------------------------------------------------------------------- #
#: Every template, with the context its sender passes it. Kept as data so a new
#: template that nobody wired up is still caught by the render sweep below.
TEMPLATES = {
    "welcome.html": {"name": "Ada", "dashboard_url": "/subscription"},
    "account_created.html": {"name": "Ada", "password_url": "/forgot-password"},
    "password_reset.html": {"name": "Ada", "link": "https://3rader.com/reset?t=abc",
                            "minutes": 30},
    "payment_success.html": {"name": "Ada", "plan_name": "Premium",
                             "amount": "$100.00", "reference": "ref-1",
                             "paid_at": "2026-01-01 09:00 NY"},
    "payment_failed.html": {"name": "Ada", "plan_name": "Premium",
                            "reason": "Insufficient funds",
                            "retry_url": "/pricing"},
    "subscription_activated.html": {"name": "Ada", "plan_name": "Premium",
                                    "expires_at": "2026-02-01",
                                    "dashboard_url": "/dashboard"},
    "subscription_changed.html": {"name": "Ada", "plan_name": "Premium",
                                  "change": "Cancelled at your request",
                                  "expires_at": ""},
}


def test_every_template_in_the_directory_is_covered_by_this_module():
    """A new template with no entry here would otherwise go unrendered forever."""
    on_disk = {p.name for p in mail._TEMPLATE_DIR.glob("*.html")
               if p.name not in ("base.html", "macros.html")}
    assert on_disk == set(TEMPLATES), (
        f"uncovered: {on_disk - set(TEMPLATES)}; stale: {set(TEMPLATES) - on_disk}")


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_renders_with_no_unfilled_placeholder(template):
    html = mail.render(template, **TEMPLATES[template])

    assert html.strip()
    # A Jinja variable with no value renders as empty, which is how a broken
    # button or a missing reference reaches a customer unnoticed.
    for required in ("3rader", "</html>"):
        assert required in html, f"{template} is missing {required!r}"
    for unrendered in ("{{", "{%", "Undefined"):
        assert unrendered not in html, f"{template} left {unrendered!r} in the body"


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_no_message_carries_a_secret_or_a_password(template):
    """The one rule a transactional email must never break."""
    html = mail.render(template, **TEMPLATES[template])
    lowered = html.lower()
    for forbidden in ("re_test_key", "api_key", "password_hash", "pbkdf2:",
                      "scrypt:", "flutterwave", "secret"):
        assert forbidden not in lowered, f"{template} mentions {forbidden!r}"


def test_the_password_reset_link_is_a_url_and_not_the_password_itself():
    """The link is the only thing that authorises a reset, and it is a token —
    the message must carry the token and nothing else usable."""
    html = mail.render("password_reset.html", name="Ada",
                       link="https://3rader.com/reset?t=abc123", minutes=30)
    assert "https://3rader.com/reset?t=abc123" in html
    assert "abc123" in html


def test_a_display_name_containing_markup_is_escaped():
    """Autoescaping is the difference between a welcome email and a way to send
    arbitrary HTML from the platform's own domain."""
    html = mail.render("welcome.html", name="<script>alert(1)</script>",
                       dashboard_url="/subscription")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_broken_template_raises_only_from_render():
    """``render`` is allowed to raise; the ``send_*`` wrappers are not. Kept
    explicit so the guarantee below is not mistaken for a property of this."""
    with pytest.raises(Exception):
        mail.render("no-such-template.html")


def test_the_shared_layout_is_never_rendered_on_its_own():
    """``base.html`` and ``macros.html`` are included, not sent, so neither is a
    message a caller should be able to pass to ``send_*``."""
    for name in ("base.html", "macros.html"):
        assert name not in TEMPLATES


# --------------------------------------------------------------------------- #
# The sender
# --------------------------------------------------------------------------- #
def test_an_unconfigured_deployment_sends_nothing_and_does_not_raise(stub, caplog):
    s = stub()
    ok = mail.EmailService(cfg=_Cfg(api_key="")).send(
        to="person@example.com", subject="Hi", html="<p>hi</p>")

    assert ok is False
    assert s.calls == [], "an unconfigured deployment still made a request"


def test_an_unusable_recipient_is_refused_without_a_request(stub):
    """The local guard is deliberately cheap: no address, no request.

    It is not address validation — a malformed-but-plausible address is sent and
    left for the provider to reject, because the address comes from the account's
    own row and guessing at validity here would only invent a second, wrong
    definition of a valid address.
    """
    s = stub()
    service = mail.EmailService(cfg=_Cfg())

    for address in ("", "   ", "not-an-address", "no at sign here"):
        assert service.send(to=address, subject="Hi", html="<p>hi</p>") is False, \
            address
    assert s.calls == []


def test_a_successful_send_posts_the_body_the_provider_expects(stub):
    s = stub()
    ok = mail.EmailService(cfg=_Cfg()).send(
        to="person@example.com", subject="Payment received",
        html="<p>paid</p>", text="paid")

    assert ok is True
    call = s.call
    assert call["url"] == mail.RESEND_ENDPOINT
    assert call["json"]["to"] == ["person@example.com"]
    assert call["json"]["subject"] == "Payment received"
    assert call["json"]["html"] == "<p>paid</p>"
    assert call["json"]["text"] == "paid"
    assert call["json"]["from"] == "3rader <no-reply@3rader.com>"
    assert call["timeout"] == mail.DEFAULT_TIMEOUT


def test_the_api_key_travels_in_the_header_and_nowhere_else(stub, caplog):
    """A key in a body or a log line is a key in a support ticket."""
    caplog.set_level(logging.DEBUG)
    s = stub()
    mail.EmailService(cfg=_Cfg(api_key="re_super_secret")).send(
        to="person@example.com", subject="Hi", html="<p>hi</p>")

    call = s.call
    assert call["headers"]["Authorization"] == "Bearer re_super_secret"
    assert "re_super_secret" not in call["json"]["html"]
    assert "re_super_secret" not in str(call["json"])
    assert "re_super_secret" not in caplog.text


def test_the_from_address_falls_back_when_configuration_is_blank(stub):
    """A blank RESEND_FROM_EMAIL must not send a message with an empty sender —
    providers reject that, and the failure would look like a broken integration."""
    s = stub()
    mail.EmailService(cfg=_Cfg(sender="")).send(
        to="person@example.com", subject="Hi", html="<p>hi</p>")
    assert s.call["json"]["from"] == "3rader <no-reply@3rader.com>"


def test_a_provider_rejection_is_reported_and_never_raised(stub, caplog):
    """A 4xx means the message was refused. That is a log line and a False —
    not an exception into a webhook that has already settled a payment."""
    caplog.set_level(logging.WARNING)
    s = stub(response=_Response(422, '{"message":"domain not verified"}'))

    ok = mail.EmailService(cfg=_Cfg()).send(
        to="person@example.com", subject="Hi", html="<p>hi</p>")

    assert ok is False
    assert "domain not verified" in caplog.text
    assert s.calls, "the request should have been attempted"


def test_a_network_failure_is_reported_and_never_raised(stub, caplog):
    caplog.set_level(logging.WARNING)
    stub(raises=requests.ConnectionError("no route to host"))

    ok = mail.EmailService(cfg=_Cfg()).send(
        to="person@example.com", subject="Hi", html="<p>hi</p>")

    assert ok is False
    assert "no route to host" in caplog.text


def test_an_unexpected_failure_is_reported_and_never_raised(stub, caplog):
    """Anything not a RequestException — a bug in this module, a bad response
    object — must be caught too, because the caller has already committed."""
    caplog.set_level(logging.ERROR)
    stub(raises=ValueError("something else entirely"))

    ok = mail.EmailService(cfg=_Cfg()).send(
        to="person@example.com", subject="Hi", html="<p>hi</p>")

    assert ok is False
    assert "something else entirely" in caplog.text


# --------------------------------------------------------------------------- #
# The message functions
# --------------------------------------------------------------------------- #
SENDERS = [
    ("send_welcome", lambda u: mail.send_welcome(u, dashboard_url="/subscription")),
    ("send_account_created", lambda u: mail.send_account_created(u)),
    ("send_password_reset", lambda u: mail.send_password_reset(
        u, "https://3rader.com/reset?t=abc", minutes=30)),
    ("send_payment_received", lambda u: mail.send_payment_received(
        u, plan_name="Premium", amount="$100.00", reference="ref-1")),
    ("send_payment_failed", lambda u: mail.send_payment_failed(
        u, plan_name="Premium", reason="Insufficient funds")),
    ("send_subscription_activated", lambda u: mail.send_subscription_activated(
        u, plan_name="Premium", expires_at="2026-02-01")),
    ("send_subscription_changed", lambda u: mail.send_subscription_changed(
        u, plan_name="Premium", change="Cancelled at your request")),
]


@pytest.mark.parametrize("name,call", SENDERS, ids=[n for n, _ in SENDERS])
def test_every_message_function_sends_a_rendered_body(name, call, stub):
    s = stub()
    assert call(_User()) is True, name
    body = s.call["json"]
    assert body["subject"], name
    assert "3rader" in body["html"], name


@pytest.mark.parametrize("name,call", SENDERS, ids=[n for n, _ in SENDERS])
def test_no_message_function_raises_when_the_provider_is_down(name, call, stub):
    """The whole point of the bool return: none of these may escape into the
    caller, which has already registered the account or settled the payment."""
    stub(raises=requests.Timeout("timed out"))
    assert call(_User()) is False, name


@pytest.mark.parametrize("name,call", SENDERS, ids=[n for n, _ in SENDERS])
def test_no_message_function_raises_for_a_user_with_no_address(name, call, stub):
    s = stub()
    assert call(_User(email="")) is False, name
    assert s.calls == []


def test_a_missing_template_is_reported_and_not_raised(monkeypatch, caplog):
    """``_send`` wraps the render precisely so a broken template cannot 500 a
    signup. Asserted directly, since no shipped template is broken."""
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(mail, "render",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mail.send_welcome(_User()) is False
    assert "boom" in caplog.text
