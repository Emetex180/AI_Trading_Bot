"""Asset manager.

Multi-asset support: a strategy name (e.g. ``USTEC``, ``GOLD``) is mapped to a
broker symbol (e.g. ``USTEC``, ``XAUUSDm``) via a JSON registry file so adding
or removing an asset never requires rewriting strategy code.

Example ``assets.json``::

    {"assets": [{"name": "GOLD", "broker_symbol": "XAUUSDm", "enabled": true, "digits": 2}]}

No symbol is hard-coded anywhere else in the codebase.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Settings, get_settings


class AssetRegistryError(Exception):
    """Raised for invalid/missing asset configuration."""


# Parsed registry payloads, keyed by ``(path, mtime)``. The dashboard builds an
# AssetManager several times per request (dropdowns, validation, the digits
# filter), and re-reading and re-parsing the JSON each time is pure waste. The
# mtime in the key means an edit is picked up on the next read.
#
# Only the *parsed JSON* is cached. Each AssetManager still builds its own
# ``Asset`` objects from it, so mutating one manager can never reach another.
_registry_cache: dict[tuple[str, float], list[dict]] = {}


def _read_registry(path: Path) -> list[dict]:
    """The raw ``assets`` list from the registry file, cached on its mtime."""
    if not path.exists():
        raise AssetRegistryError(f"Asset registry not found: {path}")

    key = (str(path), path.stat().st_mtime)
    cached = _registry_cache.get(key)
    if cached is not None:
        return cached

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - config error
        raise AssetRegistryError(f"Invalid JSON in {path}: {exc}") from exc

    assets = raw.get("assets", [])
    if not isinstance(assets, list):
        raise AssetRegistryError("'assets' must be a list in the registry file")

    # Drop this file's older payloads, so a long-lived process that edits the
    # registry repeatedly does not accumulate dead copies.
    for stale in [k for k in _registry_cache if k[0] == key[0] and k != key]:
        del _registry_cache[stale]
    _registry_cache[key] = assets
    return assets


@dataclass
class Asset:
    """A configured tradable instrument."""

    name: str                  # strategy-facing name, never a hard-coded symbol
    broker_symbol: str         # symbol handed to MT5 (a *base* name if resolved)
    enabled: bool = True
    digits: int = 0
    overrides: dict[str, Any] = field(default_factory=dict)

    # Contract fallbacks, used only when the broker's own spec is unavailable
    # (offline backtest, smoke run). Live sessions read the real numbers from
    # MT5 — see :mod:`trading.symbol_spec`.
    contract_size: float = 1.0
    volume_min: float = 0.0
    volume_step: float = 0.0
    volume_max: float = 0.0

    # Populated at runtime from the connected terminal; never persisted, since
    # it describes one broker's contract rather than the user's configuration.
    spec: Any = None

    def settings_value(self, key: str, default: Any, settings: Settings | None = None) -> Any:
        """Resolve a per-asset override falling back to global Settings/env."""
        if key in self.overrides:
            value = self.overrides[key]
            # Env overrides arrive as strings; coerce booleans for ergonomics.
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "false"}:
                    return lowered == "true"
                try:
                    if "." in value:
                        return float(value)
                    return int(value)
                except ValueError:
                    return value
            return value
        return default

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "broker_symbol": self.broker_symbol,
            "enabled": self.enabled,
            "digits": self.digits,
            "contract_size": self.contract_size,
            "volume_min": self.volume_min,
            "volume_step": self.volume_step,
            "volume_max": self.volume_max,
            "overrides": dict(self.overrides),
        }


class AssetManager:
    """Loads and mutates the asset registry.

    The registry is persisted as JSON so configuration survives restarts and
    can be edited without touching strategy code.
    """

    def __init__(self, path: Path | None = None, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self.path = Path(path) if path is not None else self._settings.assets_file
        self._assets: dict[str, Asset] = {}
        self.load()

    # ---- loading -----------------------------------------------------------
    def load(self) -> None:
        registry: dict[str, Asset] = {}
        for item in _read_registry(self.path):
            name = str(item.get("name", "")).strip()
            symbol = str(item.get("broker_symbol", "")).strip()
            if not name or not symbol:
                raise AssetRegistryError(f"Each asset needs 'name' and 'broker_symbol': {item}")
            registry[name] = Asset(
                name=name,
                broker_symbol=symbol,
                enabled=bool(item.get("enabled", True)),
                digits=int(item.get("digits", 0)),
                overrides=dict(item.get("overrides", {})),
                contract_size=float(item.get("contract_size", 1.0) or 1.0),
                volume_min=float(item.get("volume_min", 0.0) or 0.0),
                volume_step=float(item.get("volume_step", 0.0) or 0.0),
                volume_max=float(item.get("volume_max", 0.0) or 0.0),
            )
        self._assets = registry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"assets": [asset.as_dict() for asset in self._assets.values()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # A write through this process must not leave a stale payload cached —
        # clearing outright also covers a file rewritten within the same mtime
        # tick, which a timestamp comparison alone would miss.
        _registry_cache.clear()

    # ---- queries -----------------------------------------------------------
    def list_assets(self) -> list[Asset]:
        return list(self._assets.values())

    def enabled_assets(self) -> list[Asset]:
        return [a for a in self._assets.values() if a.enabled]

    def get(self, name: str) -> Asset:
        try:
            return self._assets[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AssetRegistryError(f"Unknown asset: {name}") from exc

    def has(self, name: str) -> bool:
        return name in self._assets

    def broker_symbol(self, name: str) -> str:
        return self.get(name).broker_symbol

    def names(self) -> list[str]:
        return list(self._assets.keys())

    # ---- mutations ---------------------------------------------------------
    def add_asset(self, name: str, broker_symbol: str, enabled: bool = True,
                  digits: int = 0, overrides: dict[str, Any] | None = None,
                  contract_size: float = 1.0, volume_min: float = 0.0,
                  volume_step: float = 0.0, volume_max: float = 0.0,
                  persist: bool = True) -> Asset:
        asset = Asset(name=name, broker_symbol=broker_symbol, enabled=enabled,
                      digits=digits, overrides=dict(overrides or {}),
                      contract_size=contract_size, volume_min=volume_min,
                      volume_step=volume_step, volume_max=volume_max)
        self._assets[name] = asset
        if persist:
            self.save()
        return asset

    def remove_asset(self, name: str, persist: bool = True) -> bool:
        removed = self._assets.pop(name, None) is not None
        if removed and persist:
            self.save()
        return removed

    def set_enabled(self, name: str, enabled: bool, persist: bool = True) -> Asset:
        asset = self.get(name)
        asset.enabled = enabled
        if persist:
            self.save()
        return asset

    def set_all_enabled(self, enabled: bool, persist: bool = True) -> list[str]:
        """Enable/disable every asset at once; returns the names affected.

        The registry ships a broad watchlist mostly disabled, because switching
        on twenty instruments at once multiplies the alert volume twentyfold.
        This is the one-command way to opt the whole list in.
        """
        changed: list[str] = []
        for asset in self._assets.values():
            if asset.enabled != enabled:
                asset.enabled = enabled
                changed.append(asset.name)
        if changed and persist:
            self.save()
        return changed
