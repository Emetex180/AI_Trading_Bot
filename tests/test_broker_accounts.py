"""Broker-account interface tests.

The whole point of ``trading/broker_accounts.py`` is that it *refuses to guess*.
There is no client-broker integration in this project, so every client link must
report "not connected" — a statement about the platform, not about the client's
money. The failure mode being guarded against is a plausible-looking ``0.00``,
which is indistinguishable on screen from a real, empty account.

A second, quieter property: this module sits on the client-account side of the
house, so it must never acquire the ability to place an order. That is asserted
here rather than left to review.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

from trading import broker_accounts as ba
from trading.broker_accounts import AccountSnapshot, BrokerAccountProvider


@dataclass
class _Link:
    """The minimum a provider needs from a broker link: its provider name."""
    provider: str = "mt5"
    login: str = "12345678"
    server: str = "Broker-Demo"


class _WorkingProvider:
    """A stand-in for the integration that does not exist yet.

    Its presence in these tests is the proof that the seam works: registering one
    of these is the entire change a real integration requires.
    """

    def __init__(self, snapshot=None, *, name="fake", available=True,
                 raises=False):
        self.name = name
        self._snapshot = snapshot
        self._available = available
        self._raises = raises
        self.calls = 0

    @property
    def available(self) -> bool:
        return self._available

    def fetch(self, account):
        self.calls += 1
        if self._raises:
            raise RuntimeError("terminal unreachable")
        return self._snapshot


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is module-global; no test may leak a provider into another."""
    before = dict(ba._REGISTRY)
    yield
    ba._REGISTRY.clear()
    ba._REGISTRY.update(before)


def _link(provider="mt5"):
    return _Link(provider=provider)


# --------------------------------------------------------------------------- #
# The default: nothing is connected, and that is stated as such
# --------------------------------------------------------------------------- #
def test_the_default_provider_is_the_null_one():
    assert isinstance(ba.provider_for(_link("mt5")), ba.NullProvider)
    assert isinstance(ba.get_provider(""), ba.NullProvider)
    assert isinstance(ba.get_provider("nothing-registered"), ba.NullProvider)


def test_the_null_provider_is_unavailable_and_reads_nothing():
    provider = ba.NullProvider()

    assert provider.available is False
    assert provider.fetch(_link()) is None
    assert ba.read_account(_link()) is None


def test_an_unconfigured_read_returns_none_rather_than_a_zeroed_snapshot():
    """``None`` is "not known". A zeroed snapshot would be a fabricated fact."""
    result = ba.read_account(_link("mt5"))

    assert result is None
    assert result != AccountSnapshot(balance=0.0, equity=0.0, margin_free=0.0)


def test_an_unknown_provider_name_resolves_to_null_rather_than_raising():
    """A stale link from a removed provider must render, not break the page."""
    assert ba.read_account(_link("some-broker-we-dropped")) is None


def test_an_unavailable_provider_is_never_asked():
    """``available`` exists so "not configured" never becomes "read failed"."""
    provider = _WorkingProvider(AccountSnapshot(balance=1.0), available=False)
    ba.register_provider(provider)

    assert ba.read_account(_link("fake")) is None
    assert provider.calls == 0


def test_a_provider_that_raises_does_not_take_the_page_down():
    ba.register_provider(_WorkingProvider(raises=True))

    assert ba.read_account(_link("fake")) is None


# --------------------------------------------------------------------------- #
# What a registered provider does
# --------------------------------------------------------------------------- #
def test_a_registered_provider_is_asked_and_its_answer_returned():
    snapshot = AccountSnapshot(balance=10_000.0, equity=9_842.15,
                               margin_free=9_842.15, currency="USD",
                               source="fake")
    provider = _WorkingProvider(snapshot)
    ba.register_provider(provider)

    assert ba.read_account(_link("fake")) == snapshot
    assert provider.calls == 1


def test_a_partial_read_is_still_worth_returning():
    """A broker that does not expose one field must not blank the whole panel."""
    ba.register_provider(_WorkingProvider(AccountSnapshot(balance=250.0,
                                                          currency="EUR")))

    result = ba.read_account(_link("fake"))

    assert result.balance == 250.0
    assert result.currency == "EUR"
    assert result.equity is None       # distinct from 0.0, and rendered as "—"


def test_every_snapshot_carries_its_source():
    """A figure on screen must be traceable to whatever produced it."""
    ba.register_provider(_WorkingProvider(AccountSnapshot(balance=1.0,
                                                          source="fake")))

    assert ba.read_account(_link("fake")).source == "fake"


def test_a_provider_is_keyed_by_its_name():
    ba.register_provider(_WorkingProvider())
    assert isinstance(ba.get_provider("fake"), _WorkingProvider)


def test_unregistering_removes_it():
    ba.register_provider(_WorkingProvider())
    ba.unregister_provider("fake")

    assert isinstance(ba.get_provider("fake"), ba.NullProvider)


def test_unregistering_something_absent_is_a_no_op():
    assert ba.unregister_provider("never-existed") is None


def test_registering_under_one_name_leaves_others_null():
    ba.register_provider(_WorkingProvider())

    assert isinstance(ba.get_provider("fake"), _WorkingProvider)
    assert isinstance(ba.get_provider("mt5"), ba.NullProvider)


# --------------------------------------------------------------------------- #
# Integration status, for the admin accounts page
# --------------------------------------------------------------------------- #
def test_integration_status_is_empty_by_default():
    """Which is what drives "not connected" in the UI."""
    assert ba.integration_status() == {"configured": [], "available": []}


def test_integration_status_distinguishes_configured_from_available():
    """Different things to fix: "no integration" vs "one that cannot reach out"."""
    ba.register_provider(_WorkingProvider(name="reachable"))
    ba.register_provider(_WorkingProvider(name="unreachable", available=False))

    status = ba.integration_status()

    assert status["configured"] == ["reachable", "unreachable"]
    assert status["available"] == ["reachable"]


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #
def test_the_provider_protocol_declares_only_reading():
    """Account *information* is the entire scope of this module.

    Order execution is :mod:`trading.executor`'s job, behind its own gates. A
    trading call declared here would be a second, ungated execution path — so the
    protocol's surface is pinned to exactly these three members.
    """
    declared = set(BrokerAccountProvider.__annotations__)
    declared |= {k for k in vars(BrokerAccountProvider) if not k.startswith("_")}

    assert declared == {"name", "available", "fetch"}


@pytest.mark.parametrize("forbidden", ["order_send", "order_check",
                                       "place_order", "modify", "close_order"])
def test_the_module_offers_no_way_to_trade(forbidden):
    assert not hasattr(ba, forbidden)
    assert not hasattr(ba.NullProvider(), forbidden)
    assert not hasattr(BrokerAccountProvider, forbidden)


def test_the_null_provider_satisfies_the_protocol():
    """It is the default everywhere, so it must be a valid provider."""
    assert isinstance(ba.NullProvider(), BrokerAccountProvider)


def test_the_module_never_binds_the_terminal():
    """No MT5 binding: reading a client account must never claim the scanner's
    terminal, which is the whole reason this is interface-only today."""
    source = Path(ba.__file__).read_text(encoding="utf-8")

    assert "import MetaTrader5" not in source
    assert "mt5_client" not in source
    assert "initialize(" not in source


def test_a_snapshot_defaults_every_money_field_to_none():
    """A bare snapshot is "nothing known", not "everything is zero"."""
    snapshot = AccountSnapshot()

    assert (snapshot.balance, snapshot.equity, snapshot.margin_free) \
        == (None, None, None)
    assert snapshot.currency is None
    assert snapshot.source == ""


def test_a_snapshot_is_immutable():
    """A reading that has been taken must not be edited after the fact."""
    snapshot = AccountSnapshot(balance=1.0)

    with pytest.raises(FrozenInstanceError):
        snapshot.balance = 2.0
