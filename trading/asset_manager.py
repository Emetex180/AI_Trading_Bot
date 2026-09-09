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


@dataclass
class Asset:
    """A configured tradable instrument."""

    name: str                  # strategy-facing name, never a hard-coded symbol
    broker_symbol: str         # symbol handed to MT5
    enabled: bool = True
    digits: int = 0
    overrides: dict[str, Any] = field(default_factory=dict)

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
        if not self.path.exists():
            raise AssetRegistryError(f"Asset registry not found: {self.path}")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - config error
            raise AssetRegistryError(f"Invalid JSON in {self.path}: {exc}") from exc

        assets = raw.get("assets", [])
        if not isinstance(assets, list):
            raise AssetRegistryError("'assets' must be a list in the registry file")

        registry: dict[str, Asset] = {}
        for item in assets:
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
            )
        self._assets = registry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"assets": [asset.as_dict() for asset in self._assets.values()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

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
                  persist: bool = True) -> Asset:
        asset = Asset(name=name, broker_symbol=broker_symbol, enabled=enabled,
                      digits=digits, overrides=dict(overrides or {}))
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
