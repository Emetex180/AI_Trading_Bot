"""Signal assembly, validation and duplicate prevention.

A :class:`Signal` is the single structured object that flows to AI, Telegram,
the database and the backtester. Setups are assembled into candidates by the
strategy engine and validated here (deterministic rules first, risk manager
second — AI is never allowed to override either).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import time_utils as tu
from .risk_manager import RiskDecision, RiskManager, compute_rr

# Signal lifecycle states.
PENDING = "PENDING"            # deterministic rules + risk passed, awaiting AI
AI_UNAVAILABLE = "AI_UNAVAILABLE"
APPROVED = "APPROVED"
REJECTED = "REJECTED"          # deterministic rule / risk rejected the setup
SENT = "SENT"


@dataclass
class SetupCandidate:
    """Everything the strategy detected; feeds final validation."""

    asset: str
    direction: str                     # "buy" | "sell"
    entry_price: float
    entry_time_utc: datetime
    entry_time_ny: datetime
    sl: float
    tp: float
    session_keys: list[str] = field(default_factory=list)
    session_primary: str = ""
    silver_bullet: str | None = None
    macro: str | None = None
    liquidity_type: str = ""           # PDL / PDH / EQ_LOW / ...
    liquidity_price: float = 0.0
    purge_time_ny: datetime | None = None
    cisd_tf: str = ""
    cisd_confirm_time_ny: datetime | None = None
    fvg_direction: str = ""
    fvg_lower: float = 0.0
    fvg_upper: float = 0.0
    fvg_formation_time_ny: datetime | None = None
    structure_extreme_price: float = 0.0
    structure_time_ny: datetime | None = None


@dataclass
class Signal:
    asset: str
    direction: str
    entry: float
    sl: float
    tp: float
    entry_time_utc: datetime
    entry_time_ny: datetime
    session_keys: list[str] = field(default_factory=list)
    session_primary: str = ""
    silver_bullet: str | None = None
    macro: str | None = None
    liquidity_type: str = ""
    liquidity_price: float = 0.0
    purge_time_ny: datetime | None = None
    cisd_tf: str = ""
    cisd_confirm_time_ny: datetime | None = None
    fvg_direction: str = ""
    fvg_lower: float = 0.0
    fvg_upper: float = 0.0
    fvg_formation_time_ny: datetime | None = None
    structure_extreme_price: float = 0.0
    rr: float = 0.0
    status: str = PENDING
    reason: str = ""                   # rejection reason when status == REJECTED
    ai_status: str | None = None
    ai_score: float | None = None
    ai_decision: str | None = None
    ai_reasoning: str | None = None
    ai_confidence: float | None = None
    ai_strengths: list[str] = field(default_factory=list)
    ai_risks: list[str] = field(default_factory=list)
    alert_only: bool = True
    risk_approved: bool = False

    def fingerprint(self) -> str:
        """Deterministic unique key used to prevent duplicate alerts/trades.

        Binds to asset + direction + entry minute + the liquidity level that
        defined the setup — two evaluations of the same closed M1 entry candle
        collapse to one key.
        """
        base = (
            self.asset, self.direction,
            self.entry_time_utc.strftime("%Y-%m-%dT%H:%M"),
            f"{self.liquidity_price:.5f}",
            self.cisd_tf,
        )
        raw = "|".join(str(x) for x in base)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def validate_candidate(cand: SetupCandidate, min_rr: float,
                       valid_entry_sessions: list[str],
                       reject_non_target_sessions: bool = True) -> list[str]:
    """Deterministic rule checks. Returns a list of violation reasons (empty = OK)."""
    problems: list[str] = []
    if cand.direction not in {"buy", "sell"}:
        problems.append("invalid_direction")
    if cand.entry_price <= 0 or cand.sl <= 0 or cand.tp <= 0:
        problems.append("invalid_prices")
    if not cand.session_keys:
        problems.append("no_session")
    if reject_non_target_sessions:
        allowed = set(valid_entry_sessions)
        if not allowed.intersection(set(cand.session_keys)):
            problems.append("session_not_tradable")
    if not cand.cisd_tf:
        problems.append("no_cisd")
    if not cand.fvg_direction:
        problems.append("no_fvg")
    if cand.liquidity_price <= 0:
        problems.append("no_liquidity")
    if compute_rr(cand.entry_price, cand.sl, cand.tp, cand.direction) < min_rr:
        problems.append("rr_too_low")
    return problems


def candidate_to_signal(cand: SetupCandidate, valid_entry_sessions: list[str],
                        risk: RiskManager,
                        allow_session_gate: bool = True) -> Signal:
    """Validate a candidate and emit a :class:`Signal` (status REJECTED/APPROVED)."""
    problems = validate_candidate(
        cand,
        min_rr=risk.min_rr,
        valid_entry_sessions=valid_entry_sessions,
        reject_non_target_sessions=allow_session_gate,
    )
    rr = compute_rr(cand.entry_price, cand.sl, cand.tp, cand.direction)

    signal = Signal(
        asset=cand.asset,
        direction=cand.direction,
        entry=cand.entry_price,
        sl=cand.sl,
        tp=cand.tp,
        entry_time_utc=cand.entry_time_utc,
        entry_time_ny=cand.entry_time_ny,
        session_keys=list(cand.session_keys),
        session_primary=cand.session_primary,
        silver_bullet=cand.silver_bullet,
        macro=cand.macro,
        liquidity_type=cand.liquidity_type,
        liquidity_price=cand.liquidity_price,
        purge_time_ny=cand.purge_time_ny,
        cisd_tf=cand.cisd_tf,
        cisd_confirm_time_ny=cand.cisd_confirm_time_ny,
        fvg_direction=cand.fvg_direction,
        fvg_lower=cand.fvg_lower,
        fvg_upper=cand.fvg_upper,
        fvg_formation_time_ny=cand.fvg_formation_time_ny,
        structure_extreme_price=cand.structure_extreme_price,
        rr=rr,
    )
    if problems:
        signal.status = REJECTED
        signal.reason = ",".join(problems)
        return signal

    # Risk manager is the final gate and cannot be overridden later.
    decision: RiskDecision = risk.approve(cand.entry_price, cand.sl, cand.tp, cand.direction)
    signal.risk_approved = decision.approved
    if not decision.approved:
        signal.status = REJECTED
        signal.reason = decision.reason
    else:
        signal.status = APPROVED  # AI still to be run by the caller
    return signal


class RecentSignals:
    """In-memory duplicate guard (mirrors the DB uniqueness on fingerprint)."""

    def __init__(self, window_minutes: int = 480):
        self.window_minutes = window_minutes
        self._seen: dict[str, datetime] = {}

    def is_duplicate(self, signal: Signal, now: datetime | None = None) -> bool:
        now = now or tu.now_utc()
        key = signal.fingerprint()
        if key in self._seen:
            return True
        # Housekeeping: drop entries older than the window.
        cutoff = now - timedelta(minutes=self.window_minutes)
        for k in [k for k, v in self._seen.items() if v < cutoff]:
            del self._seen[k]
        return False

    def mark(self, signal: Signal, now: datetime | None = None) -> None:
        now = now or tu.now_utc()
        self._seen[signal.fingerprint()] = now
