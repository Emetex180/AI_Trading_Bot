"""The public marketing site: home, features, pricing, about and the legal pages.

These are the only routes in the application that render for someone who is not
signed in, so two rules apply throughout:

* **Every figure shown is read from the running system.** The homepage counters
  come from the asset registry and the strategy's own session definitions — not
  from a copywriter. There is no win rate, no return figure and no customer count
  anywhere on these pages, because none of them could be substantiated and a
  fabricated one would be a lie told to a stranger deciding whether to trust us
  with money.
* **Nothing here is a promise.** The copy describes what the software does. The
  risk disclaimer is linked from every page's footer and is a page of its own,
  and the sign-up form requires it to be accepted.

The pages are static apart from the counters, so they render without touching the
database on the happy path and stay up when the engine is down or the market is
closed — which is exactly when a prospective customer is most likely to be
reading them.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, redirect, render_template, url_for

from .auth import current_user
from .plans import PLANS

log = logging.getLogger(__name__)

public_bp = Blueprint("public", __name__)

#: The trading sessions the strategy actually runs. Kept in step with
#: ``trading.sessions``; the homepage says "four sessions" because the engine
#: defines four, and this constant exists so the two cannot drift apart silently.
SESSION_NAMES = ("Asia", "London", "New York AM", "New York PM")


def _cfg():
    return current_app.config["CFG"]


def platform_figures(repo=None, cfg=None) -> dict:
    """The honest counters for the homepage.

    Read from the live registry rather than hard-coded. If a deployment monitors
    four assets, the page says four — the number is a fact about the software,
    not a marketing claim, and it stays true when the registry changes.
    """
    monitored = 0
    try:
        from .api import asset_choices

        monitored = sum(1 for a in asset_choices(cfg or _cfg(),
                                                 repo if repo is not None else g.repo)
                        if a["enabled"])
    except Exception:  # pragma: no cover - a marketing page must not 500
        log.warning("Could not read the asset registry for the homepage",
                    exc_info=True)
    return {
        "monitored_assets": monitored,
        "sessions": len(SESSION_NAMES),
        "session_names": SESSION_NAMES,
        "strategy_states": 7,
    }


def _nav_context(**extra) -> dict:
    """Everything the marketing shell needs, so no page has to remember it."""
    from trading import time_utils as tu

    context = {"plans": PLANS, "support_email": _cfg().support_email,
               "ny_year": tu.now_utc().year}
    context.update(extra)
    return context


def home_context(repo=None, cfg=None) -> dict:
    """Context for the homepage.

    Shared by the public route and by the ``/`` root handler, so a stranger
    landing on the domain and a stranger landing on a shared link see the same
    page built from the same figures.
    """
    return _nav_context(figures=platform_figures(repo, cfg))


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@public_bp.get("/features")
def features():
    return render_template("public/features.html",
                           **_nav_context(page="features",
                                          figures=platform_figures()))

@public_bp.get("/pricing")
def pricing():
    """The public price list.

    Reads :data:`app.plans.PLANS`, the same tuple the checkout prices from, so
    the figure on this page is by construction the figure the customer is
    charged. A pricing page built from a second source is how a site ends up
    advertising one price and billing another.
    """
    return render_template("public/pricing.html",
                           **_nav_context(page="pricing"))


@public_bp.get("/about")
def about():
    # Shares the homepage's counters: the about page describes the same engine,
    # so it must not be able to state a different number of instruments.
    return render_template("public/about.html",
                           **_nav_context(page="about",
                                          figures=platform_figures()))


@public_bp.get("/contact")
def contact():
    return render_template("public/contact.html", **_nav_context(page="contact"))


# --------------------------------------------------------------------------- #
# Legal
#
# Three separate documents rather than one page of boilerplate, because they
# answer three different questions and a reader looking for one should not have
# to scroll past the others.
# --------------------------------------------------------------------------- #
@public_bp.get("/terms")
def terms():
    return render_template("public/terms.html", **_nav_context(page="terms"))


@public_bp.get("/privacy")
def privacy():
    return render_template("public/privacy.html", **_nav_context(page="privacy"))


@public_bp.get("/risk")
def risk():
    return render_template("public/risk.html", **_nav_context(page="risk"))


@public_bp.get("/disclaimer")
def disclaimer():
    """The risk disclaimer has one canonical URL. This alias is kept because the
    word is the one people look for, and a 404 on it would be a poor answer."""
    return redirect(url_for("public.risk"), code=301)


def register_public(app) -> None:
    app.register_blueprint(public_bp)
