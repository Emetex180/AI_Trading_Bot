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

from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from database import models as m
from trading import broker_accounts as ba
from trading import time_utils as tu

from .api import asset_choices
from .auth import admin_required, current_user, password_problem, require_csrf
from .display import ny_str

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _cfg():
    return current_app.config["CFG"]


def _jobs():
    return current_app.config["JOBS"]


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #
def client_row(user) -> dict:
    """One client as the admin list/detail shows them.

    The profile is optional in the schema (an admin has none), so every
    subscription field tolerates its absence rather than assuming a row exists.
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
        "notes": user.notes,
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
    live = jobs.live_state()
    return render_template(
        "admin/index.html",
        nav="admin",
        clients=[client_row(u) for u in repo.list_clients()],
        counts={
            "clients": len(repo.list_clients()),
            "clients_active": sum(1 for u in repo.list_clients() if u.is_active),
            "users": repo.count_users(),
            "setups_total": repo.count_signals(),
            "setups_approved": repo.count_signals("APPROVED"),
            "trades": repo.count_trades(),
            "backtests": repo.count_backtests(),
        },
        jobs_status=jobs.status(),
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
    repo = g.repo
    return render_template(
        "admin/clients.html",
        nav="admin-clients",
        clients=[client_row(u) for u in repo.list_clients()],
        admins=[client_row(u) for u in repo.list_users(role=m.ROLE_ADMIN)],
        statuses=(m.STATUS_ACTIVE, m.STATUS_SUSPENDED),
        subscriptions=m.SUBSCRIPTION_STATES,
        min_password=_cfg().min_password_length,
    )


@admin_bp.get("/clients/<int:user_id>")
@admin_required
def client_detail(user_id: int):
    repo = g.repo
    user = repo.get_user(user_id)
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
        subscriptions=m.SUBSCRIPTION_STATES,
        min_password=_cfg().min_password_length,
        # A client's own setup history is the same read the client platform
        # makes, so an admin sees exactly what the client sees.
        setups=[s for s in repo.find_signals(limit=25)],
    )


@admin_bp.get("/activity")
@admin_required
def activity():
    repo = g.repo
    cfg = _cfg()
    jobs = _jobs()
    live = jobs.live_state()
    state = jobs.status()
    return render_template(
        "admin/activity.html",
        nav="admin-activity",
        jobs_status=state,
        live=live,
        account=state.get("account") or {},
        backtest=state.get("backtest") or {},
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
