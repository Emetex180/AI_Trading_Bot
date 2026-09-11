"""Broker-symbol resolution tests (no MT5, no network).

The registry stores portable base names (``XAUUSD``); the broker may call that
``XAUUSDm``. These tests pin the resolution rules: exact wins, suffix/prefix
variants are accepted deterministically, an explicit alias table covers names no
string rule can link, and anything genuinely ambiguous or absent returns ``None``
rather than guessing.
"""
from __future__ import annotations

from trading.symbol_resolver import (plausible_variants, resolve,
                                     resolve_and_select)


class _FakeClient:
    """A terminal exposing a fixed symbol list, like ``mt5.symbols_get()``."""

    def __init__(self, names, selectable=None):
        self._names = list(names)
        # Symbols the terminal refuses to make visible.
        self._unselectable = set(selectable or [])
        self.selected: list[str] = []

    def symbol_names(self):
        return list(self._names)

    def symbol_info(self, symbol):
        if symbol in self._names:
            return {"name": symbol, "digits": 2, "visible": symbol not in self._unselectable}
        return None

    def ensure_symbol(self, symbol):
        if symbol not in self._names:
            return False
        if symbol in self._unselectable:
            return False
        self.selected.append(symbol)
        return True


class _NoInventoryClient:
    """A minimal client with no symbol list at all."""

    def symbol_info(self, symbol):
        return None


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #
def test_exact_match_is_used_as_is():
    client = _FakeClient(["EURUSD", "EURUSDm"])
    assert resolve(client, "EURUSD") == "EURUSD"


def test_suffix_variant_is_found():
    client = _FakeClient(["EURUSDm", "GBPUSDm", "XAUUSDm"])
    assert resolve(client, "XAUUSD") == "XAUUSDm"
    assert resolve(client, "EURUSD") == "EURUSDm"


def test_shortest_decoration_wins():
    """``XAUUSDm`` beats ``XAUUSDm.raw`` — fewest decorations, deterministic."""
    client = _FakeClient(["XAUUSDm.raw", "XAUUSDm", "XAUUSDmicro"])
    assert resolve(client, "XAUUSD") == "XAUUSDm"


def test_result_is_deterministic_regardless_of_list_order():
    names = ["XAUUSDmicro", "XAUUSDm", "XAUUSDm.raw"]
    assert (resolve(_FakeClient(names), "XAUUSD")
            == resolve(_FakeClient(list(reversed(names))), "XAUUSD"))


def test_alias_table_covers_renamed_instruments():
    """USTEC is US100/NAS100 at other brokers — no string rule links those."""
    assert resolve(_FakeClient(["US100"]), "USTEC") == "US100"
    assert resolve(_FakeClient(["NAS100m"]), "USTEC") == "NAS100m"
    assert resolve(_FakeClient(["GOLDm"]), "XAUUSD") == "GOLDm"


def test_unknown_symbol_returns_none():
    client = _FakeClient(["EURUSD", "GBPUSD"])
    assert resolve(client, "NOTASYMBOL") is None


def test_partial_substring_is_not_a_match():
    """A name that merely *contains* the base must not be silently adopted."""
    client = _FakeClient(["XAUEURUSD", "MINIUSD"])
    assert resolve(client, "XAUUSD") is None


def test_no_symbol_inventory_gives_up_cleanly():
    assert resolve(_NoInventoryClient(), "EURUSD") is None


def test_blank_request_is_none():
    assert resolve(_FakeClient(["EURUSD"]), "") is None
    assert resolve(_FakeClient(["EURUSD"]), "   ") is None


# --------------------------------------------------------------------------- #
# resolve_and_select
# --------------------------------------------------------------------------- #
def test_resolve_and_select_makes_the_symbol_visible():
    """Unselected symbols return no history, so selection is part of resolving."""
    client = _FakeClient(["EURUSDm"])
    assert resolve_and_select(client, "EURUSD") == "EURUSDm"
    assert client.selected == ["EURUSDm"]


def test_resolve_and_select_reports_an_unselectable_symbol():
    client = _FakeClient(["EURUSDm"], selectable=["EURUSDm"])
    assert resolve_and_select(client, "EURUSD") is None


def test_resolve_and_select_without_a_selector_returns_the_symbol():
    """A client with no symbol-selection API still resolves; the caller decides."""

    class _NoSelector:
        def symbol_names(self):
            return ["EURUSDm"]

        def symbol_info(self, symbol):
            return {"visible": True} if symbol == "EURUSDm" else None

    assert resolve_and_select(_NoSelector(), "EURUSD") == "EURUSDm"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_plausible_variants_are_offered_for_error_messages():
    variants = plausible_variants("XAUUSD", limit=12)
    assert "XAUUSD" in variants
    assert "XAUUSDm" in variants
    assert len(variants) == 12
