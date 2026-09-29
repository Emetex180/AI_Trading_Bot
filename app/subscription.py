"""Subscriptions and card payments — the money path.

Everything a customer can do with a plan lives here: see what they hold, start a
checkout, come back from one, see their receipts, and cancel. Flutterwave is
talked to only through :mod:`app.payments`; this module decides *when*, and
:mod:`app.access` decides what a plan is worth.

The five ways this could go wrong, and where each is stopped
------------------------------------------------------------
1. **The browser claiming success.** The return leg carries a query string anyone
   can type. It is read for the *reference* and nothing else — whether the
   payment succeeded is asked of Flutterwave over an authenticated connection
   (:meth:`app.payments.FlutterwaveClient.verify`), from the server.
2. **Paying less than the plan costs.** The amount is taken from
   :data:`app.plans.PLANS`, never from the request, and the figure the provider
   reports charged is compared against it in exact integer minor units before
   anything is granted.
3. **A payment granting two subscriptions.** :meth:`Repository.settle_payment` is
   a conditional UPDATE, and :meth:`Repository.activate_subscription` is
   idempotent on ``payment_id``. The return leg and the webhook race each other
   routinely, and either may arrive twice; both are safe.
4. **A forged webhook.** ``verif-hash`` is checked with
   :func:`hmac.compare_digest` before the body is looked at, and an unconfigured
   hash refuses everything rather than accepting everything.
5. **Someone else's payment.** Every reference is resolved to a row and that row
   is checked against the signed-in user; a mismatch is a 404, not a hint.

What this module never does: mark a payment successful because a client-side
script or a query parameter said so, trust a plan key or a price from a form,
or log a secret. The only values a request contributes are a plan key (validated
against the catalogue), a payment reference (looked up, then verified), and a
CSRF token.
"""
from __future__ import annotations

import logging
import secrets

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect,
                   render_template, request, url_for)

from .access import entitlement_for
from .auth import current_user, login_required, require_csrf
from .payments import PaymentError, client_for
from .plans import PLANS, format_minor, get_plan, is_valid_key, spec_rows

log = logging.getLogger(__name__)

subscription_bp = Blueprint("subscription", __name__)

#: Our payment provider. Stored on every row so a second provider can be added
#: later without a migration that guesses what the old rows were.
PROVIDER = "flutterwave"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cfg():
    return current_app.config["CFG"]


def _period_days(cfg) -> int:
    return max(1, int(getattr(cfg, "subscription_period_days", 30) or 30))


def _payments_available(cfg) -> bool:
    """Whether a checkout could actually complete.

    Read before any "subscribe" button is drawn. An unconfigured deployment must
    say so plainly rather than walk a customer through a flow that cannot finish
    and leave them uncertain whether they were charged.
    """
    return client_for(cfg).configured


def _new_reference(user_id: int) -> str:
    """A transaction reference that is ours and unique.

    Carries the user id so a reference in a log or a support email is traceable
    without a database lookup, and a random suffix so it cannot be guessed — a
    guessable reference would let someone open a checkout against another
    account's row, which is exactly the class of bug the ownership checks below
    exist to catch.
    """
    from trading import time_utils as tu

    stamp = tu.now_utc().strftime("%Y%m%d%H%M%S")
    return f"3R-{int(user_id)}-{stamp}-{secrets.token_hex(4)}"


def _plan_row(repo, key: str):
    """The stored ``Plan`` row for a key, self-healing if it has gone missing.

    A payment can only be granted against a row that exists. The seeder never
    deletes, so a missing row means the catalogue was edited by hand; re-running
    the idempotent seed (which only inserts unseen keys) restores the canonical
    definition from :mod:`app.plans` rather than failing a customer who has
    already been charged.
    """
    row = repo.get_plan_by_key(key)
    if row is None:
        repo.sync_plans(spec_rows())
        row = repo.get_plan_by_key(key)
    return row


def _finalise(cfg, repo, payment, verification) -> str:
    """Reconcile one verified payment. Returns a short outcome word.

    The amount is checked **before** :meth:`Repository.settle_payment` is called,
    so a tampered charge is recorded as a failure rather than settled at the
    wrong figure. Outcomes:

    ``settled``    payment moved to successful and the subscription granted
    ``replayed``   already settled — the subscription is not granted twice
    ``mismatch``   the provider charged something other than the plan price
    ``not_paid``   the provider does not consider it successful
    ``no_plan``    the plan is no longer in the catalogue
    ``unknown``    the reference does not match a payment row
    """
    if payment is None:
        return "unknown"

    if payment.status == "successful":
        return "replayed"

    if not verification.ok:
        if verification.status == "failed":
            reason = verification.message or "Declined by provider."
            repo.fail_payment(payment.reference, reason)
            _notify_failed(repo, payment, reason)
        # A pending or abandoned checkout is not marked failed and gets no
        # email: silence about a basket the customer walked away from is the
        # correct amount of contact.
        return "not_paid"

    # The amount the provider says was charged must equal the amount we recorded
    # when the checkout was created — which came from the plan catalogue. Both
    # are integer minor units, so this is an exact comparison.
    if int(verification.amount_minor) != int(payment.amount_minor):
        log.warning("Payment %s amount mismatch: expected %s, provider reports "
                    "%s %s", payment.reference, payment.amount_minor,
                    verification.amount_minor, verification.currency)
        reason = "The amount charged did not match the plan price."
        repo.fail_payment(payment.reference, reason)
        _notify_failed(repo, payment, reason)
        return "mismatch"

    # A currency change between checkout and settlement would make the amount
    # comparison meaningless. Only checked when the provider actually names one.
    if verification.currency and payment.currency and \
            verification.currency.upper() != payment.currency.upper():
        reason = "The charge currency did not match the checkout."
        repo.fail_payment(payment.reference, reason)
        _notify_failed(repo, payment, reason)
        return "mismatch"

    # One transaction id must belong to one payment. The column is UNIQUE, so a
    # replay of another customer's transaction id is caught here before the
    # settle would collide on the constraint.
    if verification.provider_tx_id:
        other = repo.get_payment_by_provider_tx(verification.provider_tx_id)
        if other is not None and other.id != payment.id:
            log.warning("Transaction %s already belongs to payment %s; refusing "
                        "%s", verification.provider_tx_id, other.id,
                        payment.reference)
            return "mismatch"

    plan = _plan_row(repo, payment.plan_key)
    if plan is None:
        log.error("Payment %s settled but plan %r is not in the catalogue",
                  payment.reference, payment.plan_key)
        return "no_plan"

    settled, first_time = repo.settle_payment(
        payment.reference,
        provider_tx_id=verification.provider_tx_id,
        amount_minor=verification.amount_minor,
        currency=verification.currency or payment.currency,
        payload=verification.payload,
    )
    if settled is None:
        return "unknown"
    if not first_time:
        # Another delivery of the same event won the race. It has already granted
        # the subscription, so this one stops here.
        return "replayed"

    repo.activate_subscription(user_id=settled.user_id, plan=plan,
                               payment=settled, period_days=_period_days(cfg))
    _announce(cfg, repo, settled, plan)
    return "settled"


def _notify_failed(repo, payment, reason: str) -> None:
    """Tell the customer their charge did not go through. Best-effort.

    Called at exactly the three places that *write* the failed state, so an
    abandoned checkout gets no email and a late failure notice for a payment
    that actually succeeded can never produce one — ``fail_payment`` refuses to
    move a settled row, and this is only reached when it was called.

    A decline is discovered server to server, often after the customer has closed
    the tab, which is why this is an email rather than a flash.
    """
    user = repo.get_user(payment.user_id)
    if user is None:
        return
    spec = get_plan(payment.plan_key)
    try:
        from notifications import email as mail

        mail.send_payment_failed(
            user,
            plan_name=spec.name if spec else payment.plan_key,
            reason=reason,
            retry_url="/pricing")
    except Exception:  # pragma: no cover - a notice must not lose a failure
        log.exception("Could not send the payment-failure notice for %s",
                      payment.reference)


def _announce(cfg, repo, payment, plan) -> None:
    """Record the in-app notification and send the receipt. Best-effort.

    Both are side effects of a payment that is already committed, so neither is
    allowed to fail the transaction that produced them: the money moved and the
    subscription is live whatever happens to an email.
    """
    subscription = repo.active_subscription(payment.user_id)
    expires = getattr(subscription, "expires_at", None)
    try:
        repo.notify(
            payment.user_id, kind="payment_success",
            title=f"{plan.name} subscription active",
            body=f"Payment of {format_minor(payment.amount_minor, payment.currency)}"
                 f" received. Reference {payment.reference}.",
            link="/subscription/payments")
    except Exception:  # pragma: no cover - a notification must not lose a payment
        log.exception("Could not record a notification for payment %s",
                      payment.reference)

    user = repo.get_user(payment.user_id)
    if user is None:
        return
    try:
        from notifications import email as mail

        from .display import ny_str

        mail.send_payment_received(
            user,
            plan_name=plan.name,
            amount=format_minor(payment.amount_minor, payment.currency),
            reference=payment.reference,
            paid_at=ny_str(payment.paid_at or payment.updated_at)
            if (payment.paid_at or payment.updated_at) else "")
        mail.send_subscription_activated(
            user, plan_name=plan.name,
            expires_at=ny_str(expires) if expires else "")
    except Exception:  # pragma: no cover - email is never allowed to break this
        log.exception("Could not send the receipt for payment %s",
                      payment.reference)


def _resolve(verification_reference: str, user):
    """The signed-in user's payment row for a reference, or ``None``.

    Scoped to the owner on purpose. A reference that belongs to someone else
    resolves to nothing here, so a customer cannot drive another account's
    payment through the return leg by pasting its reference.
    """
    repo = g.repo
    payment = repo.get_payment_by_reference(verification_reference)
    if payment is None or payment.user_id != user.id:
        return None
    return payment


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@subscription_bp.get("/subscription")
@login_required
def index():
    """What the account holds, and what it could buy."""
    user, repo, cfg = current_user(), g.repo, _cfg()
    entitlement = entitlement_for(user, repo)
    return render_template(
        "subscription/index.html",
        nav="subscription",
        title="Subscription",
        entitlement=entitlement,
        plans=repo.list_plans(active_only=True),
        current=repo.active_subscription(user.id),
        history=repo.subscription_history(user.id)[:10],
        payments=repo.list_payments(user_id=user.id, limit=10),
        available=_payments_available(cfg),
        period_days=_period_days(cfg),
        format_minor=format_minor,
    )


@subscription_bp.get("/subscription/payments")
@login_required
def payments():
    """Every payment attempt on this account, successful or not."""
    user, repo = current_user(), g.repo
    return render_template(
        "subscription/payments.html",
        nav="subscription",
        title="Payment history",
        entitlement=entitlement_for(user, repo),
        rows=repo.list_payments(user_id=user.id, limit=200),
        format_minor=format_minor,
    )


# --------------------------------------------------------------------------- #
# Checkout
# --------------------------------------------------------------------------- #
@subscription_bp.post("/subscription/checkout/<plan_key>")
@login_required
def checkout(plan_key: str):
    """Start a purchase: record the attempt, then send the customer to pay.

    The plan key is the *only* thing read from the request, and it is validated
    against the catalogue. The price comes from :mod:`app.plans` on the server,
    so a form field claiming a different amount has nowhere to be read from.
    """
    if (denied := require_csrf()) is not None:
        return denied

    user, repo, cfg = current_user(), g.repo, _cfg()

    spec = get_plan(plan_key)
    if spec is None:
        abort(404)

    plan = _plan_row(repo, spec.key)
    if plan is None or not plan.is_active:
        flash("That plan is not available at the moment.", "warning")
        return redirect(url_for("subscription.index"))

    provider = client_for(cfg)
    if not provider.configured:
        # Said out loud rather than swallowed: a customer who clicked "subscribe"
        # deserves to know the difference between "your card failed" and "this
        # deployment cannot take cards".
        flash("Card payments are not configured on this deployment yet. "
              "Please contact support.", "warning")
        return redirect(url_for("subscription.index"))

    reference = _new_reference(user.id)
    payment = repo.create_payment(
        user_id=user.id, plan=plan, reference=reference,
        amount_minor=plan.price_minor, currency=plan.currency,
        provider=PROVIDER)

    try:
        link = provider.initialize(
            reference=reference,
            amount_minor=plan.price_minor,
            currency=plan.currency,
            customer_email=user.email or "",
            customer_name=user.display_name or user.username,
            customer_phone=getattr(user, "phone", "") or "",
            title=f"3rader {plan.name}",
            description=f"{plan.name} subscription, {_period_days(cfg)} days",
            redirect_url=url_for("subscription.callback", _external=True),
            # Ours, and echoed back in the webhook. Deliberately just the ids:
            # nothing here needs to carry anything sensitive.
            meta={"user_id": user.id, "plan_key": plan.key,
                  "payment_reference": reference},
        )
    except PaymentError as exc:
        # ``exc`` is written to be customer-safe; the detail is already logged in
        # app.payments.
        repo.fail_payment(reference, str(exc))
        flash(str(exc), "warning")
        return redirect(url_for("subscription.index"))

    repo.log_event("INFO", "payments",
                   f"Checkout started for {user.username} on {plan.key} "
                   f"({reference})")
    return redirect(link)


@subscription_bp.get("/subscription/callback")
@login_required
def callback():
    """Where Flutterwave sends the browser back to.

    Nothing in the query string is believed. Its only job is to name a payment
    reference; the answer to "did this succeed" comes from the provider's API,
    server to server, and the webhook will reach the same conclusion if this leg
    never runs at all.
    """
    user, repo, cfg = current_user(), g.repo, _cfg()

    reference = (request.args.get("tx_ref")
                 or request.args.get("reference") or "").strip()
    if not reference:
        return redirect(url_for("subscription.index"))

    payment = _resolve(reference, user)
    if payment is None:
        # Either not ours or not real. The same answer for both: a distinct
        # response would confirm which references exist.
        log.warning("Callback for unknown or foreign reference %r by user %s",
                    reference, user.id)
        abort(404)

    if payment.status == "successful":
        flash("That payment has already been confirmed. Your subscription is "
              "active.", "success")
        return redirect(url_for("subscription.index"))

    verification = client_for(cfg).verify(reference)
    outcome = _finalise(cfg, repo, payment, verification)

    messages = {
        "settled": ("success", "Payment confirmed. Your subscription is now "
                               "active."),
        "replayed": ("info", "That payment was already confirmed."),
        "mismatch": ("danger", "The payment did not match the plan price, so "
                               "nothing was activated. You have not been "
                               "charged for a subscription. Please contact "
                               "support with your reference."),
        "not_paid": ("warning", verification.message or
                     "The payment was not completed."),
        "no_plan": ("danger", "Your payment was received but the plan could not "
                              "be activated. Please contact support."),
        "unknown": ("danger", "We could not match that payment to your account."),
    }
    tone, text = messages.get(outcome, ("warning", "The payment could not be "
                                                   "confirmed."))
    flash(text, tone)
    return redirect(url_for("subscription.index"))


@subscription_bp.post("/subscription/webhook")
def webhook():
    """Flutterwave's server-to-server notification.

    Authenticated by signature, not by CSRF: the caller is Flutterwave, which has
    no session and no token. That makes ``verif-hash`` the *only* thing standing
    between this endpoint and a forged subscription, so it is checked first and
    a request that fails it is refused before the body is parsed.

    Always answers 200 once the signature is good, including for an event this
    deployment does not act on — providers retry anything else, and a duplicate
    is not an error here because settlement is idempotent.
    """
    cfg, repo = _cfg(), g.repo
    provider = client_for(cfg)

    supplied = request.headers.get("verif-hash")
    if not provider.webhook_signature_ok(supplied):
        log.warning("Flutterwave webhook rejected: bad or missing verif-hash "
                    "from %s", request.remote_addr)
        return jsonify({"ok": False, "reason": "invalid_signature"}), 401

    payload = request.get_json(silent=True) or {}
    data = payload.get("data") or {}
    reference = str(data.get("tx_ref") or "").strip()
    if not reference:
        return jsonify({"ok": False, "reason": "no_reference"}), 400

    payment = repo.get_payment_by_reference(reference)
    if payment is None:
        log.warning("Flutterwave webhook for unknown reference %r", reference)
        return jsonify({"ok": False, "reason": "unknown_reference"}), 404

    # The webhook body is a claim, exactly like the browser's query string. It
    # names the transaction; the API is asked whether it succeeded.
    verification = provider.verify(reference)
    outcome = _finalise(cfg, repo, payment, verification)
    log.info("Flutterwave webhook for %s resolved as %s", reference, outcome)
    return jsonify({"ok": True, "outcome": outcome}), 200


# --------------------------------------------------------------------------- #
# Managing a live subscription
# --------------------------------------------------------------------------- #
@subscription_bp.post("/subscription/cancel")
@login_required
def cancel():
    """Stop the account's current subscription.

    Only the signed-in user's own subscription can be reached, and only through
    their live row — the id is never taken from the request. The records are kept:
    a subscription that has been paid for is financial history, so cancelling
    ends access and leaves the audit trail intact.
    """
    if (denied := require_csrf()) is not None:
        return denied

    user, repo = current_user(), g.repo
    current = repo.active_subscription(user.id)
    if current is None:
        flash("You do not have an active subscription to cancel.", "info")
        return redirect(url_for("subscription.index"))

    repo.cancel_subscription(current.id)
    repo.log_event("INFO", "payments",
                   f"{user.username} cancelled subscription {current.id}")
    repo.notify(user.id, kind="subscription_changed",
                title="Subscription cancelled",
                body="Your subscription has been cancelled and will not "
                     "continue.",
                link="/subscription")
    try:
        from notifications import email as mail

        spec = get_plan(current.plan_key)
        mail.send_subscription_changed(
            user, plan_name=spec.name if spec else current.plan_key,
            change="Cancelled at your request", expires_at="")
    except Exception:  # pragma: no cover
        log.exception("Could not send the cancellation notice")

    flash("Your subscription has been cancelled.", "success")
    return redirect(url_for("subscription.index"))


def register_subscription(app) -> None:
    app.register_blueprint(subscription_bp)
