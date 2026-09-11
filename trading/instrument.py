"""Turn a registry entry into something the terminal understands.

The registry holds *portable* base names (``XAUUSD``); a broker may call that
symbol ``XAUUSDm`` and attach its own contract numbers. Bridging the two is one
concern, and this is its single home — the live session, the backtest and the
history probe all resolve and size identically because they all come through
here rather than each doing it for themselves.

Layering: this module knows about the registry, the resolver and the contract
spec. It knows nothing about jobs, threads or MT5's process-global state, so it
is testable against a plain fake client.
"""
from __future__ import annotations

from dataclasses import replace

from .asset_manager import Asset
from .symbol_resolver import resolve_and_select
from .symbol_spec import SymbolSpec


def prepare_asset(client, asset: Asset,
                  resolve: bool) -> tuple[Asset | None, SymbolSpec | None]:
    """Resolve ``asset``'s broker symbol and attach the broker's contract spec.

    Registry names are portable base names (``XAUUSD``); the terminal may call
    that ``XAUUSDm``. Resolving here — once per job, inside the worker thread
    that owns MT5 — means everything downstream (history fetch, scanner, sizing)
    uses the broker's real spelling without any of it knowing about suffixes.

    Returns ``(asset, spec)``, or ``(None, None)`` when the broker does not offer
    the symbol. The returned asset is a copy, so the shared registry object is
    never mutated.
    """
    if not hasattr(client, "symbol_info"):
        # A client that cannot report symbol metadata (a test double, or a
        # minimal wrapper): trust the registry's own symbol rather than dropping
        # every asset. Sizing then uses the registry's contract fallback.
        spec = SymbolSpec.from_asset(asset)
        return replace(asset, spec=spec), spec

    requested = asset.broker_symbol
    symbol = requested
    if resolve:
        symbol = resolve_and_select(client, requested)
        if symbol is None:
            return None, None
    else:
        ensure = getattr(client, "ensure_symbol", None)
        if callable(ensure) and not ensure(requested):
            return None, None

    info = client.symbol_info(symbol)
    spec = SymbolSpec.from_mt5_info(symbol, info)

    # Fill in only the fields the broker did *not* report, from the registry
    # entry. A partial response then degrades to the registry's numbers rather
    # than to defaults, and a complete response is never overwritten.
    reported = info or {}

    def missing(key: str) -> bool:
        try:
            return float(reported.get(key) or 0) <= 0
        except (TypeError, ValueError):
            return True

    fallback = SymbolSpec.from_asset(asset)
    if missing("trade_contract_size"):
        spec = replace(spec, contract_size=fallback.contract_size)
    if missing("volume_step"):
        spec = replace(spec, volume_min=fallback.volume_min,
                       volume_step=fallback.volume_step,
                       volume_max=fallback.volume_max)
    if missing("digits"):
        spec = replace(spec, digits=fallback.digits)

    return replace(asset, broker_symbol=symbol, digits=spec.digits or asset.digits,
                   spec=spec), spec
