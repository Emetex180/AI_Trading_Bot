"""The 3rader subscription tiers — the single source of truth.

Every price, feature bullet and ordering rank the platform shows or reasons
about comes from :data:`PLANS`. Nothing else in the codebase should hold a
price or ask "is this the VIP plan?"; routes ask :mod:`app.access`, and templates
read the plan rows this module seeds.

How a change to pricing or features is made
-------------------------------------------
Edit :data:`PLANS` here and restart. :func:`database.repository.Repository
.sync_plans` inserts any key it has never seen and **never overwrites a row that
already exists**, so an operator who edits a price through the admin area keeps
that edit. Feature *access* is a separate concern and lives in
:mod:`app.access` — the bullets below are marketing copy, the mapping there is
what actually gates a page.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Currency every plan is priced in. Flutterwave settles the charge in whatever
#: currency is passed at checkout, so this is the one place it is decided.
CURRENCY = "USD"


@dataclass(frozen=True)
class PlanSpec:
    """One tier, as defined in code.

    Mirrors :class:`database.models.Plan` but is immutable and carries no
    database identity — this is the *definition*, the row is the *record*.
    """

    key: str
    name: str
    #: Minor units (cents). ``50`` dollars is ``5000``. Integer, never a float:
    #: a webhook amount is compared against this to authorise a subscription,
    #: and a float would make that comparison inexact.
    price_minor: int
    description: str
    #: Marketing bullets for the pricing card. Must describe something the
    #: platform actually does — an unverifiable bullet is a false claim.
    features: tuple[str, ...] = field(default_factory=tuple)
    #: Ordering rank for "at least this tier" checks. Gaps are deliberate, so a
    #: tier can be inserted later without renumbering the ones after it.
    level: int = 0
    #: Presented as the default choice on /pricing. Exactly one plan should set
    #: this; the template highlights whichever do.
    highlight: bool = False
    sort_order: int = 0
    currency: str = CURRENCY

    @property
    def price(self) -> float:
        """The price in major units, for display only.

        Never use this for a comparison or an arithmetic step — convert the
        customer's charge to minor units instead (see
        :func:`app.payments.to_minor_units`).
        """
        return self.price_minor / 100

    @property
    def price_display(self) -> str:
        """``$50`` rather than ``$50.00`` when the price is a whole unit."""
        if self.price_minor % 100 == 0:
            return f"${self.price_minor // 100:,}"
        return f"${self.price_minor / 100:,.2f}"


#: The three launch tiers, cheapest first. ``level`` is what
#: :mod:`app.access` compares, so it must stay consistent with ``sort_order``.
PLANS: tuple[PlanSpec, ...] = (
    PlanSpec(
        key="basic",
        name="Basic",
        price_minor=5_000,
        description="Structured market coverage and the signals view, for a "
                    "single disciplined approach to the sessions that matter.",
        features=(
            "Multi-asset market monitoring",
            "Live quotes and session state",
            "Trading signals view",
            "Trading dashboard access",
            "Account and profile management",
        ),
        level=10,
        sort_order=1,
    ),
    PlanSpec(
        key="premium",
        name="Premium",
        price_minor=10_000,
        description="The full validated setup feed, with the liquidity and "
                    "structure detail behind every signal.",
        features=(
            "Everything in Basic",
            "Validated setup feed",
            "Setup detail: liquidity and FVG levels",
            "Signal history with filters",
            "Risk and structure breakdown",
            "Email notifications",
        ),
        level=20,
        highlight=True,
        sort_order=2,
    ),
    PlanSpec(
        key="vip",
        name="VIP",
        price_minor=50_000,
        description="Every tool the platform runs, including the analysis "
                    "workspace and performance reporting.",
        features=(
            "Everything in Premium",
            "Full analysis workspace",
            "Performance and backtest reporting",
            "Telegram signal delivery",
            "Priority support",
        ),
        level=30,
        sort_order=3,
    ),
)

#: Key -> spec, for the lookup the access layer and the templates both need.
PLANS_BY_KEY: dict[str, PlanSpec] = {p.key: p for p in PLANS}

#: Every key, cheapest first.
PLAN_KEYS: tuple[str, ...] = tuple(p.key for p in PLANS)

#: The tier a signed-up account starts on before buying anything. ``None``
#: rather than a free plan, because no free tier was specified and inventing one
#: would grant access the pricing page does not sell.
DEFAULT_PLAN_KEY: str | None = None


def get_plan(key: str | None) -> PlanSpec | None:
    """The spec for a plan key, or ``None`` if there is no such plan.

    Returns ``None`` rather than raising: a key arriving from a URL or a stale
    form post is ordinary input, and the caller's job is to render a 404 or a
    validation message, not to handle an exception.
    """
    if not key:
        return None
    return PLANS_BY_KEY.get(str(key).strip().lower())


def is_valid_key(key: str | None) -> bool:
    return get_plan(key) is not None


def plan_level(key: str | None) -> int:
    """Ordering rank of a plan, or ``0`` for an unknown/absent one.

    ``0`` is below every real tier (the cheapest is ``10``), so an account with
    no subscription fails every "at least Basic" comparison without needing a
    special case.
    """
    spec = get_plan(key)
    return spec.level if spec else 0


def level_of(plan_key_or_spec) -> int:
    """``plan_level`` that also accepts a spec or a ``Plan`` row."""
    if plan_key_or_spec is None:
        return 0
    if isinstance(plan_key_or_spec, PlanSpec):
        return plan_key_or_spec.level
    if isinstance(plan_key_or_spec, str):
        return plan_level(plan_key_or_spec)
    return int(getattr(plan_key_or_spec, "level", 0) or 0)


#: Symbols for the currencies we expect to quote in. Anything else falls back to
#: a code prefix (``"GHS 50.00"``), which is legible even if it is not pretty —
#: better than a wrong symbol.
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "NGN": "₦", "GHS": "GH₵",
            "KES": "KSh", "ZAR": "R"}


def format_minor(minor: int, currency: str = CURRENCY) -> str:
    """``5000`` -> ``"$50.00"``. Exact, via integer arithmetic.

    Deliberately not ``display.money``: that takes a major-unit figure and
    formats a float, which is right for a broker balance and wrong for money the
    platform charges. This stays in the integer minor units the database stores,
    so the figure on a receipt is the figure that was recorded.
    """
    try:
        amount = int(minor)
    except (TypeError, ValueError):
        return "—"
    code = (currency or CURRENCY).upper()
    whole, cents = divmod(abs(amount), 100)
    sign = "-" if amount < 0 else ""
    symbol = _SYMBOLS.get(code)
    body = f"{whole:,}.{cents:02d}"
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{code} {body}"


def price_of(plan, currency: str = "") -> str:
    """A display price for a spec, a stored ``Plan`` row, or a key.

    Accepts anything with ``price_minor`` because the pricing page is rendered
    from the database rows while the checkout is priced from the code specs, and
    both must format identically.
    """
    minor = getattr(plan, "price_minor", None)
    if minor is None:
        spec = get_plan(plan if isinstance(plan, str) else None)
        minor = spec.price_minor if spec else 0
    return format_minor(minor, currency or getattr(plan, "currency", "")
                        or CURRENCY)


def spec_rows() -> list[dict]:
    """The specs as plain dicts, for seeding the ``plans`` table.

    Kept here rather than in the repository so the column names live next to the
    fields they mirror and a new field is one edit, not two.
    """
    return [
        {
            "key": p.key,
            "name": p.name,
            "price_minor": p.price_minor,
            "currency": p.currency,
            "description": p.description,
            "features": list(p.features),
            "level": p.level,
            "highlight": p.highlight,
            "sort_order": p.sort_order,
            "is_active": True,
        }
        for p in PLANS
    ]
