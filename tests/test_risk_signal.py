"""Risk manager + signal engine validation tests."""
from datetime import datetime

from trading.risk_manager import RiskManager, compute_rr, position_size
from trading.signal_engine import (
    RecentSignals,
    SetupCandidate,
    candidate_to_signal,
    validate_candidate,
)

VALID = ["london_open", "ny_am", "london_close", "ny_pm", "power_hour"]
T = datetime(2026, 1, 6, 9, 10)


def _candidate(**kw):
    base = dict(
        asset="TEST",
        direction="buy",
        entry_price=101.7,
        entry_time_utc=T,
        entry_time_ny=T,
        sl=99.5,
        tp=106.0,
        session_keys=["ny_am"],
        session_primary="ny_am",
        silver_bullet=None,
        macro=None,
        liquidity_type="PDL",
        liquidity_price=100.0,
        purge_time_ny=T,
        cisd_tf="M15",
        cisd_confirm_time_ny=T,
        fvg_direction="bullish",
        fvg_lower=101.4,
        fvg_upper=101.55,
    )
    base.update(kw)
    return SetupCandidate(**base)


def test_compute_rr():
    # buy: risk 2.2 (101.7->99.5), reward 4.3 (101.7->106.0) => rr ~1.955
    assert round(compute_rr(101.7, 99.5, 106.0, "buy"), 3) == 1.955
    assert compute_rr(101.7, 99.5, 106.0, "sell") == 0.0  # inverted geometry
    assert compute_rr(100.0, 100.0, 110.0, "buy") == 0.0


def test_risk_approve_reject_below_min():
    rm = RiskManager(min_rr=1.5)
    ok = rm.approve(101.7, 99.5, 106.0, "buy")   # rr 2.0
    assert ok.approved and ok.rr >= 1.5
    bad = rm.approve(101.7, 101.0, 102.0, "buy")  # rr ~0.4
    assert not bad.approved and bad.reason == "rr_too_low"


def test_position_size():
    # 1000 equity, 1% risk => $10 at risk; risk per unit = 1 => 10 units.
    size = position_size(1000.0, 1.0, 101.0, 100.0, "buy")
    assert round(size, 4) == 10.0


def test_candidate_approved():
    rm = RiskManager(min_rr=1.5)
    sig = candidate_to_signal(_candidate(), VALID, rm)
    assert sig.status == "APPROVED"
    assert sig.risk_approved is True
    assert sig.alert_only is True


def test_candidate_rejected_wrong_session():
    rm = RiskManager(min_rr=1.5)
    sig = candidate_to_signal(_candidate(session_keys=["asian_range"]), VALID, rm)
    assert sig.status == "REJECTED"
    assert "session_not_tradable" in sig.reason


def test_candidate_rejected_rr_too_low():
    rm = RiskManager(min_rr=1.5)
    sig = candidate_to_signal(_candidate(sl=99.0, tp=102.0), VALID, rm)
    assert sig.status == "REJECTED"
    assert "rr_too_low" in sig.reason


def test_validate_candidate_missing_fields():
    probs = validate_candidate(_candidate(fvg_direction=""), 1.5, VALID)
    assert "no_fvg" in probs


def test_fingerprint_stable_and_differentiating():
    rm = RiskManager(min_rr=1.5)
    s1 = candidate_to_signal(_candidate(), VALID, rm)
    s2 = candidate_to_signal(_candidate(), VALID, rm)
    s3 = candidate_to_signal(_candidate(liquidity_price=99.0), VALID, rm)
    assert s1.fingerprint() == s2.fingerprint()
    assert s1.fingerprint() != s3.fingerprint()


def test_recent_signals_dedupes():
    store = RecentSignals(window_minutes=10)
    rm = RiskManager(min_rr=1.5)
    s = candidate_to_signal(_candidate(), VALID, rm)
    assert not store.is_duplicate(s)
    store.mark(s)
    assert store.is_duplicate(s)


def test_ai_cannot_change_risk_fields():
    """AI fields live on the signal but never alter risk geometry."""
    rm = RiskManager(min_rr=1.5)
    sig = candidate_to_signal(_candidate(), VALID, rm)
    sig.ai_score = 95.0
    sig.ai_decision = "STRONG_BUY"
    # Original deterministic geometry is untouched by AI attributes.
    assert sig.entry == 101.7 and sig.sl == 99.5 and sig.tp == 106.0
