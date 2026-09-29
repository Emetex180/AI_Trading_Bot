"""Flutterwave: transaction initialisation, server-side verification, webhooks.

Three properties this module exists to guarantee, because a payment integration
that gets any of them wrong takes real money from real people:

* **Only the server decides a payment succeeded.** The browser is never believed.
  It returns from checkout with a query string anyone can forge; every path that
  could activate a subscription goes through :meth:`FlutterwaveClient.verify`,
  which asks Flutterwave directly over an authenticated connection.
* **The amount is re-checked, not taken on trust.** What the provider reports
  charged is compared against what *we* recorded for that reference, in exact
  integer minor units. A tampered charge of $1 for a $500 plan is refused.
* **Secrets stay server-side.** The secret key and the webhook hash are read
  from configuration and used only in request headers. Nothing here is ever
  rendered into a template, returned in a response body, or logged.

All HTTP goes through ``requests``, which the project already depends on for
Telegram and the LLM overlay — so this adds no new library to the deployment.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import requests

log = logging.getLogger(__name__)

#: Flutterwave's v3 base. Not configurable: it is the provider's endpoint, not
#: a deployment setting, and a URL that could be pointed elsewhere from ``.env``
#: would be a way to redirect a payment flow at a hostile host.
API_BASE = "https://api.flutterwave.com/v3"

#: Sent on every call so a slow provider cannot hang a request thread forever.
DEFAULT_TIMEOUT = 20

#: Flutterwave's own status strings, mapped to ours.
_SUCCESS_STATES = {"successful", "success"}


class PaymentError(Exception):
    """A provider call failed. Carries a message safe to show a customer."""


def to_minor_units(amount) -> int:
    """``"100.50"`` / ``100.5`` -> ``10050`` cents.

    Goes through :class:`~decimal.Decimal` rather than ``float``: the provider
    sends amounts as strings and decimals, and ``int(100.5 * 100)`` is ``10049``
    on a bad day. Rounded half-up to the nearest cent, which is how a currency
    amount is actually defined.
    """
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, AttributeError, ValueError) as exc:
        raise PaymentError(f"Could not read the amount {amount!r}.") from exc
    return int((value * 100).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))


def from_minor_units(minor: int) -> str:
    """``10050`` -> ``"100.50"``, the form the provider's API expects."""
    return f"{Decimal(int(minor)) / 100:.2f}"


@dataclass
class Verification:
    """The outcome of asking Flutterwave what happened to a transaction."""

    ok: bool
    status: str = ""            # our vocabulary: successful/failed/pending
    provider_tx_id: str = ""
    amount_minor: int = 0
    currency: str = ""
    message: str = ""
    #: The provider's payload, for the payment record. Deliberately the response
    #: body only — no key and no card data is in it, and nothing here is rendered
    #: to a customer.
    payload: dict = field(default_factory=dict)


class FlutterwaveClient:
    """A thin, testable wrapper over the three calls the platform makes."""

    def __init__(self, cfg, *, timeout: int = DEFAULT_TIMEOUT, session=None):
        self.cfg = cfg
        self.timeout = timeout
        #: Injectable so tests can drive the whole payment flow with no network.
        self._session = session or requests

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    @property
    def secret_key(self) -> str:
        return (getattr(self.cfg, "flutterwave_secret_key", "") or "").strip()

    @property
    def public_key(self) -> str:
        return (getattr(self.cfg, "flutterwave_public_key", "") or "").strip()

    @property
    def webhook_hash(self) -> str:
        return (getattr(self.cfg, "flutterwave_webhook_secret_hash", "") or "").strip()

    @property
    def configured(self) -> bool:
        """Whether the server can actually talk to Flutterwave.

        Checked before a checkout is offered so an unconfigured deployment says
        "payments are unavailable" instead of sending a customer into a flow
        that cannot complete.
        """
        return bool(self.secret_key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/json",
                "Accept": "application/json"}

    def _url(self, path: str) -> str:
        return f"{API_BASE}/{path.lstrip('/')}"

    # ------------------------------------------------------------------ #
    # Initialize
    # ------------------------------------------------------------------ #
    def initialize(self, *, reference: str, amount_minor: int, currency: str,
                   customer_email: str, customer_name: str = "",
                   customer_phone: str = "", title: str = "3rader subscription",
                   description: str = "", redirect_url: str = "",
                   meta: dict | None = None) -> str:
        """Create a hosted checkout and return its URL.

        Raises :class:`PaymentError` with a customer-safe message on any failure.
        The amounts sent here are ours; the *verified* amount is re-read from the
        provider afterwards and compared, so nothing about this call is trusted
        as evidence that a payment happened.
        """
        if not self.configured:
            raise PaymentError("Card payments are not configured on this "
                               "deployment yet.")

        body = {
            "tx_ref": reference,
            "amount": from_minor_units(amount_minor),
            "currency": currency,
            "redirect_url": redirect_url,
            "customer": {"email": customer_email, "name": customer_name,
                         "phonenumber": customer_phone},
            "customizations": {"title": title, "description": description},
            "meta": meta or {},
        }
        try:
            response = self._session.post(self._url("payments"), json=body,
                                          headers=self._headers(),
                                          timeout=self.timeout)
            data = response.json()
        except requests.RequestException as exc:
            log.warning("Flutterwave initialize failed for %s: %s", reference, exc)
            raise PaymentError("We could not reach the payment provider. Please "
                               "try again.") from exc
        except ValueError as exc:
            log.warning("Flutterwave initialize returned non-JSON for %s",
                        reference)
            raise PaymentError("The payment provider gave an unexpected "
                               "response.") from exc

        link = ((data or {}).get("data") or {}).get("link")
        if not link or str((data or {}).get("status", "")).lower() != "success":
            # The provider's own ``message`` explains why it rejected the
            # request. Logged (it is a description of the rejection, never a
            # secret) but not shown to the customer, who gets a plain retry.
            log.warning("Flutterwave initialize rejected %s: %s", reference,
                        (data or {}).get("message"))
            raise PaymentError("We could not start the payment. Please try "
                               "again.")
        return str(link)

    # ------------------------------------------------------------------ #
    # Verify
    # ------------------------------------------------------------------ #
    def verify(self, reference: str) -> Verification:
        """Ask Flutterwave what happened to ``reference``.

        This is the only source of truth about whether a payment succeeded. The
        lookup is by *our* reference, so a response can only ever describe the
        transaction we created.
        """
        if not self.configured:
            return Verification(ok=False, status="failed",
                                message="Payments are not configured.")

        params = {"tx_ref": reference}
        try:
            response = self._session.get(
                self._url("transactions/verify_by_reference"),
                params=params, headers=self._headers(), timeout=self.timeout)
            data = response.json()
        except requests.RequestException as exc:
            log.warning("Flutterwave verify failed for %s: %s", reference, exc)
            return Verification(ok=False, status="pending",
                                message="We could not confirm the payment with "
                                        "the provider yet.")
        except ValueError:
            return Verification(ok=False, status="pending",
                                message="The provider gave an unexpected "
                                        "response.")

        body = (data or {}).get("data") or {}
        provider_status = str(body.get("status", "")).lower()
        # Every amount field is tried because Flutterwave has returned the
        # charged figure under each of these names across API versions; taking
        # the first present one beats failing a real payment over a rename.
        raw_amount = body.get("charged_amount", body.get("amount"))
        currency = str(body.get("currency", "") or "").upper()

        try:
            amount_minor = to_minor_units(raw_amount)
        except PaymentError:
            amount_minor = 0

        ok = provider_status in _SUCCESS_STATES
        return Verification(
            ok=ok,
            status="successful" if ok else (
                "pending" if provider_status in ("pending", "new", "") else "failed"),
            provider_tx_id=str(body.get("id") or ""),
            amount_minor=amount_minor,
            currency=currency,
            message=str(body.get("processor_response") or provider_status),
            payload=body,
        )

    # ------------------------------------------------------------------ #
    # Webhook
    # ------------------------------------------------------------------ #
    def webhook_signature_ok(self, supplied: str | None) -> bool:
        """Is this webhook genuinely from Flutterwave?

        Flutterwave echoes the secret hash configured in the dashboard in the
        ``verif-hash`` header. Compared with :func:`hmac.compare_digest` so the
        check cannot be walked one byte at a time by timing it.

        An unset hash returns False: without a configured secret there is nothing
        to authenticate the request against, and treating "no secret" as "accept
        anything" would leave every deployment that forgot to set one open to a
        forged activation.
        """
        if not self.webhook_hash or not supplied:
            return False
        return hmac.compare_digest(str(supplied), self.webhook_hash)


def client_for(cfg) -> FlutterwaveClient:
    """The client for this configuration. One construction site, so a test can
    substitute a fake by patching this."""
    return FlutterwaveClient(cfg)
