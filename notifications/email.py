"""Transactional email through Resend.

Every message the platform sends is rendered from a template in
``notifications/templates/email/`` that extends a shared branded layout, so
adding a message means adding one template rather than writing markup again.

Two rules this module holds to:

* **A send failure is never fatal to the action that triggered it.** Registering,
  paying or subscribing all succeed whether or not the mail provider is
  reachable. Every public ``send_*`` catches its own errors and returns a bool;
  nothing here raises into a request handler or a webhook.
* **No secret and no sensitive value is put in a message.** Templates receive
  only what the email has to say. The API key is read from configuration and
  used in one request header, and never appears in a body, a log line or a
  rendered message.

Calls go to Resend's REST API through ``requests``, the dependency the project
already uses for Telegram and the LLM overlay — so this adds no new library.

The module deliberately does **not** import Flask's ``render_template``: it is
also called from a webhook thread and a background context, where there is no
request to render against. A standalone Jinja environment is used instead.
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import get_settings

log = logging.getLogger(__name__)

#: Resend's send endpoint. Fixed, not configurable — the same reasoning as the
#: Flutterwave base URL: an endpoint a deployment could redirect is a way to
#: mail a customer's data at a host of someone else's choosing.
RESEND_ENDPOINT = "https://api.resend.com/emails"

DEFAULT_TIMEOUT = 15

_TEMPLATE_DIR = Path(__file__).parent / "templates" / "email"

#: Autoescaping on for HTML, so a display name containing markup cannot inject
#: itself into a message. The templates render user-supplied values (a person's
#: name, a plan's name), which makes this the difference between a welcome email
#: and a way to send arbitrary HTML from the platform's own domain.
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(template: str, **context) -> str:
    """Render one email template. Raises only for a missing/broken template."""
    common = {
        "support_email": _support_email(),
        "app_url": (getattr(get_settings(), "app_url", "") or "").rstrip("/"),
        "brand_name": "3rader",
        "year": _year(),
    }
    common.update(context)
    return _env.get_template(template).render(**common)


def _support_email() -> str:
    return getattr(get_settings(), "support_email", "") or "support@3rader.com"


def _year() -> int:
    from trading import time_utils as tu
    return tu.now_utc().year


class EmailService:
    """Sends rendered templates through Resend. Never raises on send."""

    def __init__(self, cfg=None, *, timeout: int = DEFAULT_TIMEOUT, session=None):
        self.cfg = cfg or get_settings()
        self.timeout = timeout
        #: Injectable so tests exercise every branch with no network.
        self._session = session or requests

    @property
    def api_key(self) -> str:
        return (getattr(self.cfg, "resend_api_key", "") or "").strip()

    @property
    def sender(self) -> str:
        return (getattr(self.cfg, "resend_from_email", "")
                or "3rader <no-reply@3rader.com>")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def send(self, *, to: str, subject: str, html: str, text: str = "") -> bool:
        """Deliver one message. Returns whether it was accepted by Resend.

        Missing configuration, a network error, a rejected request and an
        unexpected response are all handled the same way: logged, and reported as
        ``False``. A caller that cares can look, and a caller that does not — the
        registration handler, say — is unaffected.
        """
        if not to or "@" not in to:
            log.info("Email %r not sent: no usable recipient address", subject)
            return False
        if not self.configured:
            log.info("Email %r not sent: RESEND_API_KEY is not configured",
                     subject)
            return False

        body = {"from": self.sender, "to": [to], "subject": subject, "html": html}
        if text:
            body["text"] = text
        try:
            response = self._session.post(
                RESEND_ENDPOINT, json=body, timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"})
            if response.status_code >= 400:
                # The response body can echo the address and the subject, so it
                # is logged but never surfaced to a caller or a template.
                log.warning("Resend rejected %r (%s): %s", subject,
                            response.status_code, response.text[:300])
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Resend call failed for %r: %s", subject, exc)
            return False
        except Exception:  # pragma: no cover - defensive
            log.exception("Unexpected failure sending %r", subject)
            return False


# --------------------------------------------------------------------------- #
# The messages
#
# One function per event. Each renders a template, sends, and reports success as
# a bool — never raising, because every one of these is called from a path that
# has already committed the thing the email is about.
# --------------------------------------------------------------------------- #
def _send(template: str, subject: str, user, **context) -> bool:
    to = getattr(user, "email", "") or ""
    try:
        html = render(template, name=getattr(user, "full_name", "") or "there",
                      **context)
    except Exception:  # pragma: no cover - a broken template must not 500 a signup
        log.exception("Could not render the %r email template", template)
        return False
    return EmailService().send(to=to, subject=subject, html=html)


def send_welcome(user, *, dashboard_url: str = "") -> bool:
    """The registration confirmation."""
    return _send("welcome.html", "Welcome to 3rader",
                 user, dashboard_url=dashboard_url or "/subscription")


def send_password_reset(user, link: str, *, minutes: int = 30) -> bool:
    """The password-reset link."""
    return _send("password_reset.html", "Reset your 3rader password",
                 user, link=link, minutes=minutes)


def send_payment_received(user, *, plan_name: str, amount: str,
                          reference: str, paid_at: str = "") -> bool:
    """A payment was verified and recorded."""
    return _send("payment_success.html", f"Payment received — {plan_name}",
                 user, plan_name=plan_name, amount=amount,
                 reference=reference, paid_at=paid_at)


def send_subscription_activated(user, *, plan_name: str, expires_at: str = "",
                                dashboard_url: str = "/dashboard") -> bool:
    """A subscription became active."""
    return _send("subscription_activated.html",
                 f"Your {plan_name} subscription is active",
                 user, plan_name=plan_name, expires_at=expires_at,
                 dashboard_url=dashboard_url)


def send_payment_failed(user, *, plan_name: str, reason: str = "",
                        retry_url: str = "/pricing") -> bool:
    """A payment did not complete."""
    return _send("payment_failed.html", f"Payment unsuccessful — {plan_name}",
                 user, plan_name=plan_name, reason=reason, retry_url=retry_url)


def send_subscription_changed(user, *, plan_name: str, change: str,
                              expires_at: str = "") -> bool:
    """A subscription was changed by an administrator."""
    return _send("subscription_changed.html",
                 f"Your subscription was updated — {plan_name}",
                 user, plan_name=plan_name, change=change,
                 expires_at=expires_at)


def send_account_created(user, *, password_url: str = "/forgot-password") -> bool:
    """An administrator created an account on someone's behalf.

    Carries no password: the recipient sets their own through the ordinary reset
    flow, so no credential is ever transmitted in an email. That is also why the
    message does not claim no one can set a password for them — an administrator
    creating an account *does* type one, which the recipient usually does not
    know. The copy offers the reset link instead of overstating the guarantee.
    """
    return _send("account_created.html", "Your 3rader account has been created",
                 user, password_url=password_url)
