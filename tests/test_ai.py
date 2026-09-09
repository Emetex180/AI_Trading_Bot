"""AI analysis layer tests.

Verifies the hard boundary: AI output attaches to ai_* fields only; it can
never change risk geometry or resurrect a rejected setup, and failures produce
AI_UNAVAILABLE, not an execution error.
"""
from datetime import datetime
from types import SimpleNamespace

from ai.analyzer import (AI_ANALYZED, AI_DISABLED, AI_UNAVAILABLE, AiAnalysis,
                         AiAnalyzer, apply_ai)
from trading.signal_engine import Signal

OK_JSON = {
    "score": 82.0,
    "decision": "STRONG_BUY",
    "reasoning": "Clean reclaim of PDL with displacement.",
    "strengths": ["PDL sweep", "M15 confirm"],
    "risks": ["Late in session"],
    "confidence": 70.0,
}


def _signal(**kw):
    base = dict(
        asset="USTEC", direction="buy",
        entry=101.7, sl=99.5, tp=106.0,
        entry_time_utc=datetime(2026, 1, 6, 13, 10),
        entry_time_ny=datetime(2026, 1, 6, 9, 10),
        session_keys=["ny_am"], session_primary="ny_am",
        silver_bullet=None, macro="pre_ny_open",
        liquidity_type="PDL", liquidity_price=100.0,
        purge_time_ny=datetime(2026, 1, 6, 8, 0),
        cisd_tf="M15", cisd_confirm_time_ny=datetime(2026, 1, 6, 8, 30),
        fvg_direction="bullish", fvg_lower=101.4, fvg_upper=101.55,
        rr=2.0, status="APPROVED", alert_only=True, risk_approved=True,
    )
    base.update(kw)
    return Signal(**base)


def _settings(ai_enabled=True):
    return SimpleNamespace(
        ai_enabled=ai_enabled,
        llm_api_url="http://x", llm_api_key="k", llm_model="m",
        llm_timeout_seconds=10,
    )


def _ok_transport(payload):
    return {"choices": [{"message": {"content": __import__("json").dumps(OK_JSON)}}]}


def test_coerce_ok():
    a = AiAnalyzer._coerce(OK_JSON)
    assert a.available and a.score == 82.0
    assert a.decision == "STRONG_BUY"
    assert a.strengths and a.risks


def test_coerce_defensive_defaults():
    a = AiAnalyzer._coerce({"score": "oops", "decision": "hold", "confidence": 999})
    assert a.score == 0.0
    assert a.decision == "NEUTRAL"
    assert a.confidence == 100.0


def test_analyze_success_marks_analyzed():
    ex = AiAnalyzer(settings=_settings(), transport=_ok_transport)
    sig = _signal()
    res = ex.analyze(sig)
    assert res.available
    apply_ai(sig, res)
    assert sig.ai_status == AI_ANALYZED
    assert sig.ai_decision == "STRONG_BUY"
    # Risk geometry untouched.
    assert sig.entry == 101.7 and sig.sl == 99.5 and sig.tp == 106.0


def test_analyze_disabled():
    ex = AiAnalyzer(settings=_settings(ai_enabled=False))
    res = ex.analyze(_signal())
    assert res.disabled and not res.available
    sig = _signal()
    apply_ai(sig, res)
    assert sig.ai_status == AI_DISABLED


def test_analyze_network_failure_marks_unavailable():
    def boom(payload):
        raise ConnectionError("no network")

    ex = AiAnalyzer(settings=_settings(), transport=boom)
    sig = _signal()
    res = ex.analyze(sig)
    assert not res.available and res.error.startswith("ai_unavailable:")
    apply_ai(sig, res)
    assert sig.ai_status == AI_UNAVAILABLE
    # Even in failure the deterministic fields remain the authority.
    assert sig.rr == 2.0 and sig.status == "APPROVED"


def test_apply_ai_never_touches_risk_fields():
    sig = _signal()
    res = AiAnalysis(available=True, score=95.0, decision="STRONG_BUY")
    apply_ai(sig, res)
    assert sig.entry == 101.7 and sig.sl == 99.5 and sig.tp == 106.0
    assert sig.direction == "buy"
