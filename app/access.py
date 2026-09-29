"""Subscription entitlement: what a signed-in account may reach.

This is the *only* module that decides whether a feature is available. Routes
do not compare plan names, and no template performs an access check of its own —
a page is guarded by :func:`require_feature`, and the navigation asks
:func:`has_feature` merely to decide what to draw. Hiding a link is never the
control; the control is the guard, which runs on the server before the view
body does.

Resolution order
----------------
An account's tier comes from, in order:

1. The active row in ``subscriptions`` — what the payment flow writes.
2. ``ClientProfile.subscription_plan`` / ``.subscription_expires_at`` — the
   fields the admin area has always edited by hand.

Both are consulted because both are legitimate ways an entitlement is granted:
a customer buys one, or an admin grants one. Neither path can be dropped
without breaking the other. An **admin** bypasses the check entirely — they
administer every client's subscription and must be able to see what a client
sees in order to support them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from functools import wraps

from flask import flash, g, jsonify, redirect, render_template, request, url_for

from trading import time_utils as tu

from .auth import current_user
from .plans import PLAN_KEYS, get_plan, level_of

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Features
#
# One constant per gated page. Named for the *capability*, not the URL, so a
# route can move without an access change and a capability can back two routes.
# --------------------------------------------------------------------------- #
FEATURE_DASHBOARD = "dashboard"
FEATURE_MARKET = "market"
FEATURE_SETUPS = "setups"
FEATURE_HISTORY = "history"
FEATURE_ANALYSIS = "analysis"

#: Every gated capability, in the order the pricing page presents them.
FEATURES: tuple[str, ...] = (
    FEATURE_DASHBOARD, FEATURE_MARKET, FEATURE_SETUPS, FEATURE_HISTORY,
    FEATURE_ANALYSIS,
)

#: Human label per feature, for the upgrade page and the pricing matrix.
FEATURE_LABELS: dict[str, str] = {
    FEATURE_DASHBOARD: "Trading dashboard",
    FEATURE_MARKET: "Market monitoring",
    FEATURE_SETUPS: "Validated setup feed",
    FEATURE_HISTORY: "Signal history",
    FEATURE_ANALYSIS: "Analysis workspace",
}

#: Which capabilities each tier unlocks.
#:
#: Cumulative by construction — a higher tier lists everything the tier below it
#: does. They are written out in full rather than derived with set unions
#: because reading the mapping should answer "what does VIP include?" at a
#: glance, without evaluating anything.
PLAN_FEATURES: dict[str, frozenset[str]] = {
    "basic": frozenset({FEATURE_DASHBOARD, FEATURE_MARKET}),
    "premium": frozenset({FEATURE_DASHBOARD, FEATURE_MARKET, FEATURE_SETUPS,
                          FEATURE_HISTORY}),
    "vip": frozenset({FEATURE_DASHBOARD, FEATURE_MARKET, FEATURE_SETUPS,
                      FEATURE_HISTORY, FEATURE_ANALYSIS}),
}

#: The cheapest tier that unlocks each feature, for the upgrade prompt.
#: Derived, so it can never disagree with :data:`PLAN_FEATURES`.
FEATURE_MINIMUM_PLAN: dict[str, str] = {
    feature: next(
        (key for key in PLAN_KEYS if feature in PLAN_FEATURES.get(key, ())),
        PLAN_KEYS[-1],
    )
    for feature in FEATURES
}


# --------------------------------------------------------------------------- #
# Entitlement
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Entitlement:
    """What an account is entitled to right now, and where that came from.

    Carried as a value rather than recomputed per call site so one request
    cannot see two different answers — and so a template can render the plan
    name, the state and the expiry without a second lookup.
    """

    plan_key: str = ""
    status: str = "none"
    expires_at: datetime | None = None
    #: ``"subscription"`` (bought), ``"profile"`` (admin-granted), ``"none"``.
    source: str = "none"

    @property
    def plan(self):
        """The :class:`app.plans.PlanSpec`, or ``None`` for no entitlement."""
        return get_plan(self.plan_key)

    @property
    def level(self) -> int:
        return level_of(self.plan_key)

    @property
    def is_active(self) -> bool:
        return self.status == "active" and bool(self.plan_key)

    @property
    def plan_name(self) -> str:
        spec = self.plan
        return spec.name if spec else "No plan"

    @property
    def features(self) -> frozenset[str]:
        return PLAN_FEATURES.get(self.plan_key, frozenset())

    @property
    def days_left(self) -> int | None:
        if not self.expires_at:
            return None
        delta = self.expires_at - tu.now_utc()
        return max(0, delta.days)


#: Returned when there is nothing to look up — an anonymous visitor, or a
#: database error. A single shared instance because it is immutable.
NO_ENTITLEMENT = Entitlement()


def _repo():
    """The request's repository, or ``None`` outside a request context."""
    return getattr(g, "repo", None)


def _unexpired(when: datetime | None) -> bool:
    """Has a subscription's end date passed? ``None`` means "no end date".

    A plan with no expiry (one an admin granted indefinitely) stays active —
    that is the existing behaviour of ``subscription_expires_at`` being unset,
    and it is preserved here.
    """
    if when is None:
        return True
    return when > tu.now_utc()


def entitlement_for(user, repo=None) -> Entitlement:
    """Resolve ``user``'s current entitlement.

    Failing closed at every step: an unknown plan key, an expired date or an
    unreachable database all yield no access rather than access. An
    authorisation check that errors open is worse than one that errors shut,
    and this one is called on every request to a gated page.
    """
    if user is None:
        return NO_ENTITLEMENT

    # An admin administers subscriptions, so they are never gated by one.
    if getattr(user, "is_admin", False):
        return Entitlement(plan_key=PLAN_KEYS[-1], status="active",
                           source="admin")

    repo = repo if repo is not None else _repo()
    if repo is None:
        return NO_ENTITLEMENT

    try:
        # 1. A purchased subscription is authoritative when one is live.
        sub = repo.active_subscription(user.id)
        if sub is not None and _unexpired(sub.expires_at):
            if get_plan(sub.plan_key):
                return Entitlement(plan_key=sub.plan_key, status="active",
                                   expires_at=sub.expires_at,
                                   source="subscription")

        # 2. Otherwise fall back to the profile an admin maintains.
        profile = repo.client_profile(user.id)
        if profile is not None:
            key = (profile.subscription_plan or "").strip().lower()
            if get_plan(key):
                if profile.subscription_status == "active" and _unexpired(
                        profile.subscription_expires_at):
                    return Entitlement(plan_key=key, status="active",
                                       expires_at=profile.subscription_expires_at,
                                       source="profile")
                if profile.subscription_status in ("expired", "cancelled"):
                    return Entitlement(plan_key=key,
                                       status=profile.subscription_status,
                                       expires_at=profile.subscription_expires_at,
                                       source="profile")
    except Exception:
        # A database problem must not grant access, and must not 500 a page a
        # customer can otherwise read. Logged with the traceback so the operator
        # can see it; the caller just sees "no access".
        log.exception("Could not resolve entitlement for user %s",
                      getattr(user, "id", "?"))
        return NO_ENTITLEMENT

    return NO_ENTITLEMENT


def has_feature(user, feature: str, repo=None) -> bool:
    """Whether ``user`` may use ``feature``."""
    return feature in entitlement_for(user, repo).features


def require_feature(feature: str):
    """Guard a view behind a subscription tier.

    Sits *below* the authentication guard, not instead of it: an anonymous
    caller is sent to log in, and only an authenticated one is told they need a
    plan. Denial answers a browser with an upgrade page (which explains what the
    feature is and what it costs) and a ``fetch()`` with JSON, matching how
    :mod:`app.auth` answers its own refusals.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                if _wants_json():
                    return jsonify({"ok": False, "reason": "authentication_required",
                                    "message": "Sign in to continue."}), 401
                return redirect(url_for(
                    "auth.login",
                    next=request.full_path if request.query_string
                    else request.path))

            if not has_feature(user, feature):
                log.info("Feature %r refused for user %s (plan=%r)", feature,
                         user.id, entitlement_for(user).plan_key)
                if _wants_json():
                    return jsonify({
                        "ok": False,
                        "reason": "subscription_required",
                        "feature": feature,
                        "message": "Your current plan does not include this.",
                    }), 403
                return render_template(
                    "subscription/upgrade.html",
                    feature=feature,
                    feature_label=FEATURE_LABELS.get(feature, feature),
                    minimum_plan=FEATURE_MINIMUM_PLAN.get(feature),
                    entitlement=entitlement_for(user),
                ), 403

            return view(*args, **kwargs)
        return wrapper
    return decorator


def feature_gate(feature: str):
    """``require_feature`` for a *form post*: refuses with a redirect + reason.

    A gated action reached by submitting a form cannot answer with an upgrade
    page (the browser would be left on an error document mid-POST), so it
    flashes and returns to the page the form came from.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None or not has_feature(user, feature):
                if _wants_json():
                    return jsonify({"ok": False,
                                    "reason": "subscription_required"}), 403
                flash("Your current plan does not include that feature.",
                      "warning")
                return redirect(url_for("client.overview"))
            return view(*args, **kwargs)
        return wrapper
    return decorator


def _wants_json() -> bool:
    """Mirrors :func:`app.auth._wants_json` so both guards answer alike."""
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def navigation_for(user, repo=None) -> dict:
    """What the client-area navigation should draw for ``user``.

    Presentation only — it decides which links *appear*. Every one of those
    pages is independently guarded by :func:`require_feature`, so tampering with
    the returned set achieves nothing.
    """
    ent = entitlement_for(user, repo)
    items = [
        {"feature": FEATURE_DASHBOARD, "endpoint": "client.overview",
         "label": "Dashboard", "icon": "grid"},
        {"feature": FEATURE_MARKET, "endpoint": "client.market",
         "label": "Market", "icon": "activity"},
        {"feature": FEATURE_SETUPS, "endpoint": "client.setups",
         "label": "Setups", "icon": "target"},
        {"feature": FEATURE_HISTORY, "endpoint": "client.history",
         "label": "History", "icon": "clock"},
        {"feature": FEATURE_ANALYSIS, "endpoint": "client.analysis",
         "label": "Analysis", "icon": "chart"},
    ]
    locked = {f for f in FEATURES if f not in ent.features}
    return {"items": items, "locked": locked, "entitlement": ent}
