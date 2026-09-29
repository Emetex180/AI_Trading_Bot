"""Admin user management: search, edit, removal, and the billing views.

The tests here are about the guards rather than the rendering. Three properties
carry the weight:

**An administrator cannot delete themselves.** There is exactly one route into
this area, so an admin who removes their own account locks the deployment out of
its own administration, with no way back except the command line. The check is
on the session's user id, so it cannot be posted around by editing a form.

**Removal is soft, and the confirmation is typed.** A removal keeps the payment
and subscription rows — the records needed if a charge is disputed — and it
takes a typed username rather than a click, because deleting a person is not a
mis-click the interface should absorb quietly.

**The list shows the plan the account can actually use.** With two sources of
entitlement in the schema (a bought subscription, and a hand-granted plan on the
client's profile) it is easy to build a table that names a plan the client
cannot reach, which is the one way this page could actively mislead an operator.

Role *refusal* across the admin surface is asserted in ``test_auth.py``; this
file tests that a permitted call does the right thing.
"""
from __future__ import annotations

import re

from app.plans import format_minor, get_plan, spec_rows
from database import models as m

from test_admin_pages import _admin, _app, _body, _make_client, _post, _repo
from test_web import _CSRF, _PASSWORD, _grant


def _failed_payment(repo, user, reference="r-fail", reason="Insufficient funds"):
    """A payment that reached the provider and came back declined.

    The plan row is seeded first because ``create_payment`` takes the stored
    ``Plan`` (it needs ``plan_id``), not the code-level spec — the same order the
    real checkout goes through.
    """
    repo.sync_plans(spec_rows())
    plan = repo.get_plan_by_key("basic")
    repo.create_payment(user_id=user.id, plan=plan, reference=reference,
                        amount_minor=plan.price_minor, currency=plan.currency)
    return repo.fail_payment(reference, reason)


# --------------------------------------------------------------------------- #
# Search and filters
# --------------------------------------------------------------------------- #
def test_a_search_matches_username_name_and_email():
    repo = _repo()
    alice = _make_client(repo, "alice", display_name="Alice Anderson",
                         email="alice@example.com")
    bob = _make_client(repo, "bobby", display_name="Bob Brown",
                       email="bob@example.com")
    client = _admin(repo)

    for needle, expected, other in (("alice", alice, bob),
                                    ("Anderson", alice, bob),
                                    ("bob@example", bob, alice)):
        page = _body(client, f"/admin/clients?q={needle}")
        assert expected.username in page, needle
        assert other.username not in page, needle


def test_the_search_is_case_insensitive():
    repo = _repo()
    _make_client(repo, "alice", display_name="Alice Anderson")
    client = _admin(repo)
    assert "alice" in _body(client, "/admin/clients?q=ALICE")


def test_a_search_with_no_match_says_so():
    repo = _repo()
    _make_client(repo, "alice")
    client = _admin(repo)
    page = _body(client, "/admin/clients?q=nobodyhere")
    assert "alice" not in page
    assert "No account matches" in page
    # And offers the way back, rather than leaving a dead end.
    assert "Clear" in page


def test_the_plan_filter_uses_the_plan_the_client_can_actually_use():
    """A bought subscription wins over a hand-granted plan.

    ``alice``'s profile says ``basic`` and she has also bought ``premium``. The
    effective plan is premium, so filtering by premium must find her and
    filtering by basic must not — the list has to agree with what the guard on
    her own pages will decide.
    """
    repo = _repo()
    alice = _make_client(repo, "alice")
    repo.update_client_profile(alice.id, subscription_status=m.SUB_ACTIVE,
                               subscription_plan="basic")
    _grant(repo, alice, "premium")
    client = _admin(repo)

    assert "alice" in _body(client, "/admin/clients?plan=premium")
    assert "alice" not in _body(client, "/admin/clients?plan=basic")


def test_a_hand_granted_plan_is_found_by_its_own_plan_filter():
    """The other source of entitlement, so neither half of the resolution rule
    is the only one tested."""
    repo = _repo()
    alice = _make_client(repo, "alice")
    repo.update_client_profile(alice.id, subscription_status=m.SUB_ACTIVE,
                               subscription_plan="vip")
    client = _admin(repo)

    assert "alice" in _body(client, "/admin/clients?plan=vip")
    assert "alice" not in _body(client, "/admin/clients?plan=basic")


def test_the_status_filter_matches_the_account_not_the_subscription():
    """The dropdown offers account states, so the query must apply account states.

    This is the bug the test was written to catch: filtering on the *subscription*
    status made ``suspended`` match nothing at all, because it is not a
    subscription state, and made ``active`` mean "has a bought subscription"
    rather than "account is in good standing" — silently answering a different
    question than the label asks.
    """
    repo = _repo()
    _make_client(repo, "lively")
    held = _make_client(repo, "held")
    repo.set_user_status(held.id, m.STATUS_SUSPENDED)
    client = _admin(repo)

    active_page = _body(client, f"/admin/clients?status={m.STATUS_ACTIVE}")
    assert "lively" in active_page
    assert "/admin/clients/%d" % held.id not in active_page

    held_page = _body(client, f"/admin/clients?status={m.STATUS_SUSPENDED}")
    assert "held" in held_page
    assert "/admin/clients/%d" % repo.get_user_by_username("lively").id \
        not in held_page


def test_the_status_filter_is_not_defeated_by_a_subscription():
    """An account with a live subscription is still listable as suspended, and
    one with none is still listable as active — the two facets are independent."""
    repo = _repo()
    bought = _make_client(repo, "buyer")
    _grant(repo, bought, "premium")
    repo.set_user_status(bought.id, m.STATUS_SUSPENDED)
    plain = _make_client(repo, "plain")
    client = _admin(repo)

    assert "/admin/clients/%d" % bought.id in _body(
        client, f"/admin/clients?status={m.STATUS_SUSPENDED}")
    assert "/admin/clients/%d" % plain.id in _body(
        client, f"/admin/clients?status={m.STATUS_ACTIVE}")


def test_an_unrecognised_filter_is_ignored_rather_than_echoed():
    """Nonsense in the query string must not narrow the list or break the page.

    A filter value that reached the query unchecked would be a way to probe the
    database through the search box; unknown values are dropped instead.
    """
    repo = _repo()
    _make_client(repo, "alice")
    client = _admin(repo)

    for query in ("plan=' OR 1=1--", "role=superuser", "status=deleted",
                  "q=%00", "deleted=yes"):
        assert "alice" in _body(client, f"/admin/clients?{query}"), query


def test_the_admin_list_always_shows_every_administrator():
    """The account that can delete users must not be hideable from the list.

    The free-text filter applies to clients only, so an operator cannot narrow
    the page until the administrators disappear from it.
    """
    repo = _repo()
    client = _admin(repo)
    other = repo.create_user(username="second", password_hash_or_plain=_PASSWORD,
                             role=m.ROLE_ADMIN, email="second@example.com")

    page = _body(client, "/admin/clients?q=nobodyhere")
    assert other.username in page
    assert client.user.username in page


# --------------------------------------------------------------------------- #
# Editing a client
# --------------------------------------------------------------------------- #
def test_an_admin_can_edit_a_clients_details():
    repo = _repo()
    target = _make_client(repo, "alice")
    client = _admin(repo)

    _post(client, f"/admin/clients/{target.id}/details", display_name="Alice B",
          email="alice.b@example.com", phone="+44 7700 900000",
          country="United Kingdom", notes="Internal remark")

    repo.session.expire_all()
    row = repo.get_user(target.id)
    assert (row.display_name, row.email, row.phone, row.country) == (
        "Alice B", "alice.b@example.com", "+44 7700 900000", "United Kingdom")
    assert row.notes == "Internal remark"


def test_editing_cannot_change_a_role_status_or_username():
    """The escalation test, from the admin side: a crafted POST must not
    promote the target to admin, suspend them, or rename them."""
    repo = _repo()
    target = _make_client(repo, "alice")
    client = _admin(repo)

    _post(client, f"/admin/clients/{target.id}/details",
          email="alice@example.com", role=m.ROLE_ADMIN,
          status=m.STATUS_SUSPENDED, username="hacked",
          password_hash="not-a-hash", id="1", user_id="1")

    repo.session.expire_all()
    row = repo.get_user(target.id)
    assert row.role == m.ROLE_CLIENT
    assert row.status == m.STATUS_ACTIVE
    assert row.username == "alice"
    assert row.password_hash != "not-a-hash"


def test_editing_to_an_email_another_account_holds_is_refused():
    repo = _repo()
    alice = _make_client(repo, "alice", email="alice@example.com")
    _make_client(repo, "bobby", email="bob@example.com")
    client = _admin(repo)

    _post(client, f"/admin/clients/{alice.id}/details", email="bob@example.com")

    repo.session.expire_all()
    assert repo.get_user(alice.id).email == "alice@example.com"


def test_editing_an_account_to_the_email_it_already_has_is_fine():
    repo = _repo()
    alice = _make_client(repo, "alice", email="alice@example.com")
    client = _admin(repo)

    _post(client, f"/admin/clients/{alice.id}/details", email="alice@example.com",
          display_name="Still Alice")

    repo.session.expire_all()
    assert repo.get_user(alice.id).display_name == "Still Alice"


def test_editing_requires_a_valid_email():
    repo = _repo()
    alice = _make_client(repo, "alice", email="alice@example.com")
    client = _admin(repo)

    _post(client, f"/admin/clients/{alice.id}/details", email="not-an-email")

    repo.session.expire_all()
    assert repo.get_user(alice.id).email == "alice@example.com"


def test_an_edit_without_a_csrf_token_is_refused():
    repo = _repo()
    alice = _make_client(repo, "alice", email="alice@example.com")
    client = _admin(repo)

    client.post(f"/admin/clients/{alice.id}/details",
                data={"email": "evil@x.com"})

    repo.session.expire_all()
    assert repo.get_user(alice.id).email == "alice@example.com"


# --------------------------------------------------------------------------- #
# Removing and restoring
# --------------------------------------------------------------------------- #
def test_an_admin_cannot_remove_themselves():
    """Otherwise the deployment locks itself out of its own administration.

    The username is supplied correctly, so the refusal can only come from the
    self-check — not from the confirmation accidentally failing.
    """
    repo = _repo()
    client = _admin(repo)
    mine = client.user.id

    _post(client, f"/admin/clients/{mine}/delete", confirm=client.user.username)

    repo.session.expire_all()
    still = repo.get_user(mine)
    assert still is not None and not still.is_deleted
    assert "cannot delete the account you are signed in with" in _body(
        client, f"/admin/clients/{mine}")


def test_removal_needs_the_username_typed_exactly():
    """Case and content must match. Whitespace around a paste is trimmed, which
    is deliberate — the operator still had to type the exact username."""
    repo = _repo()
    target = _make_client(repo, "alice")
    client = _admin(repo)

    for wrong in ("bob", "ALICE", "Alice", "", "al", "alice2"):
        _post(client, f"/admin/clients/{target.id}/delete", confirm=wrong)
        repo.session.expire_all()
        assert repo.get_user(target.id) is not None, f"accepted {wrong!r}"

    # Whitespace is trimmed, which is the paste case and nothing more: the
    # operator still had to supply the exact name.
    _post(client, f"/admin/clients/{target.id}/delete", confirm="  alice  ")
    repo.session.expire_all()
    assert repo.get_user(target.id) is None, "a padded exact name was refused"


def test_a_confirmed_removal_hides_the_account_but_keeps_its_records():
    """The whole point of a soft delete: the account goes, the money stays."""
    repo = _repo()
    target = _make_client(repo, "alice")
    _grant(repo, target, "premium")
    client = _admin(repo)
    payments_before = len(repo.list_payments(user_id=target.id))
    assert payments_before == 1

    _post(client, f"/admin/clients/{target.id}/delete", confirm="alice")

    repo.session.expire_all()
    assert repo.get_user(target.id) is None, "still a live account"
    assert repo.get_user(target.id, include_deleted=True) is not None
    # The financial records are untouched.
    assert len(repo.list_payments(user_id=target.id)) == payments_before
    assert repo.subscription_history(target.id)


def test_a_removed_account_leaves_the_default_list():
    repo = _repo()
    target = _make_client(repo, "alice")
    bobby = _make_client(repo, "bobby")
    client = _admin(repo)

    _post(client, f"/admin/clients/{target.id}/delete", confirm="alice")

    # Asserted on the row's link rather than on the name, because the flash
    # confirmation legitimately names the account it just removed.
    page = _body(client, "/admin/clients")
    assert f"/admin/clients/{target.id}" not in page
    assert f"/admin/clients/{bobby.id}" in page


def test_the_removed_only_filter_shows_removed_accounts_and_nothing_else():
    """The control is labelled "Removed only", so it must not be a synonym for
    "include removed" — which would leave the live accounts on the page."""
    repo = _repo()
    target = _make_client(repo, "alice")
    bobby = _make_client(repo, "bobby")
    client = _admin(repo)
    _post(client, f"/admin/clients/{target.id}/delete", confirm="alice")

    page = _body(client, "/admin/clients?deleted=1")
    assert f"/admin/clients/{target.id}" in page
    assert f"/admin/clients/{bobby.id}" not in page


def test_a_removed_account_can_be_found_and_restored():
    repo = _repo()
    target = _make_client(repo, "alice")
    client = _admin(repo)
    _post(client, f"/admin/clients/{target.id}/delete", confirm="alice")

    # It is still listed when the operator asks for removed accounts.
    assert "alice" in _body(client, "/admin/clients?deleted=1")
    # Its detail page is still reachable, because that is where restore lives.
    assert "removed" in _body(client, f"/admin/clients/{target.id}")

    _post(client, f"/admin/clients/{target.id}/restore")

    repo.session.expire_all()
    assert repo.get_user(target.id) is not None
    assert "alice" in _body(client, "/admin/clients")


def test_restoring_an_account_that_is_not_removed_is_harmless():
    repo = _repo()
    target = _make_client(repo, "alice")
    client = _admin(repo)

    response = _post(client, f"/admin/clients/{target.id}/restore")

    assert response.status_code in (302, 200)
    repo.session.expire_all()
    assert repo.get_user(target.id) is not None


def test_removal_requires_csrf():
    repo = _repo()
    target = _make_client(repo, "alice")
    client = _admin(repo)

    client.post(f"/admin/clients/{target.id}/delete", data={"confirm": "alice"})

    repo.session.expire_all()
    assert repo.get_user(target.id) is not None


def test_removing_an_account_ends_its_session():
    """A removed account stops working on its very next request, not at expiry."""
    repo = _repo()
    target = _make_client(repo, "alice")
    _grant(repo, target, "basic")   # so there is a gated page to lose
    admin = _admin(repo)

    victim = _app(repo).test_client()
    victim.get("/login")
    with victim.session_transaction() as sess:
        sess["csrf"] = _CSRF
    victim.post("/login", data={"username": "alice", "password": _PASSWORD,
                                "_csrf": _CSRF})
    assert victim.get("/dashboard").status_code == 200

    _post(admin, f"/admin/clients/{target.id}/delete", confirm="alice")

    assert victim.get("/dashboard").status_code == 302


def test_creating_a_client_notifies_them(monkeypatch):
    """The admin types the password, so the client has no way to know the account
    exists unless they are told — and told how to set their own."""
    sent = []
    monkeypatch.setattr("notifications.email.send_account_created",
                        lambda u, **kw: sent.append((u.username, kw)) or True)

    repo = _repo()
    client = _admin(repo)
    _post(client, "/admin/clients/create", username="newbie", email="n@example.com",
          password=_PASSWORD, display_name="New Bie")

    assert [name for name, _ in sent] == ["newbie"]


def test_creating_a_client_without_an_address_sends_nothing(monkeypatch):
    """There is nowhere to send it, and the sender refuses the empty address —
    but it must not be attempted at all."""
    sent = []
    monkeypatch.setattr("notifications.email.send_account_created",
                        lambda u, **kw: sent.append(u) or True)

    repo = _repo()
    client = _admin(repo)
    _post(client, "/admin/clients/create", username="newbie", password=_PASSWORD)

    assert sent == []


def test_a_broken_mailer_does_not_undo_a_created_account(monkeypatch):
    """The account is committed before the email is attempted, so a mail failure
    must leave it standing."""
    def _explode(*args, **kwargs):
        raise RuntimeError("resend is unreachable")

    monkeypatch.setattr("notifications.email.send_account_created", _explode)

    repo = _repo()
    client = _admin(repo)
    _post(client, "/admin/clients/create", username="newbie", email="n@example.com",
          password=_PASSWORD)

    assert repo.get_user_by_username("newbie") is not None


# --------------------------------------------------------------------------- #
# The billing views
# --------------------------------------------------------------------------- #
def test_the_billing_pages_render():
    repo = _repo()
    client = _admin(repo)
    for path in ("/admin/subscriptions", "/admin/payments"):
        assert client.get(path).status_code == 200, path


def test_the_billing_pages_are_admin_only():
    repo = _repo()
    _make_client(repo, "alice")
    client = _app(repo).test_client()
    client.get("/login")
    with client.session_transaction() as sess:
        sess["csrf"] = _CSRF
    client.post("/login", data={"username": "alice", "password": _PASSWORD,
                                "_csrf": _CSRF})

    for path in ("/admin/subscriptions", "/admin/payments"):
        assert client.get(path).status_code == 403, path


def test_a_bought_subscription_appears_with_its_payment():
    repo = _repo()
    target = _make_client(repo, "alice")
    _grant(repo, target, "premium")
    client = _admin(repo)

    page = _body(client, "/admin/subscriptions")
    assert "alice" in page
    assert "premium" in page
    # The row names the payment that bought it. Every subscriptions row is
    # written by activate_subscription, which is only ever called with a
    # settled payment, so this is the normal case and not a special one.
    assert "Payment #" in page

    payments = _body(client, "/admin/payments")
    assert "alice" in payments
    assert "successful" in payments


def test_a_hand_granted_plan_is_not_a_subscription_row():
    """The page must not imply it lists hand-granted access.

    A plan set on a client's profile is not written to ``subscriptions``, so it
    cannot appear here. Asserted so the day someone adds a grant route that
    writes a row, this test fails and the page's copy gets revisited with it.
    """
    repo = _repo()
    target = _make_client(repo, "alice")
    repo.update_client_profile(target.id, subscription_status=m.SUB_ACTIVE,
                               subscription_plan="vip")
    client = _admin(repo)

    page = _body(client, "/admin/subscriptions")
    assert "vip" not in page
    # And the page says as much rather than leaving the reader to infer it.
    assert "hand-granted" in page.lower()

    # It does show up as the account's effective plan, which is where an
    # operator is meant to find it.
    detail = _body(client, f"/admin/clients/{target.id}")
    assert "vip" in detail


def test_a_failed_payment_shows_its_reason():
    """A run of failures with reasons is how a declined card is told from a
    broken integration; without the reason the page cannot tell them apart."""
    repo = _repo()
    target = _make_client(repo, "alice")
    _failed_payment(repo, target)
    client = _admin(repo)

    page = _body(client, "/admin/payments")
    assert "Insufficient funds" in page
    assert "failed" in page


def test_the_payments_page_offers_no_way_to_mark_a_payment_paid():
    """A subscription activated by a click is a subscription nobody paid for.

    Asserted as an absence of a *mutating* control: every form on the page is
    either the GET status filter or the sign-out button in the shell, and none of
    them reaches a payment.
    """
    repo = _repo()
    target = _make_client(repo, "alice")
    _failed_payment(repo, target)
    client = _admin(repo)

    page = _body(client, "/admin/payments")
    # The sentence wraps in the template, so the assertion is on the clause that
    # sits on one line.
    assert "to change a payment's status" in page

    # No form on the page posts anywhere but sign-out.
    forms = re.findall(r"<form\b[^>]*>", page, flags=re.I)
    posting = [f for f in forms if "post" in f.lower()]
    assert posting, "expected the shell's sign-out form, else this asserts nothing"
    for form in posting:
        action = re.search(r'action="([^"]*)"', form, flags=re.I)
        assert action and action.group(1) in ("/logout",), form

    # Turned around: posting the obvious guesses changes nothing either.
    payment = repo.list_payments(user_id=target.id)[0]
    for path in (f"/admin/payments/{payment.id}/success",
                 f"/admin/payments/{payment.id}",
                 "/admin/payments"):
        _post(client, path, status=m.PAY_SUCCESSFUL, id=payment.id)

    repo.session.expire_all()
    assert repo.list_payments(user_id=target.id)[0].status == m.PAY_FAILED


def test_the_unconfigured_notice_is_shown_when_payments_are_off():
    """An empty list must not be readable as "nobody has bought anything" when
    the truth is "nothing can be bought"."""
    repo = _repo()
    client = _admin(repo)

    # The test settings leave the Flutterwave keys blank, so both billing pages
    # and the overview must say so.
    for path, needle in (("/admin/payments", "not configured on this deployment"),
                         ("/admin/subscriptions", "not configured on this deployment"),
                         ("/admin", "not configured on this deployment")):
        page = _body(client, path)
        assert "FLUTTERWAVE_SECRET_KEY" in page, path
        assert needle in page, path


def test_the_overview_shows_billing_figures():
    repo = _repo()
    target = _make_client(repo, "alice")
    _grant(repo, target, "premium")
    client = _admin(repo)

    page = _body(client, "/admin")
    assert "Subscriptions and revenue" in page
    # The amount is the premium price in the currency it was charged in,
    # formatted through the same helper every other page uses.
    expected = format_minor(get_plan("premium").price_minor,
                            get_plan("premium").currency)
    assert f">{expected}<" in page, expected


def test_revenue_is_not_summed_across_currencies():
    """Adding NGN to USD gives a number that is not money in either currency.

    Two settled payments in two currencies must render as two amounts, and never
    as one combined figure.
    """
    repo = _repo()
    target = _make_client(repo, "alice")

    for currency, amount, ref in (("NGN", 750_000, "r-ngn"),
                                  ("USD", 5_000, "r-usd")):
        repo.sync_plans(spec_rows())
        plan = repo.get_plan_by_key("basic")
        repo.create_payment(user_id=target.id, plan=plan, reference=ref,
                            amount_minor=amount, currency=currency)
        repo.settle_payment(ref, provider_tx_id=f"flw-{ref}",
                            amount_minor=amount, currency=currency,
                            payload={"status": "successful"})

    client = _admin(repo)
    page = _body(client, "/admin")

    assert format_minor(750_000, "NGN") in page
    assert format_minor(5_000, "USD") in page
    # The sum, in either currency's notation, must not be there.
    assert format_minor(750_000 + 5_000, "NGN") not in page
    assert format_minor(750_000 + 5_000, "USD") not in page
