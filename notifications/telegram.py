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
from trading.liquidity import LEVEL_LABELS
from trading.risk_manager import risk_reward_points
from trading.sessions import SESSION_INDEX
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


def _price(value: float | None, digits: int) -> str:
    """Format a price at the instrument's own precision.

    Falls back to 2 decimals when the signal carries no precision (a hand-built
    signal, or an asset the registry has no digits for) rather than printing a
    raw float.
    """
    if value is None:
        return "-"
    places = digits if digits and digits > 0 else 2
    return f"{value:.{places}f}"


def _points(value: float | None, digits: int) -> str:
    """Risk/reward expressed in price points, at the instrument's precision."""
    if value is None:
        return "-"
    return f"{_price(value, digits)} pts"


def _ny(ts) -> str:
    return ts.strftime("%Y-%m-%d %H:%M") if ts else "-"


def _hm(ts) -> str:
    return ts.strftime("%H:%M") if ts else "-"


def _session_label(key: str) -> str:
    """Session key -> the name a trader reads ("ny_am" → "NY AM")."""
    window = SESSION_INDEX.get(key)
    return window.label if window else (key or "-")


def _level_label(kind: str) -> str:
    """Liquidity kind -> its display name ("PDL" → "Previous Day Low")."""
    return LEVEL_LABELS.get(kind, kind.replace("_", " ").title()) if kind else "-"


def format_signal_alert(signal: Signal) -> str:
    """Human-readable alert text for a signal (alert-only, no execution wording).

    Carries everything needed to judge the setup without opening the dashboard:
    symbol and direction, the session, the full purge → CISD → FVG lineage with
    its timeframes, the entry/stop/target levels, the risk and reward in price
    points, the reward:risk ratio, the liquidity target and the strength of the
    liquidity the setup was built on, and the efficiency score.
    """
    emoji = "🟢 BUY" if signal.direction == "buy" else "🔴 SELL"
    d = signal.digits

    # Risk/reward in price units: the signal's own numbers when it has them,
    # otherwise derived from the same geometry the RR is computed from.
    risk_pts, reward_pts = signal.risk_points, signal.reward_points
    if not risk_pts or not reward_pts:
        try:
            risk_pts, reward_pts = risk_reward_points(
                signal.entry, signal.sl, signal.tp, signal.direction)
        except ValueError:
            risk_pts = reward_pts = 0.0

    target_grade = f"  [{signal.target_grade}]" if signal.target_grade else ""
    purge_grade = f"  [{signal.purge_grade}]" if signal.purge_grade else ""

    lines = [
        f"{emoji} — {signal.asset}",
        f"Symbol:              {signal.asset}",
        f"Direction:           {signal.direction.upper()}",
        f"Session:             {_session_label(signal.session_primary) if signal.session_primary else ', '.join(_session_label(k) for k in signal.session_keys) or '-'}",
        f"Time (NY):           {_ny(signal.entry_time_ny)}",
        "",
        f"Entry:               {_price(signal.entry, d)}",
        f"Stop loss:           {_price(signal.sl, d)}",
        f"Take profit:         {_price(signal.tp, d)}",
        f"Risk:                {_points(risk_pts, d)}",
        f"Reward:              {_points(reward_pts, d)}",
        f"RR:                  {signal.rr:.2f}",
        "",
        f"Liquidity target:    {_level_label(signal.target_kind)}"
        f"{f' @ {_price(signal.target_price, d)}' if signal.target_price else ''}"
        f"{target_grade}",
        f"Liquidity strength:  {signal.purge_grade or '-'}"
        f"{f' — {_level_label(signal.liquidity_type)} @ {_price(signal.liquidity_price, d)} purged' if signal.liquidity_type else ''}",
    ]
    if signal.efficiency_score:
        lines.append(f"Efficiency:          {signal.efficiency_score:.0f}%")

    # The purge → CISD → FVG sequence, each step on its own timeframe.
    def _seq(label: str, value: str) -> str:
        return f"  {label:<19}{value}"

    lines += [
        "",
        "Sequence:",
        _seq("Purge (1H):",
             f"{_level_label(signal.liquidity_type)} @ "
             f"{_price(signal.liquidity_price, d)}"
             f"{purge_grade}  ({_hm(signal.purge_time_ny)} NY)"),
        _seq(f"CISD ({signal.cisd_tf or '-'}):",
             f"confirmed {_hm(signal.cisd_confirm_time_ny)} NY"),
        _seq("FVG (1M):",
             f"{signal.fvg_direction or '-'} "
             f"[{_price(signal.fvg_lower, d)}, {_price(signal.fvg_upper, d)}]"
             f"  ({_hm(signal.fvg_formation_time_ny)} NY)"),
    ]

    if signal.silver_bullet:
        lines.append("")
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
