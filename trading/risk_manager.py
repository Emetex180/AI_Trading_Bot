"""Risk manager.

The risk manager is the **authority** on whether a setup may be traded: AI can
score and comment, but it can never override the risk manager. A setup that the
risk manager rejects is never alerted as valid.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Settings, get_settings

from .liquidity import GRADE_RANK, VERY_HIGH
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
    risk, reward = risk_reward_points(entry, sl, tp, direction)
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def risk_reward_points(entry: float, sl: float, tp: float,
                       direction: str) -> tuple[float, float]:
    """``(risk, reward)`` in **price units** for a trade's geometry.

    The same numbers the RR ratio is built from, exposed because the alert has
    to print them ("Risk: 50 pts / Reward: 120 pts") and recomputing them in the
    formatter would be a second, divergent definition of risk.
    """
    if direction == "buy":
        return entry - sl, tp - entry
    if direction == "sell":
        return sl - entry, entry - tp
    raise ValueError(f"direction must be buy/sell, got {direction!r}")


# --------------------------------------------------------------------------- #
# Trade efficiency score
# --------------------------------------------------------------------------- #
#: Grade -> 0..1 quality factor, derived from the single liquidity priority
#: table in :mod:`trading.liquidity` so the two can never drift apart.
GRADE_QUALITY: dict[str, float] = {
    grade: rank / GRADE_RANK[VERY_HIGH] for grade, rank in GRADE_RANK.items()
}


@dataclass(frozen=True)
class EfficiencyWeights:
    """Relative weights of the three efficiency components (normalised on use)."""

    rr: float = 0.5          # reward:risk, saturating at ``rr_target``
    liquidity: float = 0.3   # strength of the liquidity target
    distance: float = 0.2    # room to the target, in ATRs


def efficiency_score(*, rr: float, rr_target: float, liquidity_grade: str,
                     reward_points: float, atr: float,
                     atr_target_multiple: float,
                     weights: EfficiencyWeights | None = None) -> float:
    """A transparent 0-100 trade efficiency score.

    Three components, each normalised to 0..1, combined with configurable
    weights (normalised, so the weights need not sum to 1):

    ``rr``          ``min(rr / rr_target, 1)`` — the reward:risk actually offered.
    ``liquidity``   the target level's grade quality (VERY_HIGH 1.00,
                    HIGH 0.75, MEDIUM_HIGH 0.50, MEDIUM 0.25).
    ``distance``    ``min(reward / (atr * atr_target_multiple), 1)`` — whether
                    the move to the target is at least that many ATRs of room.
                    Contributes 0 when ATR is unknown (never a guess).

    The formula is deliberately arithmetic — no model, no hidden scaling — so a
    score can be recomputed by hand from the alert's own numbers.
    """
    w = weights or EfficiencyWeights()
    total = w.rr + w.liquidity + w.distance
    if total <= 0:
        return 0.0

    rr_component = min(max(rr / rr_target, 0.0), 1.0) if rr_target > 0 else 0.0
    liq_component = GRADE_QUALITY.get((liquidity_grade or "").upper(), 0.0)
    if atr > 0 and atr_target_multiple > 0:
        dist_component = min(max(reward_points / (atr * atr_target_multiple), 0.0), 1.0)
    else:
        dist_component = 0.0

    score = (w.rr * rr_component + w.liquidity * liq_component
             + w.distance * dist_component) / total
    return round(score * 100.0, 2)


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
