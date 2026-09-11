"""Map a strategy/registry symbol name onto the broker's actual symbol.

Brokers rename instruments. The same index is ``USTEC`` at one, ``US100`` or
``NAS100`` at another; gold is ``XAUUSD`` at one and ``XAUUSDm``, ``GOLD``,
``XAUUSD.pro`` at others. Writing the broker's exact string into ``assets.json``
would tie the registry to one account, so the registry holds a **base** name and
this module asks the connected terminal what that actually is.

Resolution order (first hit wins):

1. **Exact** — the terminal already has a symbol with that name.
2. **Suffix/prefix variant** — the base name appears in the candidate, e.g.
   ``XAUUSD`` -> ``XAUUSDm``. Ties are broken deterministically (see
   :func:`_rank`) so a given terminal always resolves to the same symbol.
3. **Alias** — a small, explicit table for the genuinely different names
   (``USTEC`` -> ``US100``/``NAS100``), because no string rule links those.
4. **Give up** — return ``None``. Failing loudly beats resolving ``GOLD`` to
   something the strategy was never meant to trade.

Nothing here mutates the terminal or places orders; it only reads the symbol
list. :func:`resolve_and_select` additionally makes the symbol visible in Market
Watch, which MT5 requires before history can be fetched for it.
"""
from __future__ import annotations

from typing import Any

# Names that differ by more than a suffix/prefix. Keys are the base names used
# in ``assets.json``; values are ordered by preference.
SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "USTEC": ("US100", "NAS100", "USTEC", "NDX100", "TECH100", "USTECH"),
    "US500": ("SP500", "US500", "SPX500", "USA500", "US500.cash"),
    "US30": ("US30", "DOW30", "DJ30", "USA30", "WS30"),
    "GER40": ("GER40", "DE40", "DAX40", "GER30", "DE30", "DAX"),
    "UK100": ("UK100", "FTSE100", "GB100", "UK100.cash"),
    "JP225": ("JP225", "JPN225", "NIKKEI", "JP225.cash"),
    "XAUUSD": ("XAUUSD", "GOLD"),
    "XAGUSD": ("XAGUSD", "SILVER"),
}

# Suffixes brokers commonly append to a base name. Ordered longest-first so
# ``.pro`` is preferred over ``.p`` when a name matches both.
COMMON_SUFFIXES = (
    "", ".pro", ".raw", ".ecn", ".cash", ".std", ".m", ".micro", ".mini",
    "m", ".i", "i", ".r", "r", ".c", "c", ".z", "z", ".s", "s",
)


def _rank(requested: str, candidate: str) -> tuple:
    """Sort key for candidate symbols — lower sorts better.

    Preference order: an exact match, then a pure suffix extension of the
    requested name (``XAUUSDm``), then any other candidate containing it. Within
    a tier, the shortest name wins (fewest decorations) and the name itself
    breaks the final tie, so the result is fully deterministic.
    """
    if candidate == requested:
        return (0, 0, candidate)
    if candidate.startswith(requested):
        return (1, len(candidate), candidate)
    if candidate.endswith(requested):
        return (2, len(candidate), candidate)
    return (3, len(candidate), candidate)


def _candidates(requested: str) -> list[str]:
    """Base names to look for, most specific first."""
    base = requested.strip()
    out = [base]
    for alias in SYMBOL_ALIASES.get(base.upper(), ()):  # explicit aliases
        if alias not in out:
            out.append(alias)
    return out


def _all_names(client: Any) -> list[str]:
    """Every symbol the terminal knows, or ``[]`` when unavailable."""
    getter = getattr(client, "symbol_names", None)
    if not callable(getter):
        return []  # a stripped-down client cannot resolve; callers give up cleanly
    try:
        return [str(n) for n in (getter() or [])]
    except Exception:
        return []


def resolve(client: Any, requested: str) -> str | None:
    """Return the broker symbol for ``requested``, or ``None`` if unknown.

    ``client`` is an :class:`~trading.mt5_client.MT5Client` (or a test double
    exposing the same ``symbol_info``/``symbol_names`` methods).
    """
    name = (requested or "").strip()
    if not name:
        return None

    # 1. Exact hit — the cheapest and most common case.
    try:
        if client.symbol_info(name) is not None:
            return name
    except Exception:
        return None

    # 2. Rank every symbol the terminal offers against our candidate base names.
    names = _all_names(client)
    if not names:
        return None

    ranked: list[tuple[tuple, str]] = []
    for base in _candidates(name):
        for candidate in names:
            if base.upper() not in candidate.upper():
                continue
            key = _rank(base, candidate)
            if key[0] == 3:
                continue  # only suffix/prefix or exact matches are trustworthy
            ranked.append((key, candidate))
    if not ranked:
        return None
    return min(ranked, key=lambda item: item[0])[1]


def resolve_and_select(client: Any, requested: str) -> str | None:
    """Resolve ``requested`` and make sure the result is visible in Market Watch.

    A symbol the broker offers but that is not selected in Market Watch returns
    no history from ``copy_rates_*``. Selecting it is the difference between an
    asset that works and one that is silently skipped.
    """
    symbol = resolve(client, requested)
    if symbol is None:
        return None
    ensure = getattr(client, "ensure_symbol", None)
    if not callable(ensure):
        # A client that cannot select symbols also cannot fetch history for an
        # unselected one, but that is the caller's problem to surface — refusing
        # here would drop assets on any minimal client.
        return symbol
    try:
        return symbol if ensure(symbol) else None
    except Exception:
        return None


def plausible_variants(requested: str, limit: int = 8) -> list[str]:
    """Likely broker spellings of ``requested``, for a helpful failure message."""
    base = (requested or "").strip()
    if not base:
        return []
    out: list[str] = []
    for candidate in _candidates(base):
        for suffix in COMMON_SUFFIXES:
            name = f"{candidate}{suffix}"
            if name not in out:
                out.append(name)
            if len(out) >= limit:
                return out
    return out
