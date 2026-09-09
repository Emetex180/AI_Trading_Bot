"""Telegram alert-only tests."""
from datetime import datetime
from types import SimpleNamespace

from notifications.telegram import (ALERT_ONLY_TAG, TelegramNotifier,
                                    SendResult, format_signal_alert)
from trading.signal_engine import Signal


def _signal(**kw):
    base = dict(
        asset="GOLD", direction="buy",
        entry=2600.0, sl=2590.0, tp=2620.0,
        entry_time_utc=datetime(2026, 1, 6, 13, 10),
        entry_time_ny=datetime(2026, 1, 6, 9, 10),
        session_keys=["ny_am"], session_primary="ny_am",
        silver_bullet=None, macro=None,
        liquidity_type="PDL", liquidity_price=2590.0,
        purge_time_ny=datetime(2026, 1, 6, 8, 0),
        cisd_tf="M15", cisd_confirm_time_ny=datetime(2026, 1, 6, 8, 30),
        fvg_direction="bullish", fvg_lower=2599.0, fvg_upper=2599.5,
        rr=2.0, status="APPROVED", alert_only=True, risk_approved=True,
        ai_status=None, ai_decision=None, ai_score=None,
        ai_reasoning=None, ai_confidence=None,
    )
    base.update(kw)
    return Signal(**base)


def _settings(enabled=True):
    return SimpleNamespace(
        telegram_enabled=enabled,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        telegram_timeout_seconds=5,
    )


def test_format_alert_has_alert_only_tag():
    text = format_signal_alert(_signal())
    assert "BUY" in text
    assert ALERT_ONLY_TAG in text
    assert "execution" not in text.split("\n")[0].lower()  # not an exec cmd


def test_notifier_disabled_sends_nothing():
    called = []

    def transport(text):
        called.append(text)
        return SendResult(ok=True)

    n = TelegramNotifier(settings=_settings(enabled=False), transport=transport)
    res = n.send_signal(_signal())
    assert not res.ok and res.error == "telegram_disabled"
    assert called == []


def test_notifier_sends_when_enabled():
    seen = []
    n = TelegramNotifier(settings=_settings(enabled=True),
                         transport=lambda text: (seen.append(text), SendResult(ok=True))[1])
    res = n.send_signal(_signal())
    assert res.ok
    assert len(seen) == 1
    assert ALERT_ONLY_TAG in seen[0]


def test_ai_unavailable_flag_in_message():
    text = format_signal_alert(_signal(ai_status="AI_UNAVAILABLE"))
    assert "AI analysis unavailable" in text
    assert ALERT_ONLY_TAG in text
