"""Render every shell to a static snapshot directory for visual review.

Writes self-contained HTML (static assets copied alongside and /static/ rebased)
so a headless browser can screenshot the real templates with no server, no
database and no MT5 anywhere near them.

Run: python _snapshot.py [outdir]
"""
from __future__ import annotations

import hashlib
import re
import secrets
import shutil
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import RESET_TTL_MINUTES
from app.plans import spec_rows
from app.web import create_app
from config import get_settings
from database.models import ROLE_ADMIN, ROLE_CLIENT, Base
from database.repository import Repository
from trading import time_utils as tu

PASSWORD = "test-password-long-enough"
CSRF = "test-csrf"

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "snapshots")

ANON = [("home", "/"), ("features", "/features"), ("pricing", "/pricing"),
        ("about", "/about"), ("contact", "/contact"),
        ("login", "/login"), ("register", "/register"),
        ("forgot", "/forgot-password")]
CLIENT = [("dashboard", "/dashboard"), ("market", "/market"),
          ("setups", "/setups"), ("history", "/history"),
          ("analysis", "/analysis"), ("settings", "/settings"),
          ("subscription", "/subscription")]
ADMIN = [("admin-console", "/console"), ("admin-signals", "/console/signals"),
         ("admin-overview", "/admin"), ("admin-clients", "/admin/clients"),
         ("admin-subs", "/admin/subscriptions")]


def sign_in(client, repo, username, role, plan=None):
    user = repo.create_user(username=username, password_hash_or_plain=PASSWORD,
                            role=role, display_name=username,
                            subscribe=(role == ROLE_CLIENT))
    if plan:
        repo.sync_plans(spec_rows())
        p = repo.get_plan_by_key(plan)
        ref = f"snap-{plan}-{user.id}"
        repo.create_payment(user_id=user.id, plan=p, reference=ref,
                            amount_minor=p.price_minor, currency=p.currency)
        payment, _ = repo.settle_payment(ref, provider_tx_id=f"flw-{ref}",
                                         amount_minor=p.price_minor,
                                         currency=p.currency,
                                         payload={"status": "successful"})
        repo.activate_subscription(user_id=user.id, plan=p, payment=payment,
                                   period_days=30)
    client.get("/login")
    with client.session_transaction() as sess:
        sess["csrf"] = CSRF
    client.post("/login", data={"username": username, "password": PASSWORD,
                                "_csrf": CSRF})
    return user


def emit(client, pages, prefix=""):
    for name, url in pages:
        resp = client.get(url)
        body = resp.get_data(as_text=True)
        # Rebase /static/ so the file resolves next to the copied tree.
        body = body.replace('href="/static/', 'href="static/')
        body = body.replace('src="/static/', 'src="static/')
        body = body.replace('href="/static/', 'href="static/')
        (OUT / f"{prefix}{name}.html").write_text(body, encoding="utf-8")
        print(f"  {prefix}{name}.html  <- {url}  [{resp.status_code}]")


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "static").mkdir(parents=True)
    shutil.copytree("app/static/css", OUT / "static/css")
    shutil.copytree("app/static/js", OUT / "static/js")
    shutil.copytree("app/static/image", OUT / "static/image")

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    repo = Repository(session=sessionmaker(bind=engine, expire_on_commit=False,
                                           future=True)())
    app = create_app(settings=replace(get_settings(), flask_secret_key="snap"),
                     repository=repo, setup_db=False)

    anon = app.test_client()
    emit(anon, ANON)

    # Seed a little real content so the client pages are not all empty states.
    _seed(repo)
    client = app.test_client()
    sign_in(client, repo, "snap-client", ROLE_CLIENT, plan="vip")
    emit(client, CLIENT)

    admin = app.test_client()
    sign_in(admin, repo, "snap-admin", ROLE_ADMIN)
    emit(admin, ADMIN, prefix="")

    print(f"\n{OUT.resolve()}")
    return 0


def _seed(repo):
    """One signal and one backtest, so tables render rows rather than empty states."""
    from datetime import datetime

    import sys as _s
    _s.path.insert(0, "tests")
    from test_database import _signal

    repo.save_signal(_signal())
    repo.save_backtest(
        name="daily", asset="TEST", symbol="TEST",
        start_utc=datetime(2026, 1, 1), end_utc=datetime(2026, 1, 2),
        params={"min_rr": 1.5, "max_hold_m1": 720},
        summary={"n_signals": 2, "n_trades": 1, "n_open": 1, "n_wins": 1,
                 "n_losses": 0, "win_rate": 1.0,
                 "profit_factor": float("inf"), "total_r": 2.0,
                 "max_drawdown_r": 0.0,
                 "equity_curve": [["2026-01-01T14:00:00", 2.0]], "n_bars": 1440,
                 "first_entry_utc": "2026-01-01T13:10:00",
                 "last_exit_utc": "2026-01-01T14:00:00",
                 "by_month": {"2026-01": {"n_trades": 1, "n_wins": 1,
                                          "win_rate": 1.0, "total_r": 2.0,
                                          "expectancy": 2.0}}},
        trades=[dict(asset="TEST", direction="buy", entry=101.7, sl=99.5,
                     tp=106.0, exit_price=106.0,
                     entry_time_utc=datetime(2026, 1, 1, 13, 10),
                     exit_time_utc=datetime(2026, 1, 1, 14, 0),
                     outcome="WIN", pnl=2.0, rr=2.0, bars_held=50, reason="tp")])


if __name__ == "__main__":
    sys.exit(main())
