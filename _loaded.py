"""Render the client pages with a populated account, for visual review.

_snapshot.py seeds exactly one signal, so every list on the dashboard is a
single row and every grid is a single card. That is the empty-ish case, not the
one customers see. This seeds a realistic spread — several confirmed setups
across assets and both directions, a rejected one, a pending one, and a full
engine activity feed — and emits the same self-contained HTML, so the layout can
be reviewed at the width it will actually be read at.

Run: python _loaded.py [outdir]
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import _snapshot as S
from app.web import create_app
from config import get_settings
from database.models import Base
from database.repository import Repository

OUT = S.OUT.__class__(sys.argv[1] if len(sys.argv) > 1 else "snapshots_loaded")

sys.path.insert(0, "tests")
from test_database import _signal  # noqa: E402

#: asset, direction, entry, stop, target, status, minutes after 13:10 UTC
SETUPS = [
    ("USTEC", "buy", 101.70, 99.50, 106.00, "APPROVED", 0),
    ("EURUSD", "sell", 1.08620, 1.08900, 1.07900, "APPROVED", 35),
    ("XAUUSD", "buy", 2412.4, 2401.0, 2440.0, "APPROVED", 70),
    ("GBPUSD", "sell", 1.27040, 1.27310, 1.26300, "APPROVED", 105),
    ("USDJPY", "buy", 157.220, 156.800, 158.200, "REJECTED", 140),
    ("AUDUSD", "buy", 0.66410, 0.66180, 0.66950, "PENDING", 175),
]

EVENTS = [
    ("info", "scanner", "Scanner started, 17 assets registered from the terminal."),
    ("info", "scanner", "Session window open: ny_am 09:30-11:00 NY."),
    ("info", "setup", "USTEC: 1H liquidity purge below PDL 100.00 confirmed."),
    ("info", "setup", "USTEC: M15 CISD confirmed, FVG 101.40-101.55 recorded."),
    ("info", "risk", "USTEC: risk approved at 0.5% of equity, R:R 2.0."),
    ("warning", "risk", "USDJPY: rejected — spread 4.2 pips above the 2.0 cap."),
    ("info", "setup", "EURUSD: 1H liquidity purge above PDH 1.08920 confirmed."),
    ("info", "scanner", "Poll completed: 17 assets, 4 states changed, 1.2s."),
]


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "static").mkdir(parents=True)
    shutil.copytree("app/static/css", OUT / "static/css")
    shutil.copytree("app/static/js", OUT / "static/js")
    shutil.copytree("app/static/image", OUT / "static/image")
    S.OUT = OUT

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    repo = Repository(session=sessionmaker(bind=engine, expire_on_commit=False,
                                           future=True)())
    app = create_app(settings=replace(get_settings(), flask_secret_key="loaded"),
                     repository=repo, setup_db=False)

    base = datetime(2026, 1, 6, 13, 10)
    for asset, direction, entry, stop, target, status, lag in SETUPS:
        t = base + timedelta(minutes=lag)
        repo.save_signal(_signal(
            asset=asset, direction=direction, entry=entry, sl=stop, tp=target,
            status=status,
            risk_approved=(status == "APPROVED"),
            alert_only=(status != "APPROVED"),
            entry_time_utc=t, entry_time_ny=t,
            purge_time_ny=t - timedelta(minutes=70),
            cisd_confirm_time_ny=t - timedelta(minutes=40),
            liquidity_price=round(entry - (0.4 if direction == "buy" else -0.4), 5),
            rr=2.0,
        ))
    for level, source, message in EVENTS:
        repo.log_event(level, source, message)

    client = app.test_client()
    S.sign_in(client, repo, "snap-client", "client", plan="vip")
    S.emit(client, [("dashboard", "/dashboard"), ("setups", "/setups"),
                    ("market", "/market"), ("history", "/history"),
                    ("analysis", "/analysis")])
    print(f"\n{OUT.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
