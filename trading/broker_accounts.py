"""Read-only broker-account access for client accounts.

Why this module exists
----------------------
The platform can already read the **operator's own** MT5 account — that is the
terminal the scanner is logged into, and its balance is a genuine reading
surfaced through ``runner.JobManager.account_state()``.

Reading a **client's** broker account is a different problem: it needs a
credential or an API grant the client has given, and this project has no such
integration yet. So rather than inventing a number, the platform ships this
interface with a :class:`NullProvider` behind it. Every client account therefore
reports "not connected", which is true, and wiring a real integration later
means registering one provider — no template, schema or route changes.

Contract
--------
* **Read-only, always.** Nothing in this module may place, modify or close an
  order. Account *information* is the entire scope. Order execution remains the
  sole responsibility of :mod:`trading.executor`, behind its own gates.
* **No fabricated values.** A provider returns ``None`` when it cannot read.
  ``None`` means "not known", and the UI must render it as such rather than as
  zero — an empty account and an unreadable one are different facts.
* **Attribution.** Every snapshot carries the provider ``source``, so a figure
  on screen can always be traced to what produced it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AccountSnapshot:
    """One reading of a broker account.

    Every money field is optional because a broker may legitimately not expose
    all of them, and because a partial read is still worth showing — unlike a
    guessed one.
    """

    balance: float | None = None
    equity: float | None = None
    margin_free: float | None = None
    currency: str | None = None
    source: str = ""


@runtime_checkable
class BrokerAccountProvider(Protocol):
    """What a real client-account integration must implement."""

    #: Short identifier stored on the snapshot (e.g. ``"mt5-investor"``).
    name: str

    @property
    def available(self) -> bool:
        """Whether this provider is configured well enough to be asked.

        ``False`` lets the UI distinguish "no integration configured" from
        "configured but the read failed", which are different things to fix.
        """
        ...

    def fetch(self, account) -> AccountSnapshot | None:
        """Read one account, or ``None`` if it cannot be read right now."""
        ...


class NullProvider:
    """The default: no client broker integration is configured.

    ``available`` is False and ``fetch`` always returns ``None``, so nothing
    downstream can mistake an unconfigured platform for a funded account.
    """

    name = "none"

    @property
    def available(self) -> bool:
        return False

    def fetch(self, account) -> AccountSnapshot | None:
        return None


_REGISTRY: dict[str, BrokerAccountProvider] = {}


def register_provider(provider: BrokerAccountProvider) -> None:
    """Make ``provider`` available, keyed by its ``name``."""
    _REGISTRY[provider.name] = provider


def unregister_provider(name: str) -> None:
    _REGISTRY.pop(name, None)


def get_provider(name: str) -> BrokerAccountProvider:
    """The provider registered under ``name``, or the null provider."""
    return _REGISTRY.get(name or "", NullProvider())


def provider_for(account) -> BrokerAccountProvider:
    """The provider that handles a given :class:`database.models.BrokerAccount`.

    An account whose ``provider`` is unknown or whose provider is not configured
    resolves to the null provider rather than raising: a stale link from a
    provider that has since been removed must render as "not connected", not
    break the page it appears on.
    """
    return get_provider(getattr(account, "provider", "") or "")


def read_account(account) -> AccountSnapshot | None:
    """Read ``account`` through its provider, or ``None``.

    Swallows provider errors deliberately: a broker being unreachable is an
    operational condition the UI reports, not a reason for an admin page to
    return a 500.
    """
    provider = provider_for(account)
    try:
        if not provider.available:
            return None
        return provider.fetch(account)
    except Exception:
        return None


def integration_status() -> dict:
    """Describe the configured integrations, for the admin accounts page."""
    return {
        "configured": sorted(_REGISTRY),
        "available": sorted(n for n, p in _REGISTRY.items() if p.available),
    }
