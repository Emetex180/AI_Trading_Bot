"""Settings snapshot behaviour.

``Settings`` promises a single snapshot of the environment taken at build time.
It used to break that promise for four members — ``db_url``,
``llm_timeout_seconds``, ``telegram_timeout_seconds`` and
``asset_overrides_for`` — which re-read ``os.environ`` on every access, so
editing ``.env`` while the process ran would half-apply: some settings frozen at
startup, others changing underneath a live session.
"""
from __future__ import annotations

from config import DEFAULT_WARMUP_M1_BARS, reload_settings


def test_settings_do_not_re_read_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///first.db")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "11")
    monkeypatch.setenv("TELEGRAM_TIMEOUT_SECONDS", "22")
    cfg = reload_settings()

    assert cfg.db_url == "sqlite:///first.db"
    assert cfg.llm_timeout_seconds == 11
    assert cfg.telegram_timeout_seconds == 22

    # Changing the environment must not reach into an existing snapshot...
    monkeypatch.setenv("DATABASE_URL", "sqlite:///second.db")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "99")
    assert cfg.db_url == "sqlite:///first.db"
    assert cfg.llm_timeout_seconds == 11

    # ...but an explicit rebuild must pick the new values up.
    rebuilt = reload_settings()
    assert rebuilt.db_url == "sqlite:///second.db"
    assert rebuilt.llm_timeout_seconds == 99


def test_asset_overrides_come_from_the_snapshot(monkeypatch):
    monkeypatch.setenv("ASSET_USTEC_SL_BUFFER_ATR", "0.5")
    cfg = reload_settings()

    assert cfg.asset_overrides_for("USTEC") == {"sl_buffer_atr": "0.5"}
    assert cfg.asset_overrides_for("ustec") == {"sl_buffer_atr": "0.5"}
    assert cfg.asset_overrides_for("GOLD") == {}

    # A variable set after the snapshot is not visible through it.
    monkeypatch.setenv("ASSET_GOLD_SL_BUFFER_ATR", "0.9")
    assert cfg.asset_overrides_for("GOLD") == {}
    assert reload_settings().asset_overrides_for("GOLD") == {"sl_buffer_atr": "0.9"}


def test_warmup_bars_are_a_setting_not_an_ad_hoc_read(monkeypatch):
    monkeypatch.setenv("WARMUP_M1_BARS", "123")
    assert reload_settings().warmup_m1_bars == 123

    monkeypatch.delenv("WARMUP_M1_BARS")
    assert reload_settings().warmup_m1_bars == DEFAULT_WARMUP_M1_BARS
