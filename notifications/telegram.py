"""Telegram notifications (ALERT ONLY).

Every message is explicitly labelled an *alert*. Telegram is a one-way
broadcast channel used to inform — it never carries a trade instruction, never
confirms execution, and the notifier has no knowledge of the executor. Marking
is done both in the message body ("ALERT ONLY — not an execution request") and,
when enabled, with the dedicated ``send_message`` transport kept injectable so
tests never call the network.

The strategy may emit signals whose AI overlay is unavailable
(``ai_status == AI_UNAVAILABLE``); those are still alertable but flagged so a
reader never mistakes them for fully vetted trades.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import requests  # type: ignore

from config import Settings, get_settings
from trading.signal_engine import Signal

# Hard-coded safety suffix appended to every alert body.
ALERT_ONLY_TAG = "⚠️ ALERT ONLY — not an execution request. AUTO_TRADING gating applies."


@dataclass(frozen=True)
class SendResult:
    ok: bool
    http_status: int | None = None
    error: str = ""


def _transport_telegram(bot_token: str, chat_id: str, text: str,
                        timeout: float) -> SendResult:
    """POST to the Telegram Bot API sendMessage endpoint."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except Exception as exc:
        return SendResult(ok=False, error=f"transport_error:{exc}")
    if resp.status_code != 200:
        return SendResult(ok=False, http_status=resp.status_code,
                          error=f"telegram_http_{resp.status_code}")
    body = resp.json()
    if not body.get("ok"):
        return SendResult(ok=False, http_status=resp.status_code,
                          error=f"telegram_api:{body.get('description', 'unknown')}")
    return SendResult(ok=True, http_status=resp.status_code)


def format_signal_alert(signal: Signal) -> str:
    """Human-readable alert text for a signal (alert-only, no execution wording)."""
    emoji = "🟢 BUY" if signal.direction == "buy" else "🔴 SELL"
    lines = [
        f"{emoji} {signal.asset} — {signal.direction.upper()} ICT setup",
        "",
        f"Entry time (NY):  {signal.entry_time_ny.strftime('%Y-%m-%d %H:%M')}",
        f"Session:          {signal.session_primary or '-'.join(signal.session_keys)}",
        f"CISD tf:          {signal.cisd_tf or '-'}  (confirm {signal.cisd_confirm_time_ny.strftime('%H:%M') if signal.cisd_confirm_time_ny else '-'})",
        f"Liquidity:        {signal.liquidity_type or '-'} @ {signal.liquidity_price or '-'}",
        f"FVG:              {signal.fvg_direction or '-'} [{signal.fvg_lower or '-'}, {signal.fvg_upper or '-'}]",
        f"RR:               {signal.rr:.2f}",
        "",
    ]
    if signal.silver_bullet:
        lines.append(f"Silver Bullet window: {signal.silver_bullet}")
    if signal.macro:
        lines.append(f"Macro window:        {signal.macro}")

    # AI overlay is advisory; show it when present, flag it when missing.
    if signal.ai_status == "ANALYZED" and signal.ai_decision:
        lines.append("")
        conf = f", conf {signal.ai_confidence:.0f}" if signal.ai_confidence is not None else ""
        lines.append(f"AI read: {signal.ai_decision} (score {signal.ai_score:.0f}{conf})")
        if signal.ai_reasoning:
            lines.append(f"AI reasoning: {signal.ai_reasoning}")
    elif signal.ai_status == "AI_UNAVAILABLE":
        lines.append("")
        lines.append("AI analysis unavailable — deterministic signal only.")
    elif signal.ai_status == "DISABLED":
        lines.append("")
        lines.append("AI analysis disabled — deterministic signal only.")

    lines.append("")
    lines.append(ALERT_ONLY_TAG)
    return "\n".join(lines)


class TelegramNotifier:
    """Send ALERT-ONLY signals to a Telegram chat."""

    def __init__(self, settings: Settings | None = None,
                 transport: Callable[..., SendResult] | None = None):
        cfg = settings or get_settings()
        self.settings = cfg
        self.transport = transport or (
            lambda text: _transport_telegram(cfg.telegram_bot_token,
                                             cfg.telegram_chat_id, text,
                                             cfg.telegram_timeout_seconds))

    @property
    def enabled(self) -> bool:
        return self.settings.telegram_enabled and bool(self.settings.telegram_bot_token)

    def send_signal(self, signal: Signal) -> SendResult:
        """Send one ALERT-ONLY message for a signal. No-op when disabled."""
        if not self.enabled:
            return SendResult(ok=False, error="telegram_disabled")
        text = format_signal_alert(signal)
        return self.transport(text)

    def send_text(self, text: str) -> SendResult:
        """Send a generic ALERT-ONLY status message."""
        if not self.enabled:
            return SendResult(ok=False, error="telegram_disabled")
        return self.transport(text + f"\n\n{ALERT_ONLY_TAG}")
