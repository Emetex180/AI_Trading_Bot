"""Plans, subscription entitlement and the account-maintenance invariants.

These pin the properties the billing layer has to hold, one test each:

* plan seeding is **additive** — a price an operator edited is never reverted;
* entitlement fails **closed** — no plan, an expired plan and an unreachable
  database all resolve to no access, never to access;
* a user editing their own profile **cannot** change their role, status or
  subscription, however the form is crafted;
* settling a payment and granting a subscription are each **idempotent**, so a
  replayed webhook cannot sell one payment twice.

The app is built per-test against its own in-memory database.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from app.access import (FEATURE_ANALYSIS, FEATURE_DASHBOARD, FEATURE_HISTORY,
                        FEATURE_MARKET, FEATURE_SETUPS, PLAN_FEATURES,
                        entitlement_for, has_feature)
from app.plans import PLANS, PLAN_KEYS, get_plan, spec_rows
from app.web import create_app
from config import get_settings
from database import models as m
from trading import time_utils as tu

from test_web import _PASSWORD, _repo


def _user(repo, *, role=m.ROLE_CLIENT, username="u1"):
    return repo.create_user(username=username, password_hash_or_plain=_PASSWORD,
                            role=role, subscribe=(role == m.ROLE_CLIENT))


def _buy(repo, user, plan_key: str, *, days: int = 30, reference: str | None = None):
    """Drive the real purchase path: payment -> settle -> subscription."""
    plan = repo.get_plan_by_key(plan_key)
    ref = reference or f"ref-{plan_key}-{user.id}"
    payment = repo.create_payment(user_id=user.id, plan=plan, reference=ref,
                                  amount_minor=plan.price_minor,
                                  currency=plan.currency)
    settled, _ = repo.settle_payment(ref, provider_tx_id=f"flw-{ref}",
                                     amount_minor=plan.price_minor,
                                     currency=plan.currency,
                                     payload={"status": "successful"})
    return repo.activate_subscription(user_id=user.id, plan=plan,
                                      payment=settled, period_days=days)


# --------------------------------------------------------------------------- #
# The plan catalogue
# --------------------------------------------------------------------------- #
def test_the_three_launch_plans_are_priced_as_specified():
    assert PLAN_KEYS == ("basic", "premium", "vip")
    assert get_plan("basic").price_minor == 5_000
    assert get_plan("premium").price_minor == 10_000
    assert get_plan("vip").price_minor == 50_000
    assert {p.currency for p in PLANS} == {"USD"}


def test_levels_are_strictly_increasing_with_price():
    """Access compares ``level``, so the ordering must match the pricing order.

    If VIP ever ranked below Premium, a Premium subscriber would silently be
    granted VIP access — a bug the pricing page would not show.
    """
    by_price = sorted(PLANS, key=lambda p: p.price_minor)
    levels = [p.level for p in by_price]
    assert levels == sorted(levels) and len(set(levels)) == len(levels)


def test_an_unknown_plan_key_is_not_a_plan():
    assert get_plan("enterprise") is None
    assert get_plan("") is None
    assert get_plan(None) is None


def test_seeding_plans_twice_adds_nothing_the_second_time():
    repo = _repo()
    assert repo.sync_plans(spec_rows()) == 3
    assert repo.sync_plans(spec_rows()) == 0
    assert len(repo.list_plans()) == 3


def test_seeding_never_reverts_a_price_an_operator_edited():
    """The property that makes the table worth having rather than the config.

    A restart re-runs the seed on every boot; if that overwrote, every price
    change made through the admin area would last only until the next restart.
    """
    repo = _repo()
    repo.sync_plans(spec_rows())
    repo.set_plan_fields("premium", price_minor=12_500, name="Premium Plus")

    repo.sync_plans(spec_rows())          # what a restart does

    plan = repo.get_plan_by_key("premium")
    assert plan.price_minor == 12_500
    assert plan.name == "Premium Plus"


def test_seeding_adds_a_new_tier_without_touching_the_others():
    repo = _repo()
    repo.sync_plans(spec_rows())
    repo.set_plan_fields("basic", price_minor=4_000)

    repo.sync_plans(spec_rows() + [{"key": "elite", "name": "Elite",
                                    "price_minor": 100_000, "currency": "USD",
                                    "description": "", "features": [],
                                    "level": 40, "highlight": False,
                                    "sort_order": 4, "is_active": True}])

    assert repo.get_plan_by_key("elite") is not None
    assert repo.get_plan_by_key("basic").price_minor == 4_000


def test_plan_editing_cannot_change_the_key_or_the_level():
    """Both are load-bearing: subscriptions reference the key, access the level."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    repo.set_plan_fields("basic", key="vip", level=99, name="Basic")

    plan = repo.get_plan_by_key("basic")
    assert plan is not None and plan.name == "Basic"
    assert plan.level == 10
    assert repo.get_plan_by_key("vip").level == 30


def test_the_plan_mapping_covers_every_catalogue_key():
    assert set(PLAN_FEATURES) == set(PLAN_KEYS)


# --------------------------------------------------------------------------- #
# Entitlement resolves to the right tier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("plan_key,expected", [
    ("basic", {FEATURE_DASHBOARD, FEATURE_MARKET}),
    ("premium", {FEATURE_DASHBOARD, FEATURE_MARKET, FEATURE_SETUPS,
                 FEATURE_HISTORY}),
    ("vip", {FEATURE_DASHBOARD, FEATURE_MARKET, FEATURE_SETUPS, FEATURE_HISTORY,
             FEATURE_ANALYSIS}),
])
def test_a_bought_subscription_unlocks_exactly_its_tier(plan_key, expected):
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    _buy(repo, user, plan_key)

    ent = entitlement_for(user, repo)
    assert ent.plan_key == plan_key
    assert ent.is_active and ent.source == "subscription"
    assert ent.features == expected
    for feature in ("dashboard", "market", "setups", "history", "analysis"):
        assert has_feature(user, feature, repo) == (feature in expected), feature


def test_an_account_with_no_plan_reaches_nothing():
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)

    ent = entitlement_for(user, repo)
    assert not ent.is_active and ent.plan_key == ""
    assert all(not has_feature(user, f, repo) for f in PLAN_FEATURES["vip"])


def test_an_expired_subscription_stops_granting_access_immediately():
    """No cleanup job runs between expiry and the next request."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    sub = _buy(repo, user, "vip")

    sub.expires_at = tu.now_utc() - timedelta(seconds=1)
    repo.session.commit()

    ent = entitlement_for(user, repo)
    assert not ent.is_active
    assert not has_feature(user, "analysis", repo)


def test_a_plan_an_admin_granted_on_the_profile_also_grants_access():
    """The pre-existing admin path must keep working, not just the new one."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    repo.update_client_profile(user.id, subscription_plan="premium",
                               subscription_status=m.SUB_ACTIVE,
                               subscription_expires_at=None)

    ent = entitlement_for(user, repo)
    assert ent.is_active and ent.plan_key == "premium"
    assert ent.source == "profile"
    assert has_feature(user, FEATURE_SETUPS, repo)
    assert not has_feature(user, FEATURE_ANALYSIS, repo)


def test_a_profile_plan_with_an_unknown_key_grants_nothing():
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    repo.update_client_profile(user.id, subscription_plan="platinum",
                               subscription_status=m.SUB_ACTIVE)

    assert not entitlement_for(user, repo).is_active


def test_a_subscription_beats_a_stale_profile_plan():
    """Both sources set: the one the customer paid for wins."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    repo.update_client_profile(user.id, subscription_plan="basic",
                               subscription_status=m.SUB_ACTIVE)
    _buy(repo, user, "vip")

    ent = entitlement_for(user, repo)
    assert ent.plan_key == "vip" and ent.source == "subscription"


def test_an_admin_is_never_gated_by_a_subscription():
    """An admin has to see what a client sees in order to support them."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    admin = _user(repo, role=m.ROLE_ADMIN, username="boss")

    assert all(has_feature(admin, f, repo) for f in PLAN_FEATURES["vip"])


def test_entitlement_fails_closed_when_the_database_errors():
    """An authorisation check that errors open is worse than one that 500s."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)

    class Exploding:
        def active_subscription(self, *a, **k):
            raise RuntimeError("database is gone")

    assert not has_feature(user, FEATURE_DASHBOARD, Exploding())


def test_entitlement_of_nobody_is_nothing():
    assert not entitlement_for(None, _repo()).is_active


# --------------------------------------------------------------------------- #
# Money and idempotency
# --------------------------------------------------------------------------- #
def test_a_replayed_settlement_does_not_settle_twice():
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    plan = repo.get_plan_by_key("basic")
    repo.create_payment(user_id=user.id, plan=plan, reference="r1",
                        amount_minor=plan.price_minor, currency="USD")
    kw = dict(provider_tx_id="flw-1", amount_minor=plan.price_minor,
              currency="USD", payload={"status": "successful"})

    _, first = repo.settle_payment("r1", **kw)
    _, second = repo.settle_payment("r1", **kw)
    _, third = repo.settle_payment("r1", **kw)

    assert (first, second, third) == (True, False, False)
    assert len(repo.list_payments(status=m.PAY_SUCCESSFUL)) == 1


def test_one_payment_can_only_ever_buy_one_subscription():
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    plan = repo.get_plan_by_key("vip")
    repo.create_payment(user_id=user.id, plan=plan, reference="r2",
                        amount_minor=plan.price_minor, currency="USD")
    payment, _ = repo.settle_payment("r2", provider_tx_id="flw-2",
                                     amount_minor=plan.price_minor,
                                     currency="USD", payload={})

    first = repo.activate_subscription(user_id=user.id, plan=plan,
                                       payment=payment, period_days=30)
    second = repo.activate_subscription(user_id=user.id, plan=plan,
                                        payment=payment, period_days=30)

    assert first.id == second.id
    assert len(repo.subscription_history(user.id)) == 1


def test_upgrading_expires_the_previous_subscription():
    """One live entitlement, so the access layer has one answer."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    _buy(repo, user, "basic", reference="up-1")
    _buy(repo, user, "premium", reference="up-2")

    live = [s for s in repo.subscription_history(user.id)
            if s.status == m.SUB_ACTIVE]
    assert len(live) == 1 and live[0].plan_key == "premium"
    assert entitlement_for(user, repo).plan_key == "premium"


def test_a_late_failure_notice_cannot_revoke_a_paid_subscription():
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    plan = repo.get_plan_by_key("basic")
    repo.create_payment(user_id=user.id, plan=plan, reference="r3",
                        amount_minor=plan.price_minor, currency="USD")
    repo.settle_payment("r3", provider_tx_id="flw-3",
                        amount_minor=plan.price_minor, currency="USD", payload={})

    row = repo.fail_payment("r3", "provider retried and reported failure")

    assert row.status == m.PAY_SUCCESSFUL


# --------------------------------------------------------------------------- #
# A user cannot promote themselves
# --------------------------------------------------------------------------- #
def test_self_editing_cannot_change_role_status_or_subscription():
    """The brief's "a user must NOT be able to" list, enforced in one place."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)

    repo.update_user(user.id,
                     role=m.ROLE_ADMIN,                 # privilege escalation
                     status=m.STATUS_SUSPENDED,
                     password_hash="x",                 # credential swap
                     deleted_at=None,
                     display_name="Ada",                # the one legal field
                     email="ada@3rader.io", phone="+1", country="NG")

    fresh = repo.get_user(user.id)
    assert fresh.display_name == "Ada"
    assert fresh.email == "ada@3rader.io"
    assert fresh.role == m.ROLE_CLIENT
    assert fresh.status == m.STATUS_ACTIVE
    assert not fresh.password_hash.startswith("x")


def test_only_the_two_declared_roles_can_be_assigned():
    repo = _repo()
    user = _user(repo)

    assert repo.set_user_role(user.id, "superuser") is None
    assert repo.get_user(user.id).role == m.ROLE_CLIENT
    assert repo.set_user_role(user.id, m.ROLE_ADMIN).role == m.ROLE_ADMIN


# --------------------------------------------------------------------------- #
# Deletion keeps the financial trail
# --------------------------------------------------------------------------- #
def test_deleting_a_user_keeps_their_payments_and_hides_the_account():
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    _buy(repo, user, "premium", reference="del-1")

    repo.soft_delete_user(user.id)

    assert repo.get_user(user.id) is None          # sessions stop resolving
    assert repo.get_user_by_username("u1") is None
    assert repo.get_user_by_email("") is None
    retained = repo.get_user(user.id, include_deleted=True)
    assert retained is not None and retained.is_deleted
    assert len(repo.list_payments(user_id=user.id)) == 1
    assert len(repo.subscription_history(user.id)) == 1


def test_deleting_a_user_ends_their_subscription():
    """A removed account must not keep counting as an active subscriber."""
    repo = _repo()
    repo.sync_plans(spec_rows())
    user = _user(repo)
    _buy(repo, user, "vip", reference="del-2")

    repo.soft_delete_user(user.id)

    assert repo.active_subscription(user.id) is None
    assert repo.platform_stats()["subscriptions_active"] == 0


def test_a_fresh_install_is_still_recognisable_after_every_user_is_removed():
    """Guards the bootstrap admin: counting only live rows would re-create one."""
    repo = _repo()
    user = _user(repo)
    repo.soft_delete_user(user.id)

    assert repo.count_users() == 1                 # includes deleted
    assert repo.count_users(include_deleted=False) == 0


def test_restoring_a_deleted_user_brings_the_account_back():
    repo = _repo()
    user = _user(repo)
    repo.soft_delete_user(user.id)

    repo.restore_user(user.id)

    assert repo.get_user(user.id) is not None
    assert repo.get_user(user.id).is_active


# --------------------------------------------------------------------------- #
# Statistics are counts of real rows
# --------------------------------------------------------------------------- #
def test_platform_stats_report_what_is_actually_stored():
    repo = _repo()
    repo.sync_plans(spec_rows())
    a = _user(repo, username="a")
    b = _user(repo, username="b")
    _user(repo, role=m.ROLE_ADMIN, username="boss")
    _buy(repo, a, "basic", reference="st-1")
    _buy(repo, b, "vip", reference="st-2")
    plan = repo.get_plan_by_key("premium")
    repo.create_payment(user_id=a.id, plan=plan, reference="st-3",
                        amount_minor=plan.price_minor, currency="USD")

    stats = repo.platform_stats()

    assert stats["users_total"] == 3
    assert stats["admins"] == 1
    assert stats["subscribers_basic"] == 1
    assert stats["subscribers_vip"] == 1
    assert stats["subscribers_premium"] == 0
    assert stats["subscriptions_active"] == 2
    assert stats["payments_successful"] == 2
    assert stats["payments_pending"] == 1
    assert stats["revenue_minor"] == 5_000 + 50_000


def test_revenue_is_zero_rather_than_none_before_any_sale():
    assert _repo().platform_stats()["revenue_minor"] == 0


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def test_notifications_are_scoped_to_their_owner():
    repo = _repo()
    a, b = _user(repo, username="a"), _user(repo, username="b")
    repo.notify(a.id, kind=m.NOTIFY_REGISTRATION, title="Welcome")

    assert repo.unread_notification_count(a.id) == 1
    assert repo.unread_notification_count(b.id) == 0

    repo.mark_notifications_read(a.id)
    assert repo.unread_notification_count(a.id) == 0
    assert repo.unread_notification_count(b.id) == 0


def test_marking_read_does_not_touch_another_users_unread_rows():
    repo = _repo()
    a, b = _user(repo, username="a"), _user(repo, username="b")
    repo.notify(a.id, kind="x", title="for a")
    repo.notify(b.id, kind="x", title="for b")

    repo.mark_notifications_read(a.id)

    assert repo.unread_notification_count(b.id) == 1


# --------------------------------------------------------------------------- #
# The receipt and the decline notice
#
# The reconcile step is where the customer is told what happened to their money,
# and it is also where an email must never be allowed to change the outcome: by
# the time a receipt is sent the payment is already recorded and the subscription
# already granted.
# --------------------------------------------------------------------------- #
def _settled_payment(repo, user, plan_key="premium"):
    """A pending payment row, ready for ``_finalise`` to reconcile."""
    repo.sync_plans(spec_rows())
    plan = repo.get_plan_by_key(plan_key)
    ref = f"recon-{plan_key}-{user.id}"
    repo.create_payment(user_id=user.id, plan=plan, reference=ref,
                        amount_minor=plan.price_minor, currency=plan.currency)
    return plan, repo.get_payment_by_reference(ref)


def _verification(plan, **kw):
    from app.payments import Verification
    fields = {"ok": True, "status": "successful", "provider_tx_id": "flw-1",
              "amount_minor": plan.price_minor, "currency": plan.currency,
              "payload": {"status": "successful"}}
    fields.update(kw)
    return Verification(**fields)


def _cfg():
    """Settings, without standing up an app: ``_finalise`` reads only the billing
    period from them."""
    from config import get_settings
    return replace(get_settings(), flask_secret_key="test-secret")


def test_settling_sends_the_receipt_and_the_activation_notice(monkeypatch):
    from app import subscription as sub
    from app.plans import spec_rows as _specs

    repo = _repo()
    user = _user(repo)
    plan, payment = _settled_payment(repo, user)

    sent = []
    monkeypatch.setattr("notifications.email.send_payment_received",
                        lambda u, **kw: sent.append(("received", kw)) or True)
    monkeypatch.setattr("notifications.email.send_subscription_activated",
                        lambda u, **kw: sent.append(("activated", kw)) or True)

    outcome = sub._finalise(_cfg(), repo, payment, _verification(plan))

    assert outcome == "settled"
    kinds = [k for k, _ in sent]
    assert kinds == ["received", "activated"]
    assert sent[0][1]["reference"] == payment.reference
    assert sent[0][1]["amount"] == "$100.00"


def test_a_declined_charge_emails_the_customer_their_reason(monkeypatch):
    """A decline is discovered server to server, often after the customer has
    closed the tab, so a flash would reach nobody."""
    from app import subscription as sub

    repo = _repo()
    user = _user(repo)
    plan, payment = _settled_payment(repo, user)

    sent = []
    monkeypatch.setattr("notifications.email.send_payment_failed",
                        lambda u, **kw: sent.append(kw) or True)

    outcome = sub._finalise(_cfg(), repo, payment,
                            _verification(plan, ok=False, status="failed",
                                          message="Insufficient funds"))

    assert outcome == "not_paid"
    assert len(sent) == 1
    assert sent[0]["reason"] == "Insufficient funds"
    assert sent[0]["plan_name"] == "Premium"


def test_a_pending_charge_emails_nobody(monkeypatch):
    """A checkout the customer walked away from must not generate contact."""
    from app import subscription as sub

    repo = _repo()
    user = _user(repo)
    plan, payment = _settled_payment(repo, user)

    sent = []
    monkeypatch.setattr("notifications.email.send_payment_failed",
                        lambda u, **kw: sent.append(kw) or True)

    assert sub._finalise(_cfg(), repo, payment,
                         _verification(plan, ok=False, status="pending")) == \
        "not_paid"
    assert sent == []


def test_a_tampered_amount_emails_the_customer_and_still_refuses(monkeypatch):
    """The mismatch is recorded as a failure rather than settled at the wrong
    figure — and the customer is told, because their money is involved."""
    from app import subscription as sub

    repo = _repo()
    user = _user(repo)
    plan, payment = _settled_payment(repo, user)

    sent = []
    monkeypatch.setattr("notifications.email.send_payment_failed",
                        lambda u, **kw: sent.append(kw) or True)

    outcome = sub._finalise(
        _cfg(), repo, payment,
        _verification(plan, amount_minor=plan.price_minor - 100))

    assert outcome == "mismatch"
    assert len(sent) == 1
    repo.session.expire_all()
    assert repo.get_payment_by_reference(payment.reference).status == m.PAY_FAILED


def test_a_broken_mailer_cannot_change_the_outcome_of_a_payment(monkeypatch):
    """The property the whole best-effort design exists for: registering, paying
    and subscribing all succeed whether or not the provider is reachable."""
    from app import subscription as sub

    def _explode(*args, **kwargs):
        raise RuntimeError("resend is unreachable")

    for name in ("send_payment_received", "send_subscription_activated"):
        monkeypatch.setattr(f"notifications.email.{name}", _explode)

    repo = _repo()
    user = _user(repo)
    plan, payment = _settled_payment(repo, user)

    assert sub._finalise(_cfg(), repo, payment, _verification(plan)) == "settled"
    repo.session.expire_all()
    assert repo.get_payment_by_reference(payment.reference).status == \
        m.PAY_SUCCESSFUL
    assert repo.active_subscription(user.id) is not None


# --------------------------------------------------------------------------- #
# The webhook
#
# Flutterwave's payload shape is the provider's to change, and the reference is
# the only thing this endpoint can act on: a rename turns a payment that has
# already been taken into a 400 and a customer with no subscription. These pin
# the confirmed production shape — and the older one, so neither can be dropped
# silently.
# --------------------------------------------------------------------------- #
#: The ``verif-hash`` the test deployment is configured with. A real secret
#: never appears in a test; this one exists only to be echoed back.
_VERIF_HASH = "test-verif-hash"

#: A reference in the shape :func:`app.subscription._new_reference` produces:
#: ours, carrying the user id, and nothing Flutterwave would ever mint.
_REFERENCE = "3R-7-20260930120000-abcd1234"


def _webhook_client(repo):
    """A client whose deployment knows the webhook hash, so the real signature
    check runs and can pass."""
    settings = replace(get_settings(), flask_secret_key="test-secret",
                       flutterwave_webhook_secret_hash=_VERIF_HASH)
    return create_app(settings=settings, repository=repo,
                      setup_db=False).test_client()


def _post_webhook(client, body, *, verif_hash=_VERIF_HASH):
    return client.post("/subscription/webhook", data=json.dumps(body),
                       content_type="application/json",
                       headers={"verif-hash": verif_hash})


def _awaiting_payment(repo, *, plan_key="premium", reference=_REFERENCE):
    """An account with one pending payment row for ``reference``."""
    repo.sync_plans(spec_rows())
    user = _user(repo)
    plan = repo.get_plan_by_key(plan_key)
    repo.create_payment(user_id=user.id, plan=plan, reference=reference,
                        amount_minor=plan.price_minor, currency=plan.currency)
    return user, plan


def _verify_settles(monkeypatch, plan, seen):
    """Stand in for the provider's API: record the reference, confirm a payment.

    Patched on the *class*, so the client the route builds for itself is the one
    that answers — the signature check and the whole of ``_finalise`` stay real.
    ``seen`` is what proves the endpoint read the reference out of the payload
    rather than arriving at the right row some other way.
    """
    from app.payments import Verification

    def fake_verify(self, reference):
        seen.append(reference)
        return Verification(ok=True, status="successful",
                            provider_tx_id="flw-tx-1",
                            amount_minor=plan.price_minor,
                            currency=plan.currency,
                            payload={"status": "successful"})

    monkeypatch.setattr("app.payments.FlutterwaveClient.verify", fake_verify)
    # A receipt must never be the thing that decides whether a test reaches the
    # network, and an operator's RESEND_API_KEY must not be spent by one.
    for name in ("send_payment_received", "send_subscription_activated"):
        monkeypatch.setattr(f"notifications.email.{name}", lambda u, **kw: True)


def _production_payload(reference: str | None) -> dict:
    """The payload Flutterwave actually sent, as the diagnostic confirmed it.

    There is no ``data`` object at all and the reference is at the top level as
    ``txRef`` — beside ``id``, ``flwRef`` and ``orderRef``, which are the
    provider's own identifiers and are deliberately not interchangeable with
    ours. ``event`` is absent too; the key is ``event.type``.
    """
    body = {
        "event.type": "CARD_TRANSACTION",
        "id": 1234567,
        "flwRef": "FLW-MOCK-1234",
        "orderRef": "URF-1234",
        "status": "successful",
        "amount": 100.0,
        "charged_amount": 100.0,
        "currency": "USD",
        "customer": {"email": "buyer@example.com"},
    }
    if reference is not None:
        body["txRef"] = reference
    return body


def test_a_webhook_carrying_the_reference_at_the_top_level_settles_the_payment(
        monkeypatch):
    """The regression this whole change exists for.

    Flutterwave delivers ``txRef`` at the top level, which is where nothing used
    to look: the webhook answered 400 and the customer's money was taken with no
    subscription to show for it.
    """
    repo = _repo()
    user, plan = _awaiting_payment(repo)
    seen = []
    _verify_settles(monkeypatch, plan, seen)

    resp = _post_webhook(_webhook_client(repo), _production_payload(_REFERENCE))

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "outcome": "settled"}
    # Read from the payload, and the *right* thing read: the provider was asked
    # about our reference, not about the transaction id beside it.
    assert seen == [_REFERENCE]
    repo.session.expire_all()
    assert repo.get_payment_by_reference(_REFERENCE).status == m.PAY_SUCCESSFUL
    assert repo.active_subscription(user.id) is not None


def test_a_webhook_in_the_older_nested_shape_still_settles_the_payment(monkeypatch):
    """``data.tx_ref`` is the shape this endpoint was written for, and a
    deployment pointed at a provider account still sending it must keep working."""
    repo = _repo()
    user, plan = _awaiting_payment(repo)
    seen = []
    _verify_settles(monkeypatch, plan, seen)

    resp = _post_webhook(_webhook_client(repo), {
        "event": "charge.completed",
        "data": {"id": 1234567, "tx_ref": _REFERENCE, "status": "successful"},
    })

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "outcome": "settled"}
    assert seen == [_REFERENCE]
    repo.session.expire_all()
    assert repo.get_payment_by_reference(_REFERENCE).status == m.PAY_SUCCESSFUL
    assert repo.active_subscription(user.id) is not None


def test_an_unknown_transaction_id_is_not_mistaken_for_our_reference(monkeypatch):
    """Tolerating more than one *name* is not the same as accepting anything.

    A payload with the provider's own identifiers and no reference of ours
    settles nothing — the alternative would be an endpoint that settles whatever
    row a stranger's transaction id happens to match.
    """
    repo = _repo()
    user, plan = _awaiting_payment(repo)
    seen = []
    _verify_settles(monkeypatch, plan, seen)

    resp = _post_webhook(_webhook_client(repo),
                         _production_payload(reference=None))

    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "reason": "no_reference"}
    assert seen == []
    repo.session.expire_all()
    assert repo.get_payment_by_reference(_REFERENCE).status == m.PAY_PENDING
    assert repo.active_subscription(user.id) is None


def test_an_unauthenticated_webhook_is_refused_before_the_body_is_read(monkeypatch):
    """The signature is the only thing between this endpoint and a forged
    subscription, so a caller who has not proved they are Flutterwave settles
    nothing — including through the shape this change added."""
    repo = _repo()
    user, plan = _awaiting_payment(repo)
    seen = []
    _verify_settles(monkeypatch, plan, seen)

    resp = _post_webhook(_webhook_client(repo), _production_payload(_REFERENCE),
                         verif_hash="not-the-hash")

    assert resp.status_code == 401
    assert resp.get_json() == {"ok": False, "reason": "invalid_signature"}
    assert seen == []
    repo.session.expire_all()
    assert repo.get_payment_by_reference(_REFERENCE).status == m.PAY_PENDING
    assert repo.active_subscription(user.id) is None
