"""Deployment configuration tests.

These guard the gap between "the code works on my laptop" and "an operator can
put this on a server". Nothing here starts a server or touches the network: the
subject is the *configuration surface* — the settings the app reads, the example
file that documents them, the dependency that has to be installed, and the
commands the deployment guide tells an operator to run.

The failure this catches is a setting that exists in ``config.py`` and is read at
runtime but is nowhere in ``.env.example``, so a fresh deployment silently runs
on the default — which for ``FLASK_SECRET_KEY`` means every restart signs
everybody out, and for ``SESSION_COOKIE_SECURE`` means a cookie with no Secure
flag.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import Settings, get_settings

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
REQUIREMENTS = ROOT / "requirements.txt"
DEPLOYMENT = ROOT / "docs" / "DEPLOYMENT.md"
INSTALL_PS1 = ROOT / "deploy" / "install-service.ps1"
IIS_NOTES = ROOT / "deploy" / "iis-arr-notes.md"

#: Every environment variable this app reads, and the ones a client platform
#: cannot be deployed without. Kept explicit rather than scraped from the source
#: so that adding a setting to ``config.py`` and forgetting to document it shows
#: up as a failure here rather than as a blank line in the example file.
REQUIRED_ENV_KEYS = [
    "FLASK_SECRET_KEY",
    "SESSION_LIFETIME_HOURS",
    "SESSION_COOKIE_SECURE",
    "TRUST_PROXY",
    "SERVE_THREADS",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ADMIN_EMAIL",
    "LOGIN_MAX_ATTEMPTS",
    "LOGIN_LOCKOUT_MINUTES",
    "MIN_PASSWORD_LENGTH",
]

#: The pre-existing keys. Listed so an edit to the example file cannot quietly
#: drop one of the scanner's settings while adding the web ones.
PRESERVED_ENV_KEYS = [
    "MT5_TERMINAL_PATH", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
    "MT5_SERVER_UTC_OFFSET", "ASSETS_FILE", "SYMBOL_AUTO_RESOLVE",
    "AUTO_TRADING", "MIN_RR", "RISK_PERCENT", "SCANNER_POLL_INTERVAL_MS",
    "WARMUP_M1_BARS", "TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID", "FLASK_HOST", "FLASK_PORT", "FLASK_DEBUG",
]


def _example_text() -> str:
    assert ENV_EXAMPLE.exists(), ".env.example is missing"
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def _documented_keys(text: str | None = None) -> set[str]:
    """The variable names ``.env.example`` actually sets.

    Only ``KEY=`` at the start of a line counts. Names appearing in prose are
    not settings, and counting them would let a comment satisfy the check.
    """
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=",
                          text if text is not None else _example_text(),
                          re.MULTILINE))


# --------------------------------------------------------------------------- #
# .env.example
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", REQUIRED_ENV_KEYS)
def test_the_example_file_documents_every_web_setting(key):
    assert key in _documented_keys(), f"{key} is undocumented"


@pytest.mark.parametrize("key", PRESERVED_ENV_KEYS)
def test_the_example_file_still_documents_the_existing_settings(key):
    """The scanner's configuration must survive the platform being added."""
    assert key in _documented_keys(), f"{key} was dropped"


def test_the_documented_keys_are_the_ones_the_app_reads():
    """A documented key that nothing reads is a trap for the operator."""
    source = (ROOT / "config.py").read_text(encoding="utf-8")
    read = set(re.findall(r'_env_(?:str|bool|int|float)\(\s*"([A-Z][A-Z0-9_]*)"',
                          source))

    assert read >= set(REQUIRED_ENV_KEYS)
    assert _documented_keys() >= read, _documented_keys() ^ read


def test_the_example_file_leaves_its_secrets_blank():
    """Shipping a populated example is how a placeholder becomes production."""
    text = _example_text()

    for key in ("FLASK_SECRET_KEY", "ADMIN_PASSWORD", "MT5_PASSWORD",
                "TELEGRAM_BOT_TOKEN", "LLM_API_KEY"):
        match = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
        assert match is not None, key
        assert match.group(1).strip() == "", f"{key} ships with a value"


def test_the_example_file_leaves_auto_trading_off():
    """The one line in this file that can move real money."""
    match = re.search(r"^AUTO_TRADING=(.*)$", _example_text(), re.MULTILINE)

    assert match is not None
    assert match.group(1).strip().lower() == "false"


def test_the_example_file_does_not_tell_operators_to_enable_secure_cookies_yet():
    """``SESSION_COOKIE_SECURE=true`` before HTTPS exists blocks sign-in.

    The line must default to false with the condition stated, not to true with a
    note — a copied file with the wrong value is unfixable by reading the app.
    """
    text = _example_text()
    match = re.search(r"^SESSION_COOKIE_SECURE=(.*)$", text, re.MULTILINE)

    assert match is not None and match.group(1).strip().lower() == "false"
    assert "HTTPS" in text


def test_the_example_file_warns_about_trust_proxy():
    """Trusting forwarded headers while reachable directly is spoofable."""
    text = _example_text()
    match = re.search(r"^TRUST_PROXY=(.*)$", text, re.MULTILINE)

    assert match is not None and match.group(1).strip().lower() == "false"
    assert "proxy" in text.lower()


# --------------------------------------------------------------------------- #
# Requirements
# --------------------------------------------------------------------------- #
def test_waitress_is_a_declared_dependency():
    """``run.py serve`` is what the deployment guide starts; it is optional at
    import time, so nothing else would fail if this line were dropped."""
    assert "waitress" in REQUIREMENTS.read_text(encoding="utf-8").lower()


def test_tzdata_is_a_declared_dependency():
    """Without it Windows cannot resolve America/New_York and DST is lost."""
    assert "tzdata" in REQUIREMENTS.read_text(encoding="utf-8").lower()


# --------------------------------------------------------------------------- #
# The settings the deployment depends on
# --------------------------------------------------------------------------- #
def _default(field: str):
    """The value a setting takes when nothing sets it.

    Read from the dataclass rather than from ``get_settings()``: the latter
    returns whatever the machine's own ``.env`` says, so a test against it would
    assert the developer's configuration instead of the shipped default — and
    would break the moment an operator set the very variable it is about.
    """
    return Settings.__dataclass_fields__[field].default


def test_the_secure_cookie_defaults_to_off():
    """Off by default so a local http:// run can sign in; on in production."""
    assert _default("session_cookie_secure") is False


def test_proxy_headers_are_not_trusted_by_default():
    """Trusting X-Forwarded-* while reachable directly lets a client forge them."""
    assert _default("trust_proxy") is False


def test_the_secure_defaults_are_the_documented_ones():
    assert _default("login_max_attempts") == 5
    assert _default("login_lockout_minutes") == 15
    assert _default("min_password_length") == 12
    assert _default("session_lifetime_hours") == 12
    assert _default("serve_threads") == 8


def test_a_blank_secret_key_generates_one_rather_than_refusing_to_start():
    """Documented behaviour, and the reason the key is easy to forget.

    Asserted at the source level because reaching it end-to-end means building an
    app with a blank key and then proving the cookie still validates — which
    ``test_auth.py`` covers from the outside.
    """
    assert _default("flask_secret_key") == ""

    source = (ROOT / "app" / "auth.py").read_text(encoding="utf-8")
    assert "secrets.token_urlsafe(48)" in source
    assert "temporary one" in source


def test_telegram_is_configurable_off():
    """It is a notification channel now, not the product interface."""
    source = (ROOT / "config.py").read_text(encoding="utf-8")

    assert '_env_bool("TELEGRAM_ENABLED")' in source
    assert '_env_str("TELEGRAM_BOT_TOKEN")' in source


def test_auto_trading_is_not_forced_on_anywhere():
    """The standing constraint: nothing in the deployed config turns it on."""
    source = (ROOT / "config.py").read_text(encoding="utf-8")

    assert '_env_bool("AUTO_TRADING", default=False)' in source


def test_the_session_lifetime_is_finite_and_clamped():
    """A session that never expires is a session nobody has to steal."""
    from app.auth import timedelta_hours

    assert 0 < _default("session_lifetime_hours") <= 24 * 7
    # A zero or missing value must not produce a cookie that outlives the sun.
    assert timedelta_hours(0).total_seconds() == 3600
    assert timedelta_hours(None).total_seconds() == 3600


def test_serve_threads_is_positive():
    assert _default("serve_threads") >= 1


# --------------------------------------------------------------------------- #
# The CLI the guide tells operators to run
# --------------------------------------------------------------------------- #
def test_the_cli_still_offers_every_documented_command():
    """``serve`` is new; the other six predate the platform and must stay."""
    source = (ROOT / "run.py").read_text(encoding="utf-8")

    for command in ("scan", "backtest", "web", "serve", "assets", "smoke",
                    "user"):
        assert f'"{command}"' in source, command


def test_the_cli_picks_the_right_runner_per_command():
    """Each documented command must still reach its implementation."""
    source = (ROOT / "run.py").read_text(encoding="utf-8")

    for runner in ("run_scan", "run_backtest", "run_web", "run_serve",
                   "run_assets", "run_smoke", "run_user"):
        assert f"def {runner}(" in source, runner


def test_serve_is_wired_to_waitress_not_the_development_server():
    """The development server is single-threaded and explicitly not for
    production; a handful of clients polling would queue behind each other."""
    source = (ROOT / "run.py").read_text(encoding="utf-8")
    serve_body = source.split("def run_serve(", 1)[1].split("\ndef ", 1)[0]

    assert "waitress" in serve_body
    assert "app.run(" not in serve_body and "web_app.run(" not in serve_body


def test_the_user_command_offers_no_public_signup():
    """Accounts are admin-created. There is no ``register`` verb, by design."""
    source = (ROOT / "run.py").read_text(encoding="utf-8")

    assert '"add", "list", "passwd", "disable", "enable"' in source
    for forbidden in ("signup", "register", "self-register"):
        assert f'"{forbidden}"' not in source, forbidden


def test_the_cli_refuses_an_unknown_command():
    from run import main

    with pytest.raises(SystemExit):
        main(["definitely-not-a-command"])


# --------------------------------------------------------------------------- #
# The documents an operator follows
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [DEPLOYMENT, INSTALL_PS1, IIS_NOTES])
def test_the_deployment_files_exist(path):
    assert path.exists(), f"{path} is missing"
    assert path.stat().st_size > 500, f"{path} looks empty"


def test_the_deployment_guide_covers_the_whole_path_to_a_live_site():
    """Each of these is a step an operator cannot infer from the code."""
    text = DEPLOYMENT.read_text(encoding="utf-8")

    for topic in ("Flask", "DNS", "HTTPS", "firewall", "Task Scheduler",
                  "Reverse proxy", "restart"):
        assert topic.lower() in text.lower(), topic


def test_the_deployment_guide_starts_the_production_server():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "python run.py serve" in text
    # And says the two processes are separate, which is the operational fact an
    # operator is most likely to get wrong.
    assert "python run.py scan" in text


def test_the_deployment_guide_generates_a_secret_key():
    """Rather than letting an operator pick one, or leave it blank."""
    assert "secrets.token_urlsafe" in DEPLOYMENT.read_text(encoding="utf-8")


def test_the_deployment_guide_says_how_to_verify_the_live_site():
    """The checks are what let an operator find out; the guide must not assume."""
    text = DEPLOYMENT.read_text(encoding="utf-8")

    for check in ("Resolve-DnsName", "curl.exe"):
        assert check in text, check


def test_the_deployment_guide_backs_up_the_database_that_actually_exists():
    """It named a file the app never creates, so the copy would have failed.

    Worse than a wrong path: an operator who runs a failing ``Copy-Item`` and
    does not read the error believes they hold a backup.
    """
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert r"data\trading.db" in text
    assert "trading_bot.db" not in text


def test_the_deployment_guide_never_claims_the_domain_is_already_live():
    """Nothing in this repository can configure DNS, so nothing may assert it.

    The documented checks are phrased as things the operator runs to find out,
    which is why the file is allowed to *mention* a live domain at all.
    """
    text = DEPLOYMENT.read_text(encoding="utf-8").lower()

    for claim in ("the domain is live", "is already live", "is now live",
                  "pointing at this server already"):
        assert claim not in text, claim


def test_the_deployment_guide_warns_that_auto_trading_is_off_by_design():
    text = DEPLOYMENT.read_text(encoding="utf-8")

    assert "AUTO_TRADING" in text
    assert "false" in text.lower()


def test_the_installer_registers_both_jobs():
    """Scanner and web app are separate at the process level, by requirement."""
    text = INSTALL_PS1.read_text(encoding="utf-8")

    assert "scan" in text
    assert "serve" in text
    assert "Register-ScheduledTask" in text


def test_the_installer_uses_the_virtual_environment_python():
    """Not a bare ``python``, which on a server resolves to whatever is on PATH."""
    assert ".venv" in INSTALL_PS1.read_text(encoding="utf-8")


def test_the_installer_requires_an_env_file():
    """Starting without one is a silent misconfiguration, so it refuses."""
    text = INSTALL_PS1.read_text(encoding="utf-8")

    assert ".env" in text
    assert "throw" in text.lower() or "exit 1" in text.lower()
