"""Asset manager tests — pure, no MT5."""
import json

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
