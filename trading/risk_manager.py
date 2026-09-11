"""Risk manager.

The risk manager is the **authority** on whether a setup may be traded: AI can
score and comment, but it can never override the risk manager. A setup that the
risk manager rejects is never alerted as valid.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Settings, get_settings

from .symbol_spec import SymbolSpec


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str  # short machine reason e.g. "rr_too_low"; empty when approved
    rr: float = 0.0
    risk_per_trade: float = 0.0
    reward_per_trade: float = 0.0


def compute_rr(entry: float, sl: float, tp: float, direction: str) -> float:
    """Reward:risk ratio. Returns 0.0 when the geometry is invalid."""
    if direction == "buy":
        risk = entry - sl
        reward = tp - entry
    elif direction == "sell":
        risk = sl - entry
        reward = entry - tp
    else:
        raise ValueError(f"direction must be buy/sell, got {direction!r}")
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def position_size(equity: float, risk_percent: float, entry: float, sl: float,
                  direction: str, spec: SymbolSpec | None = None) -> float:
    """Position size in **lots**, sized for ``risk_percent``% of ``equity``.

    ``spec`` is the broker's contract specification for the instrument
    (:mod:`trading.symbol_spec`). It is what makes the result correct across
    asset classes: 1.0 lot is 100,000 units of EURUSD but one index contract of
    USTEC, so the same dollar risk is a very different number of lots.

    When ``spec`` is ``None`` or cannot describe the instrument, this falls back
    to the original index-CFD assumption (1 unit = $1 per 1.0 of price) rather
    than inventing a contract size — an unknown spec must never silently multiply
    or divide the traded size.
    """
    if direction == "buy":
        risk_per_unit = entry - sl
    else:
        risk_per_unit = sl - entry
    if risk_per_unit <= 0 or equity <= 0:
        return 0.0

    dollars_at_risk = equity * (risk_percent / 100.0)

    if spec is None:
        return dollars_at_risk / risk_per_unit

    risk_per_lot = spec.risk_per_lot(risk_per_unit)
    if risk_per_lot <= 0:
        # Spec present but unusable — behave exactly like "no spec".
        return dollars_at_risk / risk_per_unit

    return spec.round_volume(dollars_at_risk / risk_per_lot)


class RiskManager:
    """Validates RR geometry and applies position-sizing policy."""

    def __init__(self, min_rr: float | None = None, risk_percent: float | None = None,
                 settings: Settings | None = None, spec: SymbolSpec | None = None):
        cfg = settings or get_settings()
        self.min_rr = float(min_rr) if min_rr is not None else cfg.min_rr
        self.risk_percent = float(risk_percent) if risk_percent is not None else cfg.risk_percent
        # Broker contract specification for the instrument being traded. ``None``
        # keeps the legacy index-CFD sizing (see :func:`position_size`).
        self.spec = spec

    def approve(self, entry: float, sl: float, tp: float, direction: str) -> RiskDecision:
        """Reject a setup unless it satisfies the risk policy."""
        if direction not in {"buy", "sell"}:
            return RiskDecision(False, "invalid_direction")
        rr = compute_rr(entry, sl, tp, direction)
        if rr <= 0:
            return RiskDecision(False, "invalid_sl_tp", rr=rr)
        if rr < self.min_rr:
            return RiskDecision(False, "rr_too_low", rr=rr)
        risk_per_trade = abs(entry - sl)
        reward_per_trade = abs(tp - entry)
        return RiskDecision(True, "", rr=rr, risk_per_trade=risk_per_trade,
                            reward_per_trade=reward_per_trade)

    def size_position(self, equity: float, entry: float, sl: float, direction: str,
                      spec: SymbolSpec | None = None) -> float:
        return position_size(equity, self.risk_percent, entry, sl, direction,
                             spec if spec is not None else self.spec)
