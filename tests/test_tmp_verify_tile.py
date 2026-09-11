"""Throwaway: render the dashboard and print the account tile (deleted after use)."""
import re

from app.web import create_app


def test_print_dashboard_account_tile():
    app = create_app()
    client = app.test_client()
    resp = client.get("/")
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
