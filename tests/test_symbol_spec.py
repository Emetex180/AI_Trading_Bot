"""Symbol-spec and per-asset-class position-sizing tests.

The point of these is that "1 lot" means something different in every asset
class, so sizing must go through the broker's contract specification — and must
degrade to the legacy index-CFD behaviour rather than inventing numbers when the
spec is missing.
"""
from __future__ import annotations

import pytest

from trading.risk_manager import RiskManager, position_size
from trading.symbol_spec import SymbolSpec, legacy_spec


# --------------------------------------------------------------------------- #
# Spec construction
# --------------------------------------------------------------------------- #
def test_spec_from_index_cfd_info():
    spec = SymbolSpec.from_mt5_info("USTEC", {
        "digits": 2, "trade_contract_size": 1.0, "volume_min": 0.01,
        "volume_step": 0.01, "volume_max": 100.0,
        "tick_size": 0.01, "tick_value": 0.01,
    })
    assert spec.contract_size == 1.0
    assert spec.digits == 2
    assert spec.has_tick_data


def test_spec_from_forex_info():
    spec = SymbolSpec.from_mt5_info("EURUSDm", {
        "digits": 5, "trade_contract_size": 100000.0, "volume_min": 0.01,
        "volume_step": 0.01, "volume_max": 200.0,
        "tick_size": 0.00001, "tick_value": 1.0,
    })
    assert spec.contract_size == 100000.0
    # 1 pip (0.0001) of EURUSD on 1 lot is $10: 10 ticks of 0.00001 at $1 each.
    assert spec.risk_per_lot(0.0001) == pytest.approx(10.0)


def test_spec_ignores_nonsense_broker_values():
    """A bad/partial response must never inject a bogus contract size."""
    spec = SymbolSpec.from_mt5_info("BROKEN", {
        "trade_contract_size": 0, "volume_step": -1, "tick_size": float("nan"),
        "tick_value": "not-a-number", "digits": None,
    })
    assert spec.contract_size == 1.0      # falls back, does not become 0
    assert spec.volume_step == 0.0        # negative clamped to unknown
    assert spec.tick_size == 0.0          # NaN rejected
    assert spec.tick_value == 0.0
    assert spec.digits == 0
    assert not spec.has_tick_data


def test_missing_info_is_a_legacy_spec():
    spec = SymbolSpec.from_mt5_info("X", None)
    assert spec == legacy_spec("X") or (spec.contract_size == 1.0
                                        and spec.digits == 0)


# --------------------------------------------------------------------------- #
# Volume rounding / clamping
# --------------------------------------------------------------------------- #
def test_round_volume_snaps_down_to_the_broker_step():
    spec = SymbolSpec(symbol="EURUSDm", volume_step=0.01, volume_min=0.01,
                      volume_max=100.0)
    # Down, not nearest: rounding up would exceed the requested risk.
    assert spec.round_volume(0.019) == pytest.approx(0.01)
    assert spec.round_volume(1.239) == pytest.approx(1.23)


def test_round_volume_below_the_broker_minimum_skips_the_trade():
    """Sizing up to the minimum would silently risk more than RISK_PERCENT."""
    spec = SymbolSpec(symbol="X", volume_step=0.1, volume_min=0.1, volume_max=5.0)
    assert spec.round_volume(0.05) == 0.0   # cannot be taken at the asked risk
    assert spec.round_volume(0.1) == pytest.approx(0.1)   # exactly the minimum
    assert spec.round_volume(99.0) == pytest.approx(5.0)  # capped at the maximum


def test_position_size_returns_zero_when_the_trade_is_too_small_to_take():
    """The executor treats this as invalid_lot_size and records a SKIPPED row."""
    spec = SymbolSpec(symbol="XAUUSDm", contract_size=100.0, digits=2,
                      volume_min=0.10, volume_step=0.01)
    # A tiny account: $1 risk at $200 risk per lot rounds below the 0.10 minimum.
    assert position_size(100.0, 1.0, 2002.0, 2000.0, "buy", spec=spec) == 0.0


def test_round_volume_skips_unknown_constraints():
    """Zero means 'unknown', so nothing is rounded or clamped against a guess."""
    spec = SymbolSpec(symbol="X")
    assert spec.round_volume(1.23456789) == pytest.approx(1.23456789)


def test_round_price_uses_symbol_digits():
    assert SymbolSpec(symbol="X", digits=3).round_price(150.123456) == 150.123
    assert SymbolSpec(symbol="X", digits=0).round_price(150.123456) == 150.123456


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def test_position_size_without_spec_keeps_legacy_behaviour():
    """No spec must behave exactly as before the multi-asset change."""
    assert position_size(1000.0, 1.0, 101.0, 100.0, "buy") == pytest.approx(10.0)
    # And identically for a sell.
    assert position_size(1000.0, 1.0, 100.0, 101.0, "sell") == pytest.approx(10.0)


def test_position_size_forex_is_lots_not_units():
    """$10 risk on a 20-pip stop at $10/pip per lot is 0.05 lots — not 5,000."""
    spec = SymbolSpec(symbol="EURUSDm", contract_size=100000.0, digits=5,
                      volume_min=0.01, volume_step=0.01, volume_max=100.0,
                      tick_size=0.00001, tick_value=1.0)
    lots = position_size(1000.0, 1.0, 1.0020, 1.0000, "buy", spec=spec)
    assert lots == pytest.approx(0.05)


def test_position_size_jpy_pair_uses_tick_value_not_contract_size():
    """USDJPY risk is quoted in JPY; tick_value is already in account currency.

    The contract-size fallback would be wrong here (it would imply the account
    currency is JPY), which is exactly why tick data is preferred.
    """
    spec = SymbolSpec(symbol="USDJPYm", contract_size=100000.0, digits=3,
                      volume_min=0.01, volume_step=0.01, volume_max=100.0,
                      tick_size=0.001, tick_value=0.68)  # ~$0.68 per pip per lot
    # 30-pip stop: risk per lot = 300 ticks * $0.68 = $204; $100 risk => 0.49 lots.
    lots = position_size(10000.0, 1.0, 150.300, 150.000, "buy", spec=spec)
    assert lots == pytest.approx(0.49, abs=0.001)


def test_position_size_gold_uses_contract_size_when_no_tick_data():
    """100 oz per lot: a $2 move risks $200 per lot, so $100 is 0.5 lots."""
    spec = SymbolSpec(symbol="XAUUSDm", contract_size=100.0, digits=2,
                      volume_min=0.01, volume_step=0.01, volume_max=50.0)
    lots = position_size(10000.0, 1.0, 2002.0, 2000.0, "buy", spec=spec)
    assert lots == pytest.approx(0.5)


def test_position_size_rounds_to_the_broker_step():
    spec = SymbolSpec(symbol="X", contract_size=100000.0, volume_step=0.01,
                      volume_min=0.01)
    lots = position_size(1000.0, 1.0, 1.0020, 1.0000, "buy", spec=spec)
    assert lots == pytest.approx(0.05)
    assert round(lots, 8) == lots


def test_position_size_rejects_invalid_geometry_either_way():
    spec = SymbolSpec(symbol="X", contract_size=100000.0)
    assert position_size(1000.0, 1.0, 100.0, 101.0, "buy", spec=spec) == 0.0
    assert position_size(0.0, 1.0, 101.0, 100.0, "buy", spec=spec) == 0.0


def test_risk_manager_carries_its_spec_into_sizing():
    gold = SymbolSpec(symbol="XAUUSDm", contract_size=100.0, digits=2,
                      volume_min=0.01, volume_step=0.01)
    risk = RiskManager(min_rr=1.5, risk_percent=1.0, spec=gold)
    # No override given, so the manager's own spec applies:
    # $100 risk at $200 risk per lot (100 oz contract).
    assert risk.size_position(10000.0, 2002.0, 2000.0, "buy") == pytest.approx(0.5)

    # An explicit spec overrides the manager's: an index contract risks $2 per
    # lot over the same $2 stop, so the same dollars buy 100x the size.
    index = SymbolSpec(symbol="USTEC", contract_size=1.0, volume_min=0.01,
                       volume_step=0.01)
    assert risk.size_position(10000.0, 2002.0, 2000.0, "buy",
                              spec=index) == pytest.approx(50.0)
