"""AI analysis layer for deterministic signals.

Role and boundaries (hard constraints — must never be relaxed)
--------------------------------------------------------------
* AI analysis is a **read-only advisory overlay** on a signal that the
  deterministic ICT strategy has already validated and the risk manager has
  already approved. It never alters ``entry`` / ``sl`` / ``tp`` / ``direction``,
  never re-runs the session or RR gates, and never resurrects a setup the
  deterministic engine marked invalid.
* A failed / unreachable / unparseable AI response does **not** downgrade the
  deterministic approval into an execution risk — instead the signal is marked
  ``ai_status="AI_UNAVAILABLE"``. Depending on the caller's policy that grade is
  alertable but not tradeable, which keeps AI absent from the risk path.
* When AI is disabled in settings the analyzer returns ``available=False`` with
  ``disabled=True`` and the signal keeps its deterministic ``APPROVED`` status.
* The network transport is injected (``transport``) so unit tests never touch
  the network; production uses an OpenAI-compatible chat-completions endpoint.

AI is never allowed to *increase* risk. ``ai_score`` and ``ai_decision`` are
advisory labels; only the risk manager may approve a trade.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable

import requests  # type: ignore

from config import Settings, get_settings
from trading import time_utils as tu
from trading.signal_engine import Signal

AI_DISABLED = "DISABLED"
AI_ANALYZED = "ANALYZED"
AI_UNAVAILABLE = "AI_UNAVAILABLE"

# Which AI decisions are net-positive for a given trade direction.
POSITIVE = {"STRONG_BUY": "buy", "BUY": "buy"}
NEGATIVE = {"STRONG_SELL": "sell", "SELL": "sell"}


@dataclass(frozen=True)
class AiAnalysis:
    """Result of analysing one signal. ``available=False`` means no AI verdict."""

    available: bool
    score: float = 0.0                 # 0..100 net conviction for the setup
    decision: str = ""                 # STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL
    reasoning: str = ""
    strengths: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    confidence: float = 0.0
    error: str = ""
    disabled: bool = False
    model: str = ""
    analyzed_at_utc: datetime | None = None


def _transport_openai(url: str, api_key: str, payload: dict,
                      timeout: float) -> dict:
    """POST an OpenAI-compatible chat request and return the JSON body."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _snapshot_signal(signal: Signal) -> dict:
    """Deterministic facts exposed to the model (no risk fields are mutable)."""
    return {
        "asset": signal.asset,
        "direction": signal.direction,
        "entry_time_ny": signal.entry_time_ny.isoformat(),
        "session_primary": signal.session_primary,
        "session_keys": signal.session_keys,
        "silver_bullet": signal.silver_bullet,
        "macro": signal.macro,
        "liquidity_type": signal.liquidity_type,
        "liquidity_price": signal.liquidity_price,
        "cisd_tf": signal.cisd_tf,
        "cisd_confirm_time_ny": signal.cisd_confirm_time_ny.isoformat()
        if signal.cisd_confirm_time_ny else None,
        "fvg_direction": signal.fvg_direction,
        "fvg_zone": [signal.fvg_lower, signal.fvg_upper],
        "rr": round(signal.rr, 2),
    }


def _build_prompt(snapshot: dict) -> str:
    return (
        "You are a conservative ICT (Inner Circle Trader) risk analyst reviewing a "
        "signal that a deterministic engine has ALREADY validated and a risk manager "
        "has ALREADY approved for RR. Do NOT restate entry/sl/tp. Do NOT suggest "
        "changing them. Give a short qualitative read and a net conviction score.\n"
        "Respond with ONLY a JSON object:\n"
        '{"score": <0-100>, "decision": <"STRONG_BUY"|"BUY"|"NEUTRAL"|"SELL"|'
        '"STRONG_SELL">, "reasoning": "<1-3 sentences>", '
        '"strengths": ["..."], "risks": ["..."], "confidence": <0-100>}\n'
        f"Signal snapshot: {json.dumps(snapshot, default=str)}"
    )


class AiAnalyzer:
    """Analyse one signal through an injected transport (LLM or stub)."""

    def __init__(self, settings: Settings | None = None,
                 transport: Callable[..., dict] | None = None,
                 model: str | None = None, url: str | None = None,
                 api_key: str | None = None, timeout: float | None = None):
        cfg = settings or get_settings()
        self.settings = cfg
        self.model = model or cfg.llm_model
        self.url = url or cfg.llm_api_url
        self.api_key = api_key if api_key is not None else cfg.llm_api_key
        self.timeout = timeout if timeout is not None else cfg.llm_timeout_seconds
        self.transport = transport or (
            lambda payload: _transport_openai(self.url, self.api_key, payload,
                                              self.timeout))

    @property
    def enabled(self) -> bool:
        return self.settings.ai_enabled

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #
    def analyze(self, signal: Signal) -> AiAnalysis:
        """Return an AI read on ``signal`` (never raises for network issues)."""
        if not self.enabled:
            return AiAnalysis(available=False, disabled=True,
                              error="ai_disabled",
                              analyzed_at_utc=tu.now_utc())
        snapshot = _snapshot_signal(signal)
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system",
                 "content": "You output strict JSON only, no prose."},
                {"role": "user", "content": _build_prompt(snapshot)},
            ],
        }
        try:
            body = self.transport(payload)
            content = self._extract_content(body)
            parsed = json.loads(content)
            analysis = self._coerce(parsed)
        except Exception as exc:  # network, HTTP, JSON — all treated the same
            return AiAnalysis(available=False, error=f"ai_unavailable:{exc}",
                              analyzed_at_utc=tu.now_utc())
        return replace(analysis, model=self.model, analyzed_at_utc=tu.now_utc())

    # ------------------------------------------------------------------ #
    # Parsing helpers (pure, unit-testable)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_content(body: dict) -> str:
        """Pull the assistant text from an OpenAI-compatible response."""
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"unexpected LLM response shape: {exc}") from exc

    @staticmethod
    def _coerce(raw: dict) -> AiAnalysis:
        """Coerce a parsed JSON object into an AiAnalysis with safe defaults."""
        score = AiAnalyzer._clamp_number(raw.get("score"), 0.0, 0.0, 100.0)
        confidence = AiAnalyzer._clamp_number(raw.get("confidence"), 0.0, 0.0, 100.0)
        decision = str(raw.get("decision", "NEUTRAL")).upper()
        if decision not in {"STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"}:
            decision = "NEUTRAL"
        strengths = [str(x) for x in (raw.get("strengths") or [])][:5]
        risks = [str(x) for x in (raw.get("risks") or [])][:5]
        reasoning = str(raw.get("reasoning") or "")[:800]
        return AiAnalysis(available=True, score=score, decision=decision,
                          reasoning=reasoning, strengths=strengths,
                          risks=risks, confidence=confidence)

    @staticmethod
    def _clamp_number(value: Any, default: float, lo: float, hi: float) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))


# --------------------------------------------------------------------------- #
# Applying AI output to a Signal (one-way, never mutates risk fields)
# --------------------------------------------------------------------------- #
def apply_ai(signal: Signal, analysis: AiAnalysis) -> None:
    """Attach an analysis to ``signal`` in place.

    Only ``ai_*`` attributes are written. Entry/SL/TP/direction/status are left
    untouched by the analysis itself; the caller decides any status transition
    from ``analysis.available``.
    """
    signal.ai_score = analysis.score if analysis.available else None
    signal.ai_decision = analysis.decision if analysis.available else None
    signal.ai_reasoning = analysis.reasoning if analysis.available else None
    signal.ai_confidence = analysis.confidence if analysis.available else None
    signal.ai_strengths = list(analysis.strengths)
    signal.ai_risks = list(analysis.risks)
    if analysis.disabled:
        signal.ai_status = AI_DISABLED
    elif analysis.available:
        signal.ai_status = AI_ANALYZED
    else:
        signal.ai_status = AI_UNAVAILABLE
