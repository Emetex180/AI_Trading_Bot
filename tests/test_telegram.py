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


# --------------------------------------------------------------------------- #
# The ICT alert layout
# --------------------------------------------------------------------------- #
def _ict_signal(**kw):
    """A signal carrying every field the model produces.

    Callers override any field by keyword, so a test only states what it is
    actually exercising.
    """
    fields = dict(
        asset="US100", entry=20130.5, sl=20058.0, tp=20310.0, digits=2,
        purge_grade="VERY_HIGH", target_kind="PDH", target_price=20320.75,
        target_grade="VERY_HIGH", risk_points=72.5, reward_points=190.25,
        efficiency_score=82.0, rr=2.62, cisd_tf="M5",
        fvg_formation_time_ny=datetime(2026, 1, 6, 9, 31),
    )
    fields.update(kw)
    return _signal(**fields)


def test_alert_carries_every_field_the_model_produces():
    text = format_signal_alert(_ict_signal())

    for label in ("Symbol:", "Direction:", "Session:", "Entry:", "Stop loss:",
                  "Take profit:", "Risk:", "Reward:", "RR:",
                  "Liquidity target:", "Liquidity strength:"):
        assert label in text, label


def test_alert_shows_the_purge_cisd_fvg_sequence():
    text = format_signal_alert(_ict_signal())
    assert "Sequence:" in text
    assert "Purge (1H):" in text
    assert "CISD (M5):" in text
    assert "FVG (1M):" in text


def test_alert_names_levels_and_sessions_in_readable_form():
    text = format_signal_alert(_ict_signal())
    assert "Previous Day High" in text          # not the raw "PDH"
    assert "Previous Day Low" in text
    assert "NY AM" in text                      # not the raw "ny_am"


def test_alert_prints_prices_at_the_symbol_precision():
    two = format_signal_alert(_ict_signal(digits=2))
    assert "20130.50" in two
    one = format_signal_alert(_ict_signal(digits=1, entry=20130.56))
    assert "20130.6" in one                     # rounded to the symbol's digits
    assert "20130.56" not in one                # never more places than it has


def test_alert_reports_risk_reward_and_rr():
    text = format_signal_alert(_ict_signal())
    assert "72.50 pts" in text
    assert "190.25 pts" in text
    assert "2.62" in text


def test_alert_reports_efficiency_score():
    assert "82%" in format_signal_alert(_ict_signal())


def test_alert_derives_risk_reward_when_the_signal_has_none():
    """A signal from an older row still prints a coherent risk/reward."""
    text = format_signal_alert(_signal(entry=2600.0, sl=2590.0, tp=2620.0,
                                       digits=1))
    assert "10.0 pts" in text
    assert "20.0 pts" in text


def test_alert_survives_missing_lineage_fields():
    """A signal with no recorded lineage must still format, not raise."""
    text = format_signal_alert(_signal(
        liquidity_type="", liquidity_price=0.0, purge_grade="",
        target_kind="", target_price=0.0, target_grade="",
        purge_time_ny=None, cisd_confirm_time_ny=None,
        fvg_formation_time_ny=None, session_keys=[], session_primary=""))
    assert "Symbol:" in text
    assert ALERT_ONLY_TAG in text
