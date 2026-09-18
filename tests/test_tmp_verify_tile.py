"""Throwaway: render the console and print the account tile (deleted after use).

Two things changed underneath this diagnostic and are reflected here:

* the dashboard moved to ``/console`` and is now admin-only, so it signs in
  against a throwaway in-memory database — the real one is never written to;
* the live MT5 read needs a terminal, so it skips rather than fails on a machine
  that has none. It used to fail the whole suite on any server without MT5.

The account values it prints still come from the real ``JobManager`` and the
real terminal, which is the entire point of the diagnostic.
"""
import re
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.web import create_app
from config import get_settings
from database.models import Base
from database.repository import Repository

from test_web import _sign_in


def _throwaway_repo():
    """An in-memory database, so the diagnostic never touches the real file."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return Repository(session=maker())


def test_print_dashboard_account_tile():
    repo = _throwaway_repo()
    app = create_app(settings=replace(get_settings(),
                                      flask_secret_key="test-secret"),
                     repository=repo, setup_db=False)
    client = app.test_client()
    _sign_in(client, repo)               # the console is admin-only now

    resp = client.get("/console")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for pat, label in [
        (r'id="account-balance"[^>]*>\s*([^<]*)<', "balance tile"),
        (r'id="account-fetched"[^>]*>\s*([^<]*)<', "fetched"),
        (r'id="account-who"[^>]*>\s*([^<]*)<', "who"),
        (r'id="account-equity"[^>]*>\s*([^<]*)<', "equity tile"),
    ]:
        m = re.search(pat, html)
        print(f"  {label} = {m.group(1).strip()!r}" if m else f"  {label} NOT FOUND")


def _mt5_terminal_here() -> bool:
    """True when this machine has a reachable terminal to read an account from."""
    try:
        from trading.mt5_client import MT5Client
    except Exception:                   # pragma: no cover - import-time only
        return False
    client = MT5Client(get_settings())
    try:
        return bool(client.connect())
    except Exception:
        return False


@pytest.mark.skipif(not _mt5_terminal_here(),
                    reason="no MetaTrader 5 terminal on this machine")
def test_live_mt5_account_read():
    """Prove the server-side path returns the real MT5 account."""
    from runner import JobManager

    jm = JobManager()
    jm.request_account_refresh()
    state = jm.wait_for_account(timeout=30)
    jm.shutdown()
    print(f"  account state = {state}")
    assert state["state"] == "done", state
    assert state["balance"] is not None
