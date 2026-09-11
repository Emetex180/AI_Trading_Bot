"""Broker contract specifications, per instrument.

The bot's position sizing must work for **any** instrument the broker offers,
and "1.0 lot" means something different in every asset class:

    EURUSD   1 lot = 100,000 base units   (~$10 per pip)
    XAUUSD   1 lot = 100 troy ounces
    USTEC    1 lot = 1 index contract     ($1 per index point)

:class:`SymbolSpec` is the small, immutable record of the numbers MT5 reports
for a symbol, so the risk manager can size in *lots* rather than in the
price-unit abstraction that only happened to be right for index CFDs.

Two sizing routes are supported, and the tick route is preferred when the
broker supplies it because it is already denominated in the **account**
currency — which is what makes JPY-quoted pairs (USDJPY, EURJPY) correct
without any FX conversion of our own:

    risk_per_lot = (price_risk / tick_size) * tick_value      # preferred
    risk_per_lot = price_risk * contract_size                 # fallback

A spec with ``contract_size == 1`` reproduces the legacy index-CFD behaviour
exactly, which is what a missing/unknown spec degrades to.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# MT5 filling-mode constants (mirrored so this module imports without the
# MetaTrader5 package installed).
FILLING_FOK = 0
FILLING_IOC = 1
FILLING_RETURN = 2

FILLING_NAMES = {
    FILLING_FOK: "FOK",
    FILLING_IOC: "IOC",
    FILLING_RETURN: "RETURN",
}


@dataclass(frozen=True)
class SymbolSpec:
    """What MT5 reports about one tradable symbol.

    Zero on ``volume_min``/``volume_step``/``volume_max`` means *unknown*: the
    broker did not report it (or we are offline), so rounding and clamping are
    skipped rather than applied against a guessed default. Silently clamping an
    order to an invented minimum would be worse than sending what was asked for.
    """

    symbol: str
    contract_size: float = 1.0
    volume_min: float = 0.0
    volume_step: float = 0.0
    volume_max: float = 0.0
    digits: int = 0
    tick_size: float = 0.0
    tick_value: float = 0.0
    filling_mode: int | None = None

    # ------------------------------------------------------------------ #
    # Constructors
    # ------------------------------------------------------------------ #
    @classmethod
    def from_mt5_info(cls, symbol: str, info: dict[str, Any] | None) -> "SymbolSpec":
        """Build a spec from :meth:`trading.mt5_client.MT5Client.symbol_info`.

        Unreadable or nonsensical fields degrade to their unknown defaults, so a
        partial broker response can never inject a bogus contract size into the
        sizing maths.
        """
        data = info or {}

        def num(key: str, default: float = 0.0) -> float:
            try:
                value = float(data.get(key))
            except (TypeError, ValueError):
                return default
            return value if math.isfinite(value) else default

        def positive(key: str, default: float) -> float:
            value = num(key, default)
            return value if value > 0 else default

        filling = data.get("filling_mode")
        try:
            filling_mode = int(filling) if filling is not None else None
        except (TypeError, ValueError):
            filling_mode = None

        return cls(
            symbol=symbol,
            contract_size=positive("trade_contract_size", 1.0),
            volume_min=max(num("volume_min"), 0.0),
            volume_step=max(num("volume_step"), 0.0),
            volume_max=max(num("volume_max"), 0.0),
            digits=int(num("digits", 0)),
            tick_size=max(num("tick_size"), 0.0),
            tick_value=max(num("tick_value"), 0.0),
            filling_mode=filling_mode,
        )

    @classmethod
    def from_asset(cls, asset: Any) -> "SymbolSpec":
        """Fallback spec from a registry entry, when MT5 is unavailable.

        Uses the per-asset ``contract_size``/``volume_*`` fields so a backtest or
        an offline smoke run still sizes realistically for asset classes the
        registry describes by hand.
        """
        def num(key: str, default: float) -> float:
            try:
                value = float(getattr(asset, key, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return cls(
            symbol=str(getattr(asset, "broker_symbol", "") or getattr(asset, "name", "")),
            contract_size=num("contract_size", 1.0),
            volume_min=num("volume_min", 0.0),
            volume_step=num("volume_step", 0.0),
            volume_max=num("volume_max", 0.0),
            digits=int(num("digits", 0)),
        )

    # ------------------------------------------------------------------ #
    # Sizing support
    # ------------------------------------------------------------------ #
    @property
    def has_tick_data(self) -> bool:
        """True when the broker gave us a usable tick size *and* value."""
        return self.tick_size > 0 and self.tick_value > 0

    def risk_per_lot(self, price_risk: float) -> float:
        """Money lost per 1.0 lot if price moves ``price_risk`` against us.

        Returns 0.0 when the spec cannot describe the instrument, which the risk
        manager treats as "unknown spec" and falls back to raw units.
        """
        if price_risk <= 0:
            return 0.0
        if self.has_tick_data:
            return (price_risk / self.tick_size) * self.tick_value
        if self.contract_size > 0:
            return price_risk * self.contract_size
        return 0.0

    def round_volume(self, lots: float) -> float:
        """Snap ``lots`` down to the broker's volume step and clamp to its range.

        Rounds *down* on purpose: rounding up could exceed the requested risk,
        and the volume step is a hard broker constraint — an unrounded volume is
        rejected outright (``Invalid volume``), not filled approximately.

        A size that rounds below the broker's minimum returns **0.0**, meaning
        "this trade cannot be taken within the requested risk". The caller skips
        it. Sizing up to the minimum would silently risk more than the configured
        ``RISK_PERCENT``, which is the one thing this module exists to prevent.
        """
        value = float(lots)
        if value <= 0:
            return 0.0
        if self.volume_step > 0:
            steps = math.floor(value / self.volume_step + 1e-9)
            value = steps * self.volume_step
        if self.volume_min > 0 and value < self.volume_min:
            return 0.0
        if self.volume_max > 0 and value > self.volume_max:
            value = self.volume_max
        return round(value, 8)

    def round_price(self, price: float) -> float:
        """Round a price to the symbol's digit precision (no-op when unknown)."""
        if self.digits <= 0:
            return float(price)
        return round(float(price), self.digits)

    def describe(self) -> str:
        """One-line human summary for logs and the dashboard."""
        return (f"{self.symbol}: contract={self.contract_size:g} "
                f"vol={self.volume_min:g}/{self.volume_step:g}/{self.volume_max:g} "
                f"digits={self.digits} "
                f"tick={self.tick_size:g}/{self.tick_value:g}")


def legacy_spec(symbol: str = "") -> SymbolSpec:
    """The pre-multi-asset assumption: 1 lot = $1 per 1.0 of price."""
    return SymbolSpec(symbol=symbol, contract_size=1.0)
