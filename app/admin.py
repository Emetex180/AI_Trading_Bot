"""Administration area: clients, trading activity and broker-account links.

Every route here requires the ``admin`` role (see :func:`app.auth.admin_required`),
on both the pages and the mutations — a client calling a mutation endpoint
directly gets a flat 403 rather than a redirect, so there is no path that relies
on the UI merely not offering a button.

Account/fund information
------------------------
:mod:`trading.broker_accounts` defines the interface for reading a *client's*
broker account, and this module renders whatever it returns. Today it returns
nothing: no client-broker integration is configured, so every link displays "not
connected". That is a statement about the platform, not about the client's
money, and the page says so in as many words rather than printing a zero.

The distinction the code enforces: ``latest_snapshot() is None`` means *never
read*, which is rendered as an unknown. A snapshot whose fields are ``None``
means *read, but the broker did not expose that field*, which is rendered as an
em dash. Neither is ever a fabricated figure. When a real provider is registered
(one :func:`trading.broker_accounts.register_provider` call), the figures appear
here with no template or schema change.
"""
from __future__ import annotations

import logging
from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from database import models as m
from trading import broker_accounts as ba
from trading import time_utils as tu

from .api import asset_choices
from .auth import admin_required, current_user, password_problem, require_csrf
from .client import live_view
from .display import ny_str
from .access import entitlement_for
from .plans import PLAN_KEYS, spec_rows

log = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _cfg():
    return current_app.config["CFG"]


def _jobs():
    return current_app.config["JOBS"]


def _engine_view() -> tuple[dict, dict]:
    """The engine's live state and job status, read across the process boundary.

    The scanner runs as its own process (``run.py scan``) and owns the MT5
    terminal while it does, so this process' ``JobManager`` has no live thread
    of its own to report. Reading it directly answered "stopped" for a scanner
    that was running perfectly well — the same defect the client dashboard was
    fixed for.

    ``live_view`` is that fix, and it is reused rather than reimplemented: one
    cross-process bridge (``engine_state``), so the console and the client
    dashboard can never disagree about whether the engine is up.

    ``jobs_status`` keeps the rest of ``JobManager.status()`` — the backtest,
    probe, broker and account slices, which are per-process by nature — but its
    ``live`` slice and ``live_running`` flag are replaced with the leased view,
    because those two are the ones that describe the scanner.
    """
    jobs, repo = _jobs(), g.repo
    live = live_view(jobs, repo)
    status = jobs.status()
    status["live_running"] = bool(live.get("alive"))
    status["live"] = live
    return live, status


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #
def client_row(user) -> dict:
    """One client as the admin list/detail shows them.

    The profile is optional in the schema (an admin has none), so every
    subscription field tolerates its absence rather than assuming a row exists.

    Two plan fields, and the difference matters. ``subscription_plan`` is what
    the admin-maintained profile says; ``effective_plan`` is what
    :mod:`app.access` will actually decide for this account, which is the
    purchased subscription when there is one. The list shows the effective one,
    because a row that named a plan the account cannot use would be worse than
    no row at all — an operator reading it would draw the wrong conclusion about
    what the customer has paid for.
    """
    profile = user.profile
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name or user.username,
        "role": user.role,
        "status": user.status,
        "is_active": user.is_active,
        "is_deleted": user.is_deleted,
        "notes": user.notes,
        "phone": user.phone,
        "country": user.country,
        "has_avatar": bool(user.avatar_path),
        "created_by": user.created_by or "—",
        "created_at": user.created_at,
        "created_at_ny": ny_str(user.created_at),
        "last_login_at": user.last_login_at,
        "last_login_ny": ny_str(user.last_login_at) if user.last_login_at else "",
        "subscription_status": (profile.subscription_status if profile
                                else m.SUB_NONE),
        "subscription_plan": profile.subscription_plan if profile else "",
        "subscription_expires_at": profile.subscription_expires_at if profile else None,
        "subscription_expires_ny": (ny_str(profile.subscription_expires_at)
                                    if profile and profile.subscription_expires_at
                                    else ""),
    }


def client_rows(users, repo) -> list[dict]:
    """``client_row`` for a list, with the effective plan resolved readably.

    :func:`app.access.entitlement_for` answers "what does this account actually
    have" — consulting the purchased subscription first and the admin-granted
    profile second, exactly as the guard on every gated page does. Reusing it
    rather than reading ``subscription_plan`` off the profile is what stops the
    admin list from showing a plan the client cannot use, which is the one way
    this table could actively mislead an operator.

    The entitlement object is kept whole on the row, so a template can show the
    source ("bought" versus "granted") without a second lookup.
    """
    rows = []
    for user in users:
        row = client_row(user)
        entitlement = entitlement_for(user, repo)
        row["entitlement"] = entitlement
        row["effective_plan"] = entitlement.plan_key
        row["effective_plan_name"] = (entitlement.plan_name
                                      if entitlement.is_active else "")
        row["effective_source"] = entitlement.source
        rows.append(row)
    return rows


def broker_row(account, repo) -> dict:
    """One broker link, with whatever the provider can actually tell us.

    ``snapshot`` is ``None`` for a link no provider has ever read. The template
    keys "not connected" off exactly that, which is why this returns the raw
    snapshot rather than a zeroed-out dict.
    """
    provider = ba.provider_for(account)
    provider_available = bool(getattr(provider, "available", False))
    snapshot = repo.latest_snapshot(account.id)
    return {
        "id": account.id,
        "user_id": account.user_id,
        "provider": account.provider,
        "provider_available": provider_available,
        "provider_name": getattr(provider, "name", account.provider),
        "login": account.login,
        "server": account.server,
        "label": account.label,
        "is_active": account.is_active,
        "created_at": account.created_at,
        "created_at_ny": ny_str(account.created_at),
        "snapshot": snapshot,
        "balance": snapshot.balance if snapshot else None,
        "equity": snapshot.equity if snapshot else None,
        "margin_free": snapshot.margin_free if snapshot else None,
        "currency": snapshot.currency if snapshot else None,
        # No snapshot at all is "not connected"; a snapshot with no balance is
        # "connected, but the broker did not report one". Different sentences.
        "connected": snapshot is not None,
        "read_at_ny": (ny_str(snapshot.fetched_at_utc)
                       if snapshot and snapshot.fetched_at_utc else ""),
        "source": snapshot.source if snapshot else "",
    }


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@admin_bp.get("")
@admin_bp.get("/")
@admin_required
def index():
    repo = g.repo
    cfg = _cfg()
    jobs = _jobs()
    live, jobs_status = _engine_view()
    stats = repo.platform_stats()
    return render_template(
        "admin/index.html",
        nav="admin",
        clients=client_rows(repo.list_clients(), repo),
        counts={
            "clients": len(repo.list_clients()),
            "clients_active": sum(1 for u in repo.list_clients() if u.is_active),
            "users": repo.count_users(),
            "setups_total": repo.count_signals(),
            "setups_approved": repo.count_signals("APPROVED"),
            "trades": repo.count_trades(),
            "backtests": repo.count_backtests(),
        },
        # The billing figures, from the same aggregate the payments page uses.
        # ``revenue_minor`` is rendered through ``format_minor`` so the amount
        # and the currency on the page come from one place, and it is summed per
        # currency rather than across them — adding NGN to USD would produce a
        # number that is not money in either.
        stats=stats,
        revenue_by_currency=_revenue_by_currency(repo),
        payments_configured=_payments_configured(),
        jobs_status=jobs_status,
        live=live,
        recent_events=repo.recent_events(12),
        broker_links=[broker_row(a, repo)
                      for a in repo.list_broker_accounts()],
        integrations=ba.integration_status(),
        telegram={"enabled": cfg.telegram_enabled,
                  "configured": bool(cfg.telegram_bot_token
                                     and cfg.telegram_chat_id)},
        auto_trading=cfg.effective_auto_trading,
        account=jobs.account_state(),
    )


@admin_bp.get("/clients")
@admin_required
def clients():
    """The client list, filtered.

    The filters are read from the query string so a filtered view is a URL an
    operator can bookmark or paste to a colleague — which is what makes search
    worth having here rather than a client-side filter over a rendered table.

    The filtering happens in SQL (:meth:`Repository.search_users`) rather than in
    the template, because a page that renders every account and then hides most
    of them has still read every account, and the count shown above a filtered
    table would be a count of something else.
    """
    repo = g.repo

    query = (request.args.get("q") or "").strip()
    role = (request.args.get("role") or "").strip().lower()
    plan = (request.args.get("plan") or "").strip().lower()
    status = (request.args.get("status") or "").strip().lower()
    deleted = (request.args.get("deleted") or "") == "1"

    if role not in m.ROLES:
        role = ""
    if plan not in PLAN_KEYS:
        plan = ""
    if status not in (m.STATUS_ACTIVE, m.STATUS_SUSPENDED):
        status = ""

    found = repo.search_users(query=query, role=role or None, plan=plan or None,
                              status=status or None, only_deleted=deleted)

    return render_template(
        "admin/clients.html",
        nav="admin-clients",
        clients=client_rows(found, repo),
        admins=client_rows(repo.list_users(role=m.ROLE_ADMIN), repo),
        statuses=(m.STATUS_ACTIVE, m.STATUS_SUSPENDED),
        subscriptions=m.SUBSCRIPTION_STATES,
        plans=spec_rows(),
        min_password=_cfg().min_password_length,
        filters={"q": query, "role": role, "plan": plan, "status": status,
                 "deleted": deleted},
        filtering=bool(query or role or plan or status or deleted),
        totals={"shown": len(found), "all": repo.count_users(include_deleted=False)},
    )


@admin_bp.get("/clients/<int:user_id>")
@admin_required
def client_detail(user_id: int):
    repo = g.repo
    # Soft-deleted accounts are still reachable here on purpose: this page is
    # where the restore button lives, so hiding it would make the removal
    # irreversible from the UI. The page says the account is removed, so an
    # operator cannot mistake it for a live one.
    user = repo.get_user(user_id, include_deleted=True)
    if user is None:
        abort(404)
    links = repo.list_broker_accounts(user_id)
    return render_template(
        "admin/client_detail.html",
        nav="admin-clients",
        c=client_row(user),
        user=user,
        broker_links=[broker_row(a, repo) for a in links],
        integrations=ba.integration_status(),
        statuses=(m.STATUS_ACTIVE, m.STATUS_SUSPENDED),
        roles=m.ROLES,
        subscriptions=m.SUBSCRIPTION_STATES,
        min_password=_cfg().min_password_length,
        entitlement=entitlement_for(user, repo),
        subscription_history=repo.subscription_history(user_id),
        payments=repo.list_payments(user_id=user_id, limit=50),
        # A client's own setup history is the same read the client platform
        # makes, so an admin sees exactly what the client sees.
        setups=[s for s in repo.find_signals(limit=25)],
    )


@admin_bp.get("/activity")
@admin_required
def activity():
    repo = g.repo
    cfg = _cfg()
    live, jobs_status = _engine_view()
    return render_template(
        "admin/activity.html",
        nav="admin-activity",
        jobs_status=jobs_status,
        live=live,
        account=jobs_status.get("account") or {},
        backtest=jobs_status.get("backtest") or {},
        monitored=live.get("assets") or [],
        setups=live.get("setups") or {},
        prices=live.get("prices") or {},
        price_times=live.get("price_times") or {},
        assets=asset_choices(cfg, repo),
        recent_signals=repo.recent_signals(40),
        recent_trades=repo.recent_trades(25),
        events=repo.recent_events(60),
        auto_trading=cfg.effective_auto_trading,
    )


@admin_bp.get("/accounts")
@admin_required
def accounts():
    repo = g.repo
    links = repo.list_broker_accounts()
    by_user = {u.id: u for u in repo.list_users()}
    return render_template(
        "admin/accounts.html",
        nav="admin-accounts",
        links=[{**broker_row(a, repo),
                "username": (by_user[a.user_id].username
                             if a.user_id in by_user else "—")}
               for a in links],
        integrations=ba.integration_status(),
        operator=current_app.config["JOBS"].account_state(),
    )


# --------------------------------------------------------------------------- #
# Mutations
#
# Every one is POST + CSRF-checked (see app.auth.require_csrf) and answers with a
# flash + redirect, so a double-submit cannot silently apply twice and the admin
# always lands back on a rendered page.
# --------------------------------------------------------------------------- #
def _deny_unless_csrf():
    """CSRF gate for an admin mutation; ``None`` when the request may proceed."""
    return require_csrf()


@admin_bp.post("/clients/create")
@admin_required
def create_client():
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    cfg = _cfg()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    email = (request.form.get("email") or "").strip()
    display_name = (request.form.get("display_name") or "").strip()
    role = (request.form.get("role") or m.ROLE_CLIENT).strip().lower()
    plan = (request.form.get("subscription_plan") or "").strip()

    if role not in m.ROLES:
        role = m.ROLE_CLIENT

    def back(message: str, level: str = "danger"):
        flash(message, level)
        return redirect(url_for("admin.clients"))

    if not username:
        return back("A username is required.")
    if repo.get_user_by_username(username) is not None:
        return back(f"The username {username!r} is already taken.")
    problem = password_problem(password, minimum=cfg.min_password_length)
    if problem:
        return back(problem)

    user = repo.create_user(
        username=username, password_hash_or_plain=password, role=role,
        email=email, display_name=display_name,
        created_by=current_user().username,
        subscribe=(role == m.ROLE_CLIENT))
    if role == m.ROLE_CLIENT:
        repo.update_client_profile(user.id, subscription_status=m.SUB_TRIAL,
                                   subscription_plan=plan)
    repo.log_event("INFO", "admin",
                   f"{current_user().username} created {role} {username!r}")

    # Best-effort, and after the account is committed. An admin who creates an
    # account has to type a password for it, which the client has no way of
    # knowing — so this tells them the account exists and how to set their own.
    # Only attempted when an address was given; the sender refuses the rest.
    if email:
        try:
            from notifications.email import send_account_created

            send_account_created(user)
        except Exception:  # pragma: no cover - an email must not undo a creation
            log.exception("Could not send the account-created email for %r",
                          username)

    flash(f"Created {role} {username!r}.", "success")
    return redirect(url_for("admin.client_detail", user_id=user.id))


@admin_bp.post("/clients/<int:user_id>/status")
@admin_required
def set_client_status(user_id: int):
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    user = repo.get_user(user_id)
    if user is None:
        abort(404)
    status = (request.form.get("status") or "").strip().lower()
    if status not in (m.STATUS_ACTIVE, m.STATUS_SUSPENDED):
        flash("Unknown status.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    actor = current_user()
    # An admin locking themselves out is the one mistake here that cannot be
    # undone from inside the app, so it is refused rather than flashed about.
    if user.id == actor.id and status == m.STATUS_SUSPENDED:
        flash("You cannot suspend your own account.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    repo.set_user_status(user_id, status)
    repo.log_event("INFO", "admin",
                   f"{actor.username} set {user.username!r} to {status}")
    flash(f"{user.username} is now {status}.", "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.post("/clients/<int:user_id>/password")
@admin_required
def set_client_password(user_id: int):
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo, cfg = g.repo, _cfg()
    user = repo.get_user(user_id)
    if user is None:
        abort(404)

    password = request.form.get("password") or ""
    problem = password_problem(password, minimum=cfg.min_password_length)
    if problem:
        flash(problem, "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    repo.set_user_password(user_id, password)
    # Recorded as a warning: an admin resetting someone's credentials is exactly
    # the event an audit trail exists for. The password itself is never logged.
    repo.log_event("WARN", "admin",
                   f"{current_user().username} reset the password for "
                   f"{user.username!r}")
    flash(f"Password updated for {user.username}.", "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.post("/clients/<int:user_id>/profile")
@admin_required
def set_client_profile(user_id: int):
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    user = repo.get_user(user_id)
    if user is None:
        abort(404)

    status = (request.form.get("subscription_status") or "").strip().lower()
    if status not in m.SUBSCRIPTION_STATES:
        status = m.SUB_NONE
    plan = (request.form.get("subscription_plan") or "").strip()
    expires = _parse_date(request.form.get("subscription_expires_at"))

    repo.update_client_profile(user_id, subscription_status=status,
                               subscription_plan=plan,
                               subscription_expires_at=expires)
    repo.log_event("INFO", "admin",
                   f"{current_user().username} set {user.username!r} "
                   f"subscription to {status}")
    flash("Subscription updated.", "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.post("/clients/<int:user_id>/details")
@admin_required
def set_client_details(user_id: int):
    """Edit a client's own account fields, as an administrator.

    Separate from :func:`set_client_profile`, which edits the *subscription*.
    The two are different questions — who this person is, versus what they have
    paid for — and a single form covering both is how an operator changes a
    display name and accidentally rewrites a plan.

    ``role`` and ``password`` are deliberately not here either: promoting an
    account is :func:`set_client_role`, and setting a password is
    :func:`set_client_password`. Three narrow endpoints rather than one wide one,
    so each has one thing to get right.
    """
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    user = repo.get_user(user_id)
    if user is None:
        abort(404)

    display_name = (request.form.get("display_name") or "").strip()[:128]
    email = (request.form.get("email") or "").strip()[:254]
    phone = (request.form.get("phone") or "").strip()[:32]
    country = (request.form.get("country") or "").strip()[:64]
    notes = (request.form.get("notes") or "").strip()[:4000]

    if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
        flash("A valid email address is required.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    # The same uniqueness question the client's own settings page asks, from the
    # other side: an address identifies the account for sign-in and password
    # recovery, so two accounts must not share one or a reset link could reach
    # the wrong person's inbox.
    holder = repo.get_user_by_email(email)
    if holder is not None and holder.id != user.id:
        flash(f"{email} is already used by {holder.username!r}.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    repo.update_user_as_admin(user_id, display_name=display_name, email=email,
                              phone=phone, country=country, notes=notes)
    repo.log_event("INFO", "admin",
                   f"{current_user().username} edited the details of "
                   f"{user.username!r}")
    flash("Client details updated.", "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.post("/clients/<int:user_id>/role")
@admin_required
def set_client_role(user_id: int):
    """Change an account's role, between the two roles the platform declares.

    Narrow like every other mutation here — one field, one endpoint — and the
    reference :func:`set_client_details` makes to it is now a real route.

    Three guards, and the third is the one that cannot be left to the interface:

    * **Admin-only.** :func:`app.auth.admin_required` answers a client calling
      this endpoint directly with a flat 403, so nothing here depends on the
      button merely not being rendered.
    * **Only a declared role is accepted.** ``m.ROLES`` is the same tuple the
      guard and the schema use. An unknown value is refused rather than coerced,
      so a crafted POST cannot write one no other part of the platform
      understands.
    * **An admin cannot demote themselves.** The check is on the session's user
      id, not on a form field, so it cannot be posted around. The detail page
      also disables the control on the operator's own account, but that is a
      convenience; this is the rule.

    Recorded at ``WARN``, like the other privilege-shaped mutations here: the
    line names the actor, the target and both roles, because "who changed whose
    access, and when" is the question an audit trail exists to answer. No
    credential of any kind is written to it.
    """
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    user = repo.get_user(user_id)
    if user is None:
        abort(404)

    wanted = (request.form.get("role") or "").strip().lower()
    if wanted not in m.ROLES:
        flash("Unknown role.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    actor = current_user()
    # Refused rather than flashed about as a warning: an admin who removes their
    # own admin role locks the deployment out of its own administration, and
    # there is no route back in except the command line.
    if user.id == actor.id and wanted != m.ROLE_ADMIN:
        flash("You cannot remove your own administrator role.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    previous = user.role
    if previous == wanted:
        flash(f"{user.username} is already {wanted}.", "info")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    repo.set_user_role(user_id, wanted)
    repo.log_event("WARN", "admin",
                   f"{actor.username} changed the role of {user.username!r} "
                   f"from {previous} to {wanted}")
    flash(f"{user.username} is now {wanted}.", "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.post("/clients/<int:user_id>/delete")
@admin_required
def delete_client(user_id: int):
    """Remove an account, softly, with the confirmation the operation needs.

    Three guards, and each is load-bearing:

    * **An admin cannot delete themselves.** Otherwise the only account with
      access to this page can lock the deployment out of its own administration,
      and there is no route back in except the command line. The check is on the
      session's user id, not on a form field, so it cannot be posted around.
    * **A typed confirmation is required.** Deleting a person is not a mis-click
      the UI should absorb silently; ``confirm`` must equal the username, which
      makes the operator name the account they are removing.
    * **It is a soft delete.** ``deleted_at`` is set, so the payments and
      subscriptions the account accrued survive — those are the records an
      operator needs when a charge is disputed — while the account stops
      resolving on its next request and its live subscription is expired.
    """
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    user = repo.get_user(user_id, include_deleted=True)
    if user is None:
        abort(404)

    actor = current_user()
    if user.id == actor.id:
        # Stated plainly rather than as a generic refusal: an operator who tried
        # this needs to know it is the rule, not a fault.
        flash("You cannot delete the account you are signed in with.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    if user.is_deleted:
        flash(f"{user.username!r} has already been removed.", "warning")
        return redirect(url_for("admin.clients"))

    if (request.form.get("confirm") or "").strip() != user.username:
        flash(f"Type the username ({user.username}) to confirm the removal.",
              "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    repo.soft_delete_user(user_id)
    repo.log_event("WARN", "admin",
                   f"{actor.username} removed {user.username!r}")
    # The username is still shown because the operator needs to know the removal
    # happened to the account they meant; the row is gone from the list either
    # way, which is the confirmation that it took.
    flash(f"Removed {user.username!r}. Their payment history is retained.",
          "success")
    return redirect(url_for("admin.clients"))


@admin_bp.post("/clients/<int:user_id>/restore")
@admin_required
def restore_client(user_id: int):
    """Undo a soft delete.

    Worth having because the removal is reversible: an operator who removes the
    wrong account can put it back, which is the difference between a considered
    decision and an irreversible one.
    """
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    user = repo.get_user(user_id, include_deleted=True)
    if user is None:
        abort(404)
    if not user.is_deleted:
        flash(f"{user.username!r} is not removed.", "warning")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    repo.restore_user(user_id)
    repo.log_event("INFO", "admin",
                   f"{current_user().username} restored {user.username!r}")
    flash(f"Restored {user.username!r}. They will need to sign in again.",
          "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.get("/subscriptions")
@admin_required
def subscriptions():
    """Every subscription bought through checkout, newest first.

    Read-only on purpose. A subscription is granted by a payment settling or by
    an admin editing the profile, and both of those already have a route; a
    third one that flipped a status directly would be a way to grant access with
    no payment and no audit line.

    Only the first of those two appears here, because only it writes a
    ``subscriptions`` row. A hand-granted plan is a column on the client's
    profile and is shown as that account's effective plan instead — so this page
    must not be read as the full list of who has access.
    """
    repo = g.repo
    wanted = (request.args.get("status") or "").strip().lower()
    if wanted not in m.SUBSCRIPTION_STATES:
        wanted = ""

    rows = repo.list_subscriptions(status=wanted or None, limit=500)
    names = {u.id: u for u in repo.list_users(include_deleted=True)}
    return render_template(
        "admin/subscriptions.html",
        nav="admin-subscriptions",
        rows=[{"sub": s, "user": names.get(s.user_id)} for s in rows],
        statuses=m.SUBSCRIPTION_STATES,
        filter_status=wanted,
        payments_configured=_payments_configured(),
    )


@admin_bp.get("/payments")
@admin_required
def payments():
    """Every payment attempt, including the failed ones.

    Failed attempts are shown rather than filtered out by default: a run of
    failures from one card is how a declined payment is distinguished from a
    broken integration, and a page that only listed successes could not tell an
    operator which of the two they are looking at.
    """
    repo = g.repo
    wanted = (request.args.get("status") or "").strip().lower()
    if wanted not in m.PAYMENT_STATES:
        wanted = ""

    rows = repo.list_payments(status=wanted or None, limit=500)
    names = {u.id: u for u in repo.list_users(include_deleted=True)}
    return render_template(
        "admin/payments.html",
        nav="admin-payments",
        rows=[{"payment": p, "user": names.get(p.user_id)} for p in rows],
        statuses=m.PAYMENT_STATES,
        filter_status=wanted,
        stats=repo.platform_stats(),
        stats_by_currency=_revenue_by_currency(repo),
        payments_configured=_payments_configured(),
    )


def _payments_configured() -> bool:
    """Whether this deployment can actually take a card payment.

    Surfaced on the billing pages because an operator looking at an empty list
    needs to know whether that means "nobody has paid" or "nothing can be paid".
    Those look identical in a table and mean opposite things.
    """
    cfg = _cfg()
    return bool((cfg.flutterwave_secret_key or "").strip()
                and (cfg.flutterwave_public_key or "").strip())


def _revenue_by_currency(repo) -> dict:
    """Successful payments summed per currency.

    Per currency rather than one total, because adding NGN to USD produces a
    number that is not money in any currency. A deployment that has only ever
    charged in one will show one row, which is the honest version of a total.
    """
    totals: dict[str, int] = {}
    for payment in repo.list_payments(status=m.PAY_SUCCESSFUL, limit=2000):
        code = (payment.currency or "").upper() or "—"
        totals[code] = totals.get(code, 0) + int(payment.amount_minor or 0)
    return totals


def _parse_date(raw):
    """A ``YYYY-MM-DD`` date field as a naive-UTC instant, or ``None``.
    Stored as an instant (midnight NY that day) rather than a date, because every
    other timestamp in this schema is naive-UTC and mixing the two conventions is
    how a date ends up rendering a day early.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return tu.ny_to_utc(parsed)


@admin_bp.post("/clients/<int:user_id>/broker/add")
@admin_required
def add_broker_account(user_id: int):
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    user = repo.get_user(user_id)
    if user is None:
        abort(404)

    login = (request.form.get("login") or "").strip()
    if not login:
        flash("A broker login is required.", "danger")
        return redirect(url_for("admin.client_detail", user_id=user_id))

    account = repo.add_broker_account(
        user_id=user_id, login=login,
        server=(request.form.get("server") or "").strip(),
        provider=(request.form.get("provider") or "mt5").strip() or "mt5",
        label=(request.form.get("label") or "").strip())
    repo.log_event("INFO", "admin",
                   f"{current_user().username} linked broker account "
                   f"{login!r} to {user.username!r}")
    flash(f"Linked {login}. Balance appears once a provider is configured.", "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.post("/broker/<int:account_id>/remove")
@admin_required
def remove_broker_account(account_id: int):
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    account = next((a for a in repo.list_broker_accounts() if a.id == account_id),
                   None)
    if account is None:
        abort(404)
    user_id = account.user_id
    repo.remove_broker_account(account_id)
    repo.log_event("INFO", "admin",
                   f"{current_user().username} removed broker link "
                   f"{account.login!r}")
    flash("Broker link removed.", "success")
    return redirect(url_for("admin.client_detail", user_id=user_id))


@admin_bp.post("/broker/<int:account_id>/refresh")
@admin_required
def refresh_broker_account(account_id: int):
    """Ask the provider for a fresh reading, if one is configured.

    Returns the honest outcome either way: with no provider registered this
    records nothing and says so, rather than writing a snapshot full of ``None``
    that would later read as "connected but empty".
    """
    denied = _deny_unless_csrf()
    if denied is not None:
        return denied

    repo = g.repo
    account = next((a for a in repo.list_broker_accounts() if a.id == account_id),
                   None)
    if account is None:
        abort(404)

    provider = ba.provider_for(account)
    if not getattr(provider, "available", False):
        flash("No broker integration is configured, so there is nothing to "
              "read. See trading/broker_accounts.py.", "warning")
        return redirect(url_for("admin.accounts"))

    snapshot = ba.read_account(account)
    if snapshot is None:
        flash("The provider could not read that account just now.", "warning")
        return redirect(url_for("admin.accounts"))

    repo.save_broker_snapshot(account.id, balance=snapshot.balance,
                              equity=snapshot.equity,
                              margin_free=snapshot.margin_free,
                              currency=snapshot.currency,
                              source=snapshot.source)
    repo.log_event("INFO", "admin",
                   f"read broker account {account.login!r} via "
                   f"{snapshot.source or provider.name}")
    flash("Account read.", "success")
    return redirect(url_for("admin.accounts"))


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def register_admin(app) -> None:
    """Attach the admin area to ``app`` (called from ``create_app``)."""
    app.register_blueprint(admin_bp)
