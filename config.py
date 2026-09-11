"""Central project configuration.

All settings are read from environment variables (`.env`) with safe defaults.
The module performs no I/O to MetaTrader / network; it only parses configuration,
so it is safe to import from unit tests.

Time convention
---------------
The project uses a FIXED algorithmic New York clock of UTC-4 (never auto-switches
to UTC-5). MT5 candle times arrive in the *broker server* clock; the offset
``MT5_SERVER_UTC_OFFSET`` is the number of hours the server is ahead of UTC.
All conversion between broker time and the NY (UTC-4) clock lives in
:mod:`trading.time_utils` — nothing else may convert timezones.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# Historical M1 candles replayed at attach so the engine's episode state is
# current before live signals are considered.
DEFAULT_WARMUP_M1_BARS = 5000

# Load `.env` from the project root (no-op if missing).
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------- #
# Small typed helpers
# --------------------------------------------------------------------------- #
def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Settings object
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    """Typed, read-only snapshot of runtime settings."""

    # --- MetaTrader 5 -------------------------------------------------------
    mt5_terminal_path: str
    mt5_login: str | None
    mt5_password: str | None
    mt5_server: str | None
    mt5_server_utc_offset: float

    # --- Assets --------------------------------------------------------------
    assets_file: Path
    symbol_auto_resolve: bool

    # --- Trading safety ------------------------------------------------------
    auto_trading: bool
    min_rr: float
    target_rr_range: tuple[float, float]
    risk_percent: float
    sl_buffer_atr: float
    valid_entry_sessions: list[str]
    cisd_threshold_hour_ny: int
    order_deviation: int

    # --- Scanner / backtest --------------------------------------------------
    scanner_poll_interval_ms: int
    backtest_m1_bars: int
    warmup_m1_bars: int

    # --- Telegram -------------------------------------------------------------
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str

    # --- AI ------------------------------------------------------------------
    ai_enabled: bool
    llm_api_url: str
    llm_api_key: str
    llm_model: str

    # --- Flask ---------------------------------------------------------------
    flask_host: str
    flask_port: int
    flask_debug: bool

    # --- Derived -------------------------------------------------------------
    # Snapshotted like everything else, so `.env` is read exactly once per
    # Settings. Reading these lazily from ``os.environ`` would mean half the
    # configuration froze at startup while half changed under a running process.
    db_url: str
    llm_timeout_seconds: float
    telegram_timeout_seconds: float
    # Raw ``ASSET_<NAME>_<FIELD>`` entries, resolved by ``asset_overrides_for``.
    asset_env: dict[str, str] = field(default_factory=dict)

    extra: dict[str, Any] = field(default_factory=dict)

    def asset_overrides_for(self, asset_name: str) -> dict[str, Any]:
        """Per-asset strategy overrides read from ``ASSET_<NAME>_...`` env keys.

        Example: ``ASSET_USTEC_SL_BUFFER_ATR=0.5``.
        """
        prefix = f"ASSET_{asset_name.upper()}_"
        return {key[len(prefix):].lower(): value
                for key, value in self.asset_env.items()
                if key.startswith(prefix)}


def _build_settings() -> Settings:
    default_rr_target = _env_list("TARGET_RR_RANGE", ["1.5", "2.5"])
    try:
        rr_low, rr_high = float(default_rr_target[0]), float(default_rr_target[1])
    except (IndexError, ValueError):
        rr_low, rr_high = 1.5, 2.5

    assets_file = _env_str("ASSETS_FILE", "assets.json")
    assets_path = Path(assets_file)
    if not assets_path.is_absolute():
        assets_path = BASE_DIR / assets_path

    return Settings(
        mt5_terminal_path=_env_str("MT5_TERMINAL_PATH"),
        mt5_login=_env_str("MT5_LOGIN") or None,
        mt5_password=_env_str("MT5_PASSWORD") or None,
        mt5_server=_env_str("MT5_SERVER") or None,
        mt5_server_utc_offset=_env_float("MT5_SERVER_UTC_OFFSET", 2.0),
        assets_file=assets_path,
        symbol_auto_resolve=_env_bool("SYMBOL_AUTO_RESOLVE", default=True),
        auto_trading=_env_bool("AUTO_TRADING", default=False),
        min_rr=_env_float("MIN_RR", 1.5),
        target_rr_range=(rr_low, rr_high),
        risk_percent=_env_float("RISK_PERCENT", 1.0),
        sl_buffer_atr=_env_float("SL_BUFFER_ATR", 0.25),
        valid_entry_sessions=_env_list(
            "VALID_ENTRY_SESSIONS",
            ["london_open", "ny_am", "london_close", "ny_pm", "power_hour"],
        ),
        cisd_threshold_hour_ny=_env_int("CISD_THRESHOLD_HOUR_NY", 9),
        order_deviation=_env_int("ORDER_DEVIATION", 20),
        scanner_poll_interval_ms=_env_int("SCANNER_POLL_INTERVAL_MS", 5000),
        backtest_m1_bars=_env_int("BACKTEST_M1_BARS", 20000),
        warmup_m1_bars=_env_int("WARMUP_M1_BARS", DEFAULT_WARMUP_M1_BARS),
        telegram_enabled=_env_bool("TELEGRAM_ENABLED"),
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env_str("TELEGRAM_CHAT_ID"),
        ai_enabled=_env_bool("AI_ENABLED"),
        llm_api_url=_env_str("LLM_API_URL", "https://api.openai.com/v1/chat/completions"),
        llm_api_key=_env_str("LLM_API_KEY"),
        llm_model=_env_str("LLM_MODEL", "gpt-4o-mini"),
        flask_host=_env_str("FLASK_HOST", "127.0.0.1"),
        flask_port=_env_int("FLASK_PORT", 5000),
        flask_debug=_env_bool("FLASK_DEBUG"),
        db_url=_env_str("DATABASE_URL", f"sqlite:///{DATA_DIR / 'trading.db'}"),
        llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 20.0),
        telegram_timeout_seconds=_env_float("TELEGRAM_TIMEOUT_SECONDS", 10.0),
        asset_env={k: v for k, v in os.environ.items() if k.startswith("ASSET_")},
    )


# A single lazily-built instance; tests may construct their own Settings too.
_settings_cache: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` (cached after first build)."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = _build_settings()
    return _settings_cache


def reload_settings() -> Settings:
    """Rebuild settings (useful after editing .env in a long-lived process)."""
    global _settings_cache
    _settings_cache = _build_settings()
    return _settings_cache
