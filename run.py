"""Command-line entrypoints for the AI ICT trading platform.

Usage (from the project root)::

    python run.py scan      # live multi-asset scanner (ALERT ONLY by default)
    python run.py backtest  # run a historical backtest per enabled asset
    python run.py web       # dashboard + control panel (development server)
    python run.py serve     # the same app behind Waitress (production)
    python run.py assets    # list the asset registry
    python run.py smoke     # quick no-MT5 self-check
    python run.py user ...  # create and manage platform accounts

The scanner never executes orders unless AUTO_TRADING=true is set explicitly in
``.env`` (default false). All safety gating lives in ``trading.executor``.

``scan`` and ``backtest`` are thin wrappers around :mod:`runner`, which the
dashboard also drives — so the browser and the CLI run identical code.

``web`` and ``serve`` run the *same* WSGI application; they differ only in the
server in front of it. ``web`` is Flask's development server — fine for a
laptop, single-threaded and explicitly not for production. ``serve`` is
Waitress, which is what the deployment guide starts on the server.

Either one starts the whole system, not just the dashboard: the trading engine
comes up with it, on the same :class:`runner.JobManager` the pages read, so
there is no second command to remember and no second engine to reconcile. The
start is never forced past an engine that is still there — when another process
holds the engine lease the web app stands down and reports that engine instead
(see :func:`start_engine`) — but a lease left behind by a process that has since
died is taken over, so restarting after a crash brings the engine back rather
than showing it stopped. ``--no-engine``, or ``ENGINE_AUTOSTART=false``, gives a
dashboard that owns no engine at all.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from config import Settings, get_settings
from database.repository import Repository, init_db

# --------------------------------------------------------------------------- #
# Live scanner
# --------------------------------------------------------------------------- #
def run_scan(settings: Settings, force: bool = False) -> int:
    """Live multi-asset scanner, run by the shared :class:`runner.JobManager`.

    The loop itself lives in ``runner.py`` so the CLI and the dashboard drive
    exactly the same implementation. This function only starts it, blocks until
    Ctrl+C or a fatal error, then stops it.

    Refuses to start when another process is already scanning — two engines
    would each reach the same setups and alert on them twice. ``--force`` is for
    the case the check cannot tell apart from a live one: a hard crash leaves a
    fresh-looking lease behind until it expires.
    """
    from runner import LIVE_ERROR, JobManager

    jobs = JobManager(settings=settings, on_event=print)
    started = jobs.start_live(force=force)
    if not started["ok"]:
        print(f"[scan] {started['message']}")
        if started.get("reason") == "engine_elsewhere":
            print("[scan] re-run with --force only if you are certain that "
                  "engine is gone.")
        return 2

    try:
        while jobs.is_live_running():
            time.sleep(0.25)
    except KeyboardInterrupt:  # pragma: no cover - user stop
        print("\n[scan] stopping...")
        jobs.stop_live()
        return 0

    state = jobs.live_state()
    if state["state"] == LIVE_ERROR:
        print(f"[scan] {state['last_error']}")
        return 2
    return 0


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def run_backtest(settings: Settings, asset: str | None = None) -> int:
    """Historical backtest per asset, run by the shared :class:`runner.JobManager`."""
    from runner import BT_ERROR, JobManager

    jobs = JobManager(settings=settings, on_event=print)
    requested = jobs.request_backtest(asset=asset)
    if not requested["ok"]:
        print(f"[backtest] {requested['message']}")
        return 2

    state = jobs.wait_for_backtest()
    if state["state"] == BT_ERROR:
        print(f"[backtest] {state['last_error']}")
        return 2
    return 0


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def _windows_pid_is_running(pid: int) -> bool:
    """The Windows half of :func:`_pid_is_running` (see it for the contract)."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        # ERROR_INVALID_PARAMETER is what Windows returns for a pid with no
        # process behind it. Anything else — usually access denied, when the
        # scanner runs as a service under another account — means the process is
        # there and this one simply may not look at it.
        return kernel32.GetLastError() != 87
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle),
                                           ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _pid_is_running(pid) -> bool:
    """Is a process with this id still alive on this machine?

    One narrow question, asked because the engine lease cannot answer it on its
    own: the lease is fresh, but is the pid it names an engine that is still
    scanning or the ghost of one that was killed? ``False`` here licenses
    ``start_live(force=True)``, so **every uncertain answer is ``True``**. An
    unnecessary deferral costs one restart; a wrong ``False`` starts a second
    engine that broadcasts every setup twice.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True  # no pid recorded: nothing here proves the engine is gone
    if pid <= 0:
        return True
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # permission denied and friends: it exists, or we can't tell
    return True


def _report_start(log, result: dict) -> dict:
    """Print one start attempt's outcome in the operator's words."""
    reason = result.get("reason") or ""
    if result.get("ok"):
        log(f"[engine] {result.get('message') or 'starting'} scanning will "
            f"begin as soon as the terminal is connected and the broker clock "
            f"is verified.")
    elif reason == "already_running":
        log("[engine] already running in this process.")
    elif reason == "backtest_running":
        log(f"[engine] {result.get('message')}")
    else:
        log(f"[engine] could not start: {result.get('message') or reason}")
    return result


def start_engine(jobs, settings: Settings, *, autostart: bool | None = None,
                 log=print) -> dict:
    """Start the trading engine on the web app's own :class:`runner.JobManager`.

    This is the whole of "the dashboard starts everything": the engine the
    dashboard reads is the engine this starts, because ``jobs`` is the very
    object ``create_app`` put in ``app.config["JOBS"]``. There is no second
    scanner, no second connection and no state to reconcile — the loop, the
    setup pipeline, the MT5 session and the signal engine all live behind that
    one call, and everything the dashboard renders is read back out of it.

    The start is **not forced past a live engine**. ``JobManager.start_live``
    already refuses when another process holds the engine lease, and that
    refusal is the correct outcome here rather than an error: a deployment that
    runs ``run.py scan`` as its own task (see ``deploy/install-service.ps1``)
    must keep exactly one engine, and starting a second one would duplicate
    every signal and every Telegram alert. When the refusal happens the
    dashboard reports the *other* engine from the persisted lease, so the
    operator still sees live state — just not from this process.

    There is exactly one exception, and it is the case that made a first version
    of this function leave a dashboard stuck at ``STOPPED``: a lease held by a
    pid that no longer exists. An engine killed without running its cleanup —
    ``taskkill /F``, a closed console window, a power cut — leaves its last
    heartbeat behind, and the lease stays fresh for another 25 seconds. A web app
    started in that window would refuse the start, then never try again, and the
    dashboard would report an engine that is not there. So when the lease's pid
    is provably gone (:func:`_pid_is_running`) the start is retried with
    ``force=True``, which is precisely what the refusal message tells an operator
    to do by hand. A pid that is alive, or that cannot be checked, is left alone.

    Returns the start result so a caller (or a test) can act on the reason.
    ``autostart=None`` follows ``settings.engine_autostart``; pass ``False`` to
    suppress it, which is what tests and ``--no-engine`` do.
    """
    enabled = getattr(settings, "engine_autostart", True) if autostart is None \
        else bool(autostart)
    if not enabled:
        log("[engine] not started by this process (ENGINE_AUTOSTART is off); "
            "the dashboard will report whichever engine is running.")
        return {"ok": False, "reason": "disabled"}

    result = jobs.start_live()
    if result.get("ok") or result.get("reason") != "engine_elsewhere":
        return _report_start(log, result)

    lease = result.get("lease") or {}
    if _pid_is_running(lease.get("pid")):
        log(f"[engine] not started here: {result.get('message')}")
        log("[engine] this dashboard will show that engine's state instead. "
            "Stop it first if you want the web app to own the engine.")
        return result

    log(f"[engine] the lease belongs to pid {lease.get('pid')}, which is no "
        f"longer running — taking the engine over.")
    return _report_start(log, jobs.start_live(force=True))


def stop_engine(jobs, log=print) -> None:
    """Stop the engine at shutdown, so it records that it stopped.

    Only meaningful for the engine this process started, and harmless
    otherwise: ``JobManager.shutdown`` returns immediately when nothing is
    running. It matters because the live worker is a daemon thread — killed
    with the process, its ``finally`` never runs, so the engine-state row keeps
    the last heartbeat and a fresh lease. For the next 25 seconds a restart
    then reads that lease as a live engine elsewhere and refuses to start,
    which is the correct refusal for a scanner that is genuinely running and a
    confusing one for the one just killed here.

    Stopping deliberately writes ``state=stopped`` with a **zero** lease, so
    the dashboard stops reporting the engine as alive the moment this returns
    rather than waiting the lease out.
    """
    shutdown = getattr(jobs, "shutdown", None)
    if not callable(shutdown):
        return
    try:
        if callable(getattr(jobs, "is_live_running", None)) and \
                jobs.is_live_running():
            log("[engine] stopping the scanner...")
        shutdown()
    except Exception as exc:  # a shutdown path must never mask the real exit
        log(f"[engine] could not stop cleanly: {type(exc).__name__}: {exc}")


def run_web(settings: Settings, autostart: bool | None = None) -> int:
    from app.web import create_app

    web_app = create_app(settings=settings)
    print(f"[web] dashboard at http://{settings.flask_host}:{settings.flask_port}")
    # Before the server starts serving, so the engine is connecting to the
    # terminal while the operator is still looking at the login page — and never
    # after, because ``app.run`` blocks.
    start_engine(web_app.config["JOBS"], settings, autostart=autostart)
    try:
        # The reloader MUST stay off. It forks a second process, and because the
        # dashboard can now start a live scanner that process would warm up its
        # own engine and broadcast DUPLICATE Telegram alerts for every setup.
        web_app.run(host=settings.flask_host, port=settings.flask_port,
                    debug=settings.flask_debug, use_reloader=False)
    finally:
        stop_engine(web_app.config["JOBS"])
    return 0


def run_serve(settings: Settings, autostart: bool | None = None) -> int:
    """Production WSGI server: the same app, behind Waitress.

    Waitress rather than Flask's development server because the latter is
    single-threaded and explicitly not for production; a handful of clients
    polling every five seconds would queue behind each other on it.

    ``threads`` comes from ``SERVE_THREADS`` and is deliberately modest. The
    client pages poll, so each open browser tab holds a thread for the duration
    of a request — never a held-open connection, which is the other reason
    polling was chosen over SSE for the live updates.

    Like ``run.py web``, this starts the engine before it starts serving, unless
    ``ENGINE_AUTOSTART`` is off or another process already holds the lease.
    """
    from app.web import create_app

    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print("[serve] Waitress is not installed. Run:\n"
              "    pip install -r requirements.txt\n"
              "or use `python run.py web` for local development.")
        return 2

    web_app = create_app(settings=settings)
    threads = max(1, int(getattr(settings, "serve_threads", 8) or 8))
    print(f"[serve] listening on {settings.flask_host}:{settings.flask_port} "
          f"with {threads} threads")
    if not (settings.flask_secret_key or "").strip():
        print("[serve] WARNING: FLASK_SECRET_KEY is not set. A temporary key "
              "means every restart signs everyone out. Set it in .env.")
    if not settings.session_cookie_secure:
        print("[serve] WARNING: SESSION_COOKIE_SECURE is false, so the session "
              "cookie would travel over plain HTTP. Set it to true once the "
              "site is behind HTTPS.")
    if settings.effective_auto_trading:
        print("[serve] WARNING: AUTO_TRADING is enabled — the engine may place "
              "live orders.")
    else:
        print("[serve] AUTO_TRADING is off: the platform is alert-only.")
    start_engine(web_app.config["JOBS"], settings, autostart=autostart)
    try:
        waitress_serve(web_app, host=settings.flask_host,
                       port=settings.flask_port, threads=threads)
    finally:
        stop_engine(web_app.config["JOBS"])
    return 0


# --------------------------------------------------------------------------- #
# Asset registry listing / smoke check
# --------------------------------------------------------------------------- #
def run_assets(settings: Settings, enable_all: bool = False,
               disable_all: bool = False) -> int:
    from trading.asset_manager import AssetManager

    manager = AssetManager(settings=settings)
    if enable_all or disable_all:
        changed = manager.set_all_enabled(enable_all)
        verb = "enabled" if enable_all else "disabled"
        print(f"[assets] {verb} {len(changed)} asset(s).")
    for asset in manager.list_assets():
        state = "ENABLED" if asset.enabled else "disabled"
        contract = (f"contract={asset.contract_size:g}"
                    if asset.contract_size != 1.0 else "contract=1 (index-style)")
        print(f"{asset.name:10s} -> {asset.broker_symbol:12s} [{state}] "
              f"digits={asset.digits} {contract}")
    print(f"\n{len(manager.enabled_assets())} of "
          f"{len(manager.list_assets())} enabled. "
          "Symbols are resolved against the broker at session start.")
    return 0


def run_smoke(settings: Settings) -> int:
    """No-MT5 self-check: imports, DB init, dashboard health, backtest round-trip."""
    print("[smoke] imports OK")
    init_db(settings)
    with Repository(settings=settings) as repo:
        repo.log_event("INFO", "smoke", "self-check ok")
        repo.upsert_asset("SMOKE", "SMOKE", enabled=False)
    print("[smoke] database OK")

    from app.web import create_app

    web_app = create_app(settings=settings)
    client = web_app.test_client()
    status = client.get("/health").status_code
    assert status == 200, f"dashboard /health returned {status}"
    print("[smoke] dashboard OK")
    return 0


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
def _read_password(settings: Settings, supplied: str | None) -> str | None:
    """The password to use, or ``None`` if it is unacceptable.

    Prompted rather than passed on the command line by default: an argument is
    visible in the process list and lands in the shell's history file, which for
    an account password is a worse leak than anything the app could do. The flag
    exists for scripted setup; the prompt is the normal path.
    """
    import getpass

    from app.auth import password_problem

    minimum = int(getattr(settings, "min_password_length", 10) or 10)
    if supplied:
        password = supplied
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("[user] the two passwords do not match.")
            return None

    problem = password_problem(password, minimum=minimum)
    if problem:
        print(f"[user] {problem}")
        return None
    return password


def run_user(settings: Settings, action: str | None, username: str | None,
             role: str, email: str, display_name: str,
             password: str | None) -> int:
    """Create and manage the accounts that can sign in to the platform.

    This is the only way to make the first administrator besides the
    ``ADMIN_USERNAME``/``ADMIN_PASSWORD`` bootstrap in ``.env``, and it is
    deliberately a command an operator runs on the server rather than anything
    reachable over the network. There is no public sign-up.
    """
    from database import models as m

    if not action:
        print("[user] which action? add | list | passwd | disable | enable")
        return 2

    init_db(settings)
    with Repository(settings=settings) as repo:
        if action == "list":
            rows = repo.list_users()
            if not rows:
                print("[user] no accounts yet. Create one with "
                      "`python run.py user add --username NAME --role admin`.")
                return 0
            print(f"{'id':>4}  {'username':<20} {'role':<8} {'status':<10} "
                  f"{'last login':<17} name")
            for u in rows:
                last = (u.last_login_at.strftime("%Y-%m-%d %H:%M")
                        if u.last_login_at else "never")
                print(f"{u.id:>4}  {u.username:<20} {u.role:<8} {u.status:<10} "
                      f"{last:<17} {u.display_name or ''}")
            print(f"\n{len(rows)} account(s).")
            return 0

        if action == "add":
            name = (username or "").strip()
            if not name:
                print("[user] --username is required.")
                return 2
            if repo.get_user_by_username(name) is not None:
                print(f"[user] the username {name!r} is already taken.")
                return 2
            role = (role or m.ROLE_CLIENT).strip().lower()
            if role not in m.ROLES:
                print(f"[user] unknown role {role!r}; expected one of "
                      f"{', '.join(sorted(m.ROLES))}.")
                return 2
            secret = _read_password(settings, password)
            if secret is None:
                return 2
            user = repo.create_user(
                username=name, password_hash_or_plain=secret, role=role,
                email=(email or "").strip(),
                display_name=(display_name or "").strip(), created_by="cli",
                subscribe=(role == m.ROLE_CLIENT))
            if role == m.ROLE_CLIENT:
                repo.update_client_profile(user.id,
                                           subscription_status=m.SUB_TRIAL)
            repo.log_event("INFO", "cli", f"created {role} {name!r} via run.py")
            print(f"[user] created {role} {name!r} (id {user.id}).")
            return 0

        # The remaining actions all target one existing account.
        target = (username or "").strip()
        if not target:
            print(f"[user] --username is required for {action}.")
            return 2
        user = repo.get_user_by_username(target)
        if user is None:
            print(f"[user] no account named {target!r}.")
            return 2

        if action == "passwd":
            secret = _read_password(settings, password)
            if secret is None:
                return 2
            repo.set_user_password(user.id, secret)
            # Logged as a warning for the same reason the admin reset is: a
            # credential change is exactly what an audit trail is for. The
            # password itself is never written anywhere.
            repo.log_event("WARN", "cli",
                           f"password reset for {target!r} via run.py")
            print(f"[user] password updated for {target!r}.")
            return 0

        if action in ("disable", "enable"):
            status = m.STATUS_ACTIVE if action == "enable" else m.STATUS_SUSPENDED
            repo.set_user_status(user.id, status)
            repo.log_event("INFO", "cli", f"{action}d {target!r} via run.py")
            print(f"[user] {target!r} is now {status}.")
            return 0

    print(f"[user] unknown action {action!r}.")
    return 2


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    parser.add_argument("command",
                        choices=["scan", "backtest", "web", "serve", "assets",
                                 "smoke", "user"],
                        help="which subsystem to run")
    parser.add_argument("--enable-all", action="store_true",
                        help="assets: enable every asset in the registry")
    parser.add_argument("--disable-all", action="store_true",
                        help="assets: disable every asset in the registry")
    parser.add_argument("--asset", default=None,
                        help="backtest: run one asset instead of all enabled")
    parser.add_argument("--force", action="store_true",
                        help="scan: start even if another process holds a live "
                             "engine lease (only when you know it is gone)")
    parser.add_argument("--no-engine", action="store_true",
                        help="web/serve: do not start the trading engine; show "
                             "whichever engine is already running")

    # `user` only. Kept as flags on the single command rather than a nested
    # subparser so every existing invocation keeps parsing exactly as before.
    parser.add_argument("user_action", nargs="?", default=None,
                        choices=["add", "list", "passwd", "disable", "enable"],
                        help="user: which account action to perform")
    parser.add_argument("--username", default=None,
                        help="user: the account to act on")
    parser.add_argument("--role", default="client",
                        help="user add: 'client' (default) or 'admin'")
    parser.add_argument("--email", default="", help="user add: email address")
    parser.add_argument("--display-name", default="",
                        help="user add: the name shown in the interface")
    parser.add_argument("--password", default=None,
                        help="user add/passwd: skip the prompt (visible in the "
                             "process list; prefer the prompt)")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.command == "assets":
        return run_assets(settings, enable_all=args.enable_all,
                          disable_all=args.disable_all)
    if args.command == "user":
        return run_user(settings, action=args.user_action,
                        username=args.username, role=args.role,
                        email=args.email, display_name=args.display_name,
                        password=args.password)
    if args.command == "backtest":
        return run_backtest(settings, asset=args.asset)
    if args.command == "scan":
        return run_scan(settings, force=args.force)
    if args.command in ("web", "serve"):
        # The engine starts with the app unless ENGINE_AUTOSTART is off. The
        # flag can only turn it *off*, never on, so a deployment that disabled
        # the setting cannot have an engine started by a stray argument.
        return (run_web if args.command == "web" else run_serve)(
            settings, autostart=False if args.no_engine else None)
    runner = {
        "backtest": run_backtest,
        "web": run_web,
        "serve": run_serve,
        "assets": run_assets,
        "smoke": run_smoke,
    }[args.command]
    return runner(settings)


if __name__ == "__main__":
    sys.exit(main())
