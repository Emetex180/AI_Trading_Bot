"""Asset manager tests — pure, no MT5."""
import json
import time

import pytest

from trading.asset_manager import AssetManager, AssetRegistryError


def _registry_file(tmp_path, assets):
    p = tmp_path / "assets.json"
    p.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    return p


def test_load_and_query(tmp_path):
    p = _registry_file(tmp_path, [
        {"name": "USTEC", "broker_symbol": "USTEC", "enabled": True, "digits": 2},
        {"name": "GOLD", "broker_symbol": "XAUUSDm", "enabled": False, "digits": 2},
    ])
    mgr = AssetManager(path=p)
    assert mgr.broker_symbol("USTEC") == "USTEC"
    assert mgr.broker_symbol("GOLD") == "XAUUSDm"
    enabled = [a.name for a in mgr.enabled_assets()]
    assert enabled == ["USTEC"]


def test_add_remove_persist(tmp_path):
    p = _registry_file(tmp_path, [{"name": "USTEC", "broker_symbol": "USTEC"}])
    mgr = AssetManager(path=p)
    mgr.add_asset("BTCUSD", "BTCUSD", enabled=True)
    assert mgr.has("BTCUSD")
    # Reload from disk proves persistence.
    mgr2 = AssetManager(path=p)
    assert mgr2.has("BTCUSD")
    assert mgr2.remove_asset("BTCUSD")
    mgr3 = AssetManager(path=p)
    assert not mgr3.has("BTCUSD")


def test_contract_fields_load_and_default(tmp_path):
    """Contract sizing fields are optional; an entry without them stays valid."""
    p = _registry_file(tmp_path, [
        {"name": "EURUSD", "broker_symbol": "EURUSD", "enabled": True,
         "digits": 5, "contract_size": 100000, "volume_min": 0.01,
         "volume_step": 0.01, "volume_max": 100},
        {"name": "USTEC", "broker_symbol": "USTEC", "enabled": True, "digits": 2},
    ])
    mgr = AssetManager(path=p)

    fx = mgr.get("EURUSD")
    assert fx.contract_size == 100000.0
    assert fx.volume_min == 0.01
    assert fx.volume_step == 0.01
    assert fx.volume_max == 100.0

    # A plain entry keeps the legacy index-CFD assumption and "unknown" limits.
    idx = mgr.get("USTEC")
    assert idx.contract_size == 1.0
    assert idx.volume_min == 0.0
    assert idx.volume_step == 0.0
    assert idx.volume_max == 0.0


def test_contract_fields_round_trip_through_save(tmp_path):
    p = _registry_file(tmp_path, [
        {"name": "XAUUSD", "broker_symbol": "XAUUSD", "enabled": True,
         "digits": 2, "contract_size": 100, "volume_min": 0.01,
         "volume_step": 0.01},
    ])
    mgr = AssetManager(path=p)
    mgr.set_enabled("XAUUSD", True)          # triggers a save
    reloaded = AssetManager(path=p).get("XAUUSD")
    assert reloaded.contract_size == 100.0
    assert reloaded.volume_min == 0.01
    assert reloaded.volume_step == 0.01


def test_set_all_enabled_loads_the_whole_watchlist(tmp_path):
    """The registry ships a broad list mostly off; this opts it all in at once."""
    p = _registry_file(tmp_path, [
        {"name": "USTEC", "broker_symbol": "USTEC", "enabled": True},
        {"name": "EURUSD", "broker_symbol": "EURUSD", "enabled": False},
        {"name": "XAUUSD", "broker_symbol": "XAUUSD", "enabled": False},
    ])
    mgr = AssetManager(path=p)
    assert [a.name for a in mgr.enabled_assets()] == ["USTEC"]

    changed = mgr.set_all_enabled(True)
    assert sorted(changed) == ["EURUSD", "XAUUSD"]        # only what flipped
    assert len(mgr.enabled_assets()) == 3
    assert len(AssetManager(path=p).enabled_assets()) == 3  # persisted

    assert sorted(mgr.set_all_enabled(False)) == ["EURUSD", "USTEC", "XAUUSD"]
    assert mgr.enabled_assets() == []


def test_missing_file_raises(tmp_path):
    with pytest.raises(AssetRegistryError):
        AssetManager(path=tmp_path / "nope.json")


def test_no_hardcoded_symbol_defaults(tmp_path):
    """A fresh registry must NOT assume USTEC unless configured."""
    p = _registry_file(tmp_path, [{"name": "EURUSD", "broker_symbol": "EURUSD.a"}])
    mgr = AssetManager(path=p)
    assert mgr.names() == ["EURUSD"]


def test_overrides_and_env_style_coercion(tmp_path):
    p = _registry_file(tmp_path, [{
        "name": "GOLD",
        "broker_symbol": "XAUUSDm",
        "overrides": {"sl_buffer_atr": "0.5"},
    }])
    mgr = AssetManager(path=p)
    assert mgr.get("GOLD").settings_value("sl_buffer_atr", 0.25) == 0.5
    assert mgr.get("GOLD").settings_value("min_rr", 1.5) == 1.5


# --------------------------------------------------------------------------- #
# The registry read cache
#
# The dashboard builds an AssetManager several times per request (dropdowns,
# validation, the price-precision filter). Only the parsed JSON is cached —
# never the Asset objects — so one caller mutating its manager cannot reach
# another's.
# --------------------------------------------------------------------------- #
def test_two_managers_never_share_asset_objects(tmp_path):
    p = _registry_file(tmp_path, [
        {"name": "XAUUSD", "broker_symbol": "XAUUSD", "enabled": True},
    ])

    first = AssetManager(path=p)
    second = AssetManager(path=p)

    first.get("XAUUSD").enabled = False   # mutate one manager's copy

    assert second.get("XAUUSD").enabled is True
    assert AssetManager(path=p).get("XAUUSD").enabled is True


def test_an_external_edit_is_picked_up(tmp_path):
    """The cache is keyed on mtime, so a rewritten file is never read stale."""
    p = _registry_file(tmp_path, [
        {"name": "XAUUSD", "broker_symbol": "XAUUSD", "enabled": True},
    ])
    assert AssetManager(path=p).get("XAUUSD").enabled is True

    time.sleep(0.01)  # guarantee a different mtime
    _registry_file(tmp_path, [
        {"name": "XAUUSD", "broker_symbol": "XAUUSD", "enabled": False},
    ])

    assert AssetManager(path=p).get("XAUUSD").enabled is False


def test_a_write_through_a_manager_is_visible_immediately(tmp_path):
    """A save inside one mtime tick must not be masked by the cache."""
    p = _registry_file(tmp_path, [
        {"name": "USTEC", "broker_symbol": "USTEC", "enabled": True},
    ])
    assert AssetManager(path=p).has("USTEC")

    AssetManager(path=p).add_asset("BTC", "BTCUSD", enabled=True)

    assert AssetManager(path=p).has("BTC")
    assert set(AssetManager(path=p).names()) == {"USTEC", "BTC"}
