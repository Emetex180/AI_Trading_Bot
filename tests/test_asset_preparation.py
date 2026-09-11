"""Registry asset -> broker symbol + contract spec, as the job runners do it.

This is the integration point where a portable registry entry (``XAUUSD``) turns
into something the terminal understands (``XAUUSDm``) plus the contract numbers
that make position sizing correct for that asset class. It is deliberately
tested on its own because the live session, the backtest and the history probe
all go through it.
"""
from __future__ import annotations

from trading.asset_manager import Asset
from trading.instrument import prepare_asset


class _SpecClient:
    """A terminal with a fixed symbol list and per-symbol contract metadata."""

    def __init__(self, infos):
        self._infos = dict(infos)

    def symbol_names(self):
        return list(self._infos)

    def symbol_info(self, symbol):
        return self._infos.get(symbol)

    def ensure_symbol(self, symbol):
        return symbol in self._infos


def test_resolves_the_broker_symbol_and_reads_the_contract():
    client = _SpecClient({"XAUUSDm": {
        "name": "XAUUSDm", "digits": 2, "visible": True,
        "trade_contract_size": 100.0, "volume_min": 0.01,
        "volume_step": 0.01, "volume_max": 50.0,
        "tick_size": 0.01, "tick_value": 1.0,
    }})
    asset = Asset(name="XAUUSD", broker_symbol="XAUUSD", enabled=True, digits=2)

    resolved, spec = prepare_asset(client, asset, resolve=True)

    assert resolved.broker_symbol == "XAUUSDm"   # the broker's spelling
    assert resolved.name == "XAUUSD"             # the strategy name is unchanged
    assert spec.contract_size == 100.0
    assert spec.volume_step == 0.01
    assert resolved.spec is spec
    # The shared registry object must not be mutated by a session.
    assert asset.broker_symbol == "XAUUSD"
    assert asset.spec is None


def test_falls_back_to_the_registry_for_fields_the_broker_omits():
    """A partial response degrades to the registry, never to plausible defaults."""
    client = _SpecClient({"EURUSDm": {
        # No contract/volume fields at all — only a name and a precision.
        "name": "EURUSDm", "digits": 5, "visible": True,
    }})
    asset = Asset(name="EURUSD", broker_symbol="EURUSD", enabled=True, digits=5,
                  contract_size=100000.0, volume_min=0.01, volume_step=0.01)

    resolved, spec = prepare_asset(client, asset, resolve=True)

    assert resolved.broker_symbol == "EURUSDm"
    assert spec.contract_size == 100000.0    # registry fallback, not 1.0
    assert spec.volume_step == 0.01
    assert spec.digits == 5                  # reported by the broker


def test_broker_reported_values_are_never_overwritten_by_the_registry():
    """A complete response wins — the registry's fallback is only a fallback."""
    client = _SpecClient({"USTECm": {
        "name": "USTECm", "digits": 1, "visible": True,
        "trade_contract_size": 10.0, "volume_min": 0.1,
        "volume_step": 0.1, "volume_max": 20.0,
    }})
    # The registry disagrees on every one of those numbers.
    asset = Asset(name="USTEC", broker_symbol="USTEC", enabled=True, digits=2,
                  contract_size=1.0, volume_min=0.01, volume_step=0.01,
                  volume_max=100.0)

    _resolved, spec = prepare_asset(client, asset, resolve=True)

    assert spec.contract_size == 10.0
    assert spec.volume_min == 0.1
    assert spec.volume_step == 0.1
    assert spec.volume_max == 20.0
    assert spec.digits == 1


def test_a_symbol_the_broker_does_not_offer_is_skipped():
    client = _SpecClient({"EURUSDm": {"name": "EURUSDm"}})
    asset = Asset(name="NOPE", broker_symbol="NOPE", enabled=True)

    assert prepare_asset(client, asset, resolve=True) == (None, None)


def test_without_resolution_the_registry_symbol_is_trusted():
    client = _SpecClient({"USTEC": {
        "name": "USTEC", "digits": 2, "visible": True,
        "trade_contract_size": 1.0, "volume_step": 0.01,
    }})
    asset = Asset(name="USTEC", broker_symbol="USTEC", enabled=True, digits=2)

    resolved, spec = prepare_asset(client, asset, resolve=False)

    assert resolved.broker_symbol == "USTEC"
    assert spec.contract_size == 1.0


def test_without_resolution_an_unknown_symbol_is_still_skipped():
    client = _SpecClient({})
    asset = Asset(name="NOPE", broker_symbol="NOPE", enabled=True)
    assert prepare_asset(client, asset, resolve=False) == (None, None)


def test_a_client_without_symbol_metadata_does_not_drop_every_asset():
    """Minimal clients must still scan — the registry's own numbers apply."""

    class _Bare:
        def connect(self):
            return True

    asset = Asset(name="USTEC", broker_symbol="USTEC", enabled=True, digits=2)
    resolved, spec = prepare_asset(_Bare(), asset, resolve=True)

    assert resolved is not None
    assert resolved.broker_symbol == "USTEC"
    assert spec.contract_size == 1.0
