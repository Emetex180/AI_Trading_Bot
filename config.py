"""Central project configuration.

All settings are read from environment variables (`.env`) with safe defaults.
The module performs no I/O to MetaTrader / network; it only parses configuration,
so it is safe to import from unit tests.

Time convention
---------------
The project uses the **America/New_York** calendar clock, resolved DST-aware via
:mod:`zoneinfo` (EDT/EST switch automatically — never a hard-coded UTC-4). MT5
candle times arrive in the *broker server* clock; the offset
``MT5_SERVER_UTC_OFFSET`` is the number of hours the server is ahead of UTC.
All conversion between broker time and the NY clock lives in
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


def _env_str_or(key: str, default: str) -> str:
    """A string setting where a *blank* value means "use the default".

    Distinct from :func:`_env_str`, where blank is a legitimate value: an empty
    ``MT5_LOGIN`` genuinely means "use the account the terminal already has", and
    an empty ``FLASK_SECRET_KEY`` means "generate a temporary one". For a setting
    like ``DATABASE_URL`` blank is never a real choice — it is what an operator
    gets by uncommenting a line in a copied ``.env`` without filling it in — and
    letting that through would replace the working default with an empty string.
    """
    return (os.getenv(key) or "").strip() or default


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


def _env_optional_float(key: str) -> float | None:
    """A float that may legitimately be absent.

    Distinct from :func:`_env_float`: there, a missing key means "use the
    default"; here it means "no value was configured at all", which is a
    different answer from any number. ``MT5_SERVER_UTC_OFFSET`` relies on that
    distinction — blank means "discover it from MT5", not "assume zero".
    """
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


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


def auto_trading_enabled(settings: Any) -> bool:
    """The effective auto-trading switch, honouring a runtime override.

    A dashboard override wins over the ``.env`` baseline when one is set;
    ``None`` means no override has been requested, so the baseline stands.

    Duck-typed rather than typed as :class:`Settings` on purpose: the executor
    tests build a ``SimpleNamespace`` stand-in, and a settings object with no
    ``auto_trading_override`` attribute must read as "no override" rather than
    raising ``AttributeError`` inside the execution gate.
    """
    override = getattr(settings, "auto_trading_override", None)
    if override is None:
        return bool(getattr(settings, "auto_trading", False))
    return bool(override)


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
    #: Broker clock offset ahead of UTC, in hours. ``None`` (the default) means
    #: *discover it from the live MT5 terminal* — see :mod:`trading.time_utils`.
    #: Only set this to pin a number, and only when you know the broker's clock.
    mt5_server_utc_offset: float | None

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
    #: A second destination for the *same* alert — a broadcast channel. Blank
    #: (the default) sends to ``telegram_chat_id`` alone, so leaving this unset
    #: reproduces the pre-channel behaviour exactly. Set it to the channel's
    #: numeric id (``-100...``), which survives a rename; ``@username`` also
    #: works for a public channel. The bot must be able to post there.
    telegram_channel_id: str

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
    #: Log the MT5 -> UTC -> New York -> session conversion chain. Temporary
    #: verification aid; falls back to ``flask_debug`` when unset.
    time_debug: bool = False

    # --- ICT model -----------------------------------------------------------
    # All optional (defaulted) so the dataclass still allows the non-default
    # fields above; every one is also overridable per asset via
    # ``ASSET_<NAME>_<UPPER_SNAKE>`` (see ``asset_overrides_for``).
    #
    # CISD is fixed to M5 by the model: the 5M candle is both the confirmation
    # candle and the liquidity-taking candle the stop is anchored to.
    cisd_timeframe: str = "M5"
    # Stop loss: the greater of an ATR buffer and a whole number of ticks beyond
    # the 5M liquidity-taking candle. Tick-based so the offset respects each
    # symbol's own point size instead of a fixed number of points.
    sl_min_offset_ticks: float = 2.0
    # Take profit: pulled this far *before* the liquidity level it targets.
    tp_liquidity_offset_atr: float = 0.1
    tp_liquidity_offset_ticks: float = 2.0
    # Grade a level must reach to be a valid TP target at all...
    min_tp_liquidity_grade: str = "MEDIUM"
    # ...and the stricter bar a CONDITIONAL session imposes.
    conditional_min_liquidity_grade: str = "HIGH"
    # Setup timeouts, in candles of the timeframe each step runs on.
    max_cisd_candles: int = 8
    fvg_wait_m1: int = 90
    retrace_wait_m1: int = 90
    # Minimum FVG depth as a fraction of 5M ATR. Defaults to 0.10: a gap thinner
    # than a tenth of the M5 true range is noise, not displacement, and without
    # a floor a single-tick gap qualifies as an FVG. 0 disables the filter.
    fvg_min_atr_frac: float = 0.10
    # Where the entry price must sit relative to the gap.
    #   "inside_fvg"     – (default) the entry candle must CLOSE within
    #                      [fvg.lower, fvg.upper]; the fill is the gap itself.
    #   "reaction_close" – the older, looser rule: any close back beyond the gap
    #                      that reclaims it counts, even if the close is outside
    #                      the zone. Opt in only deliberately; it permits a
    #                      signal whose entry price is not in the FVG at all.
    fvg_entry_model: str = "inside_fvg"
    # Liquidity lookbacks.
    swing_lookback: int = 3
    session_lookback_days: int = 3
    # Per-session cap on emitted signals; 0 = unlimited.
    max_signals_per_session: int = 0
    # Whether a pending setup is dropped when a NO-trade session begins.
    invalidate_on_no_trade_session: bool = True
    # Session enforcement: when False, the session windows are informational and
    # never block an entry.
    enforce_sessions: bool = True
    # Activity gate: when True the live loop is awake only inside a tradeable
    # session on a trading day, and sleeps otherwise. Deliberately independent of
    # ``enforce_sessions`` above — that one filters *entries*, this one decides
    # *when the process works at all*. Set False to restore always-on scanning.
    session_gate_enabled: bool = True
    # Weekday names the bot may run on. Anything outside this set is slept
    # through, so the default is the Mon-Fri week the operator asked for.
    trading_days: list[str] = field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    # Trade-efficiency score: RR saturation point, the ATR multiple of room a
    # full-distance target is worth, and the three component weights.
    efficiency_rr_target: float = 3.0
    efficiency_atr_target_multiple: float = 4.0
    efficiency_weight_rr: float = 0.5
    efficiency_weight_liquidity: float = 0.3
    efficiency_weight_distance: float = 0.2

    extra: dict[str, Any] = field(default_factory=dict)

    # --- Runtime overrides ----------------------------------------------------
    # Set from the dashboard, and deliberately NOT persisted: a restart drops
    # back to the ``auto_trading`` baseline read from ``.env``, so the bot can
    # never come back up armed. ``None`` means "follow the baseline".
    #
    # Declared last because a defaulted field cannot precede the non-default
    # ones above; every construction site uses keywords, so position is free.
    auto_trading_override: bool | None = None

    # --- Web platform: sessions and accounts ----------------------------------
    # Everything below is defaulted for the same reason as ``auto_trading_override``
    # above, and so that tests constructing a Settings via ``dataclasses.replace``
    # keep working without naming any of it.
    #
    #: Signs the Flask session cookie. **Required in production.** When blank the
    #: app generates a temporary key at startup and warns: sessions then reset on
    #: every restart, which is logged-out-but-not-insecure. Blank is never
    #: silently accepted in place of a real key on a client-facing deployment.
    flask_secret_key: str = ""
    #: Set ``SESSION_COOKIE_SECURE=true`` in production. Left False by default so
    #: a plain-HTTP localhost run can still log in — a Secure cookie is never sent
    #: over http, so a local run would be unable to authenticate at all.
    session_cookie_secure: bool = False
    session_lifetime_hours: int = 12
    #: Trust ``X-Forwarded-*`` from the reverse proxy (IIS/ARR). Must stay False
    #: when the app is reached directly, or a client could forge its own scheme
    #: and host through those headers.
    trust_proxy: bool = False
    #: First-run admin, created only while the users table is empty.
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_email: str = ""
    #: Login throttle: failed attempts per username+address before a cooldown.
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    #: Enforced wherever a password is set (CLI, admin console).
    min_password_length: int = 12
    #: Waitress worker threads for ``run.py serve``.
    serve_threads: int = 8
    #: Whether starting the web application also starts the trading engine.
    #:
    #: True by default, because a dashboard whose scanner has to be started by a
    #: second command is the configuration that produces "Scanner: STOPPED" on a
    #: machine where nothing is actually wrong. ``run.py web`` and ``run.py
    #: serve`` both honour it; the engine runs on a background thread inside the
    #: web process, which is exactly what the console's Start button has always
    #: done.
    #:
    #: The start is never *forced*: ``runner.JobManager.start_live`` refuses when
    #: another process already holds the engine lease, so a deployment that runs
    #: the scanner as its own task (see ``deploy/install-service.ps1``) keeps
    #: exactly one engine and this quietly becomes a no-op.
    #:
    #: Set false for a dashboard that owns no engine — watching a scanner started
    #: elsewhere, or any process that must not touch MT5.
    engine_autostart: bool = True

    @property
    def effective_auto_trading(self) -> bool:
        """The value every gate, log line and badge must read.

        Not ``auto_trading`` itself: the baseline is what ``.env`` said, and the
        override is what the operator asked for at runtime. Rendering the
        baseline on the dashboard would show a stale posture the moment the
        override is used.
        """
        return auto_trading_enabled(self)

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
        mt5_server_utc_offset=_env_optional_float("MT5_SERVER_UTC_OFFSET"),
        assets_file=assets_path,
        symbol_auto_resolve=_env_bool("SYMBOL_AUTO_RESOLVE", default=True),
        auto_trading=_env_bool("AUTO_TRADING", default=False),
        min_rr=_env_float("MIN_RR", 1.5),
        target_rr_range=(rr_low, rr_high),
        risk_percent=_env_float("RISK_PERCENT", 1.0),
        sl_buffer_atr=_env_float("SL_BUFFER_ATR", 0.25),
        valid_entry_sessions=_env_list(
            "VALID_ENTRY_SESSIONS",
            ["london_open", "ny_am", "ny_pm", "power_hour"],
        ),
        cisd_threshold_hour_ny=_env_int("CISD_THRESHOLD_HOUR_NY", 9),
        order_deviation=_env_int("ORDER_DEVIATION", 20),
        scanner_poll_interval_ms=_env_int("SCANNER_POLL_INTERVAL_MS", 5000),
        backtest_m1_bars=_env_int("BACKTEST_M1_BARS", 20000),
        warmup_m1_bars=_env_int("WARMUP_M1_BARS", DEFAULT_WARMUP_M1_BARS),
        telegram_enabled=_env_bool("TELEGRAM_ENABLED"),
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env_str("TELEGRAM_CHAT_ID"),
        telegram_channel_id=_env_str("TELEGRAM_CHANNEL_ID"),
        ai_enabled=_env_bool("AI_ENABLED"),
        llm_api_url=_env_str("LLM_API_URL", "https://api.openai.com/v1/chat/completions"),
        llm_api_key=_env_str("LLM_API_KEY"),
        llm_model=_env_str("LLM_MODEL", "gpt-4o-mini"),
        flask_host=_env_str("FLASK_HOST", "127.0.0.1"),
        flask_port=_env_int("FLASK_PORT", 5000),
        flask_debug=_env_bool("FLASK_DEBUG"),
        db_url=_env_str_or("DATABASE_URL", f"sqlite:///{DATA_DIR / 'trading.db'}"),
        llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 20.0),
        telegram_timeout_seconds=_env_float("TELEGRAM_TIMEOUT_SECONDS", 10.0),
        asset_env={k: v for k, v in os.environ.items() if k.startswith("ASSET_")},
        time_debug=_env_bool("TIME_DEBUG"),
        # --- ICT model ----------------------------------------------------- #
        cisd_timeframe=_env_str("CISD_TIMEFRAME", "M5"),
        sl_min_offset_ticks=_env_float("SL_MIN_OFFSET_TICKS", 2.0),
        tp_liquidity_offset_atr=_env_float("TP_LIQUIDITY_OFFSET_ATR", 0.1),
        tp_liquidity_offset_ticks=_env_float("TP_LIQUIDITY_OFFSET_TICKS", 2.0),
        min_tp_liquidity_grade=_env_str("MIN_TP_LIQUIDITY_GRADE", "MEDIUM").upper(),
        conditional_min_liquidity_grade=_env_str(
            "CONDITIONAL_MIN_LIQUIDITY_GRADE", "HIGH").upper(),
        max_cisd_candles=_env_int("MAX_CISD_CANDLES", 8),
        fvg_wait_m1=_env_int("FVG_WAIT_M1", 90),
        retrace_wait_m1=_env_int("RETRACE_WAIT_M1", 90),
        fvg_min_atr_frac=_env_float("FVG_MIN_ATR_FRAC", 0.10),
        fvg_entry_model=_env_str("FVG_ENTRY_MODEL", "inside_fvg").strip().lower(),
        swing_lookback=_env_int("SWING_LOOKBACK", 3),
        session_lookback_days=_env_int("SESSION_LOOKBACK_DAYS", 3),
        max_signals_per_session=_env_int("MAX_SIGNALS_PER_SESSION", 0),
        invalidate_on_no_trade_session=_env_bool("INVALIDATE_ON_NO_TRADE_SESSION", default=True),
        enforce_sessions=_env_bool("ENFORCE_SESSIONS", default=True),
        session_gate_enabled=_env_bool("SESSION_GATE_ENABLED", default=True),
        trading_days=_env_list("TRADING_DAYS",
                               ["mon", "tue", "wed", "thu", "fri"]),
        efficiency_rr_target=_env_float("EFFICIENCY_RR_TARGET", 3.0),
        efficiency_atr_target_multiple=_env_float("EFFICIENCY_ATR_TARGET_MULTIPLE", 4.0),
        efficiency_weight_rr=_env_float("EFFICIENCY_WEIGHT_RR", 0.5),
        efficiency_weight_liquidity=_env_float("EFFICIENCY_WEIGHT_LIQUIDITY", 0.3),
        efficiency_weight_distance=_env_float("EFFICIENCY_WEIGHT_DISTANCE", 0.2),
        # --- Web platform -------------------------------------------------- #
        flask_secret_key=_env_str("FLASK_SECRET_KEY"),
        session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE", default=False),
        session_lifetime_hours=_env_int("SESSION_LIFETIME_HOURS", 12),
        trust_proxy=_env_bool("TRUST_PROXY", default=False),
        bootstrap_admin_username=_env_str("ADMIN_USERNAME"),
        bootstrap_admin_password=_env_str("ADMIN_PASSWORD"),
        bootstrap_admin_email=_env_str("ADMIN_EMAIL"),
        login_max_attempts=_env_int("LOGIN_MAX_ATTEMPTS", 5),
        login_lockout_minutes=_env_int("LOGIN_LOCKOUT_MINUTES", 15),
        min_password_length=_env_int("MIN_PASSWORD_LENGTH", 12),
        serve_threads=_env_int("SERVE_THREADS", 8),
        engine_autostart=_env_bool("ENGINE_AUTOSTART", default=True),
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
