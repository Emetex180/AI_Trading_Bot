"""Verify the 3rader brand lockup is present and correct on every surface.

Renders one page per shell as an anonymous visitor, as a client and as an
operator, and checks three things on each:

  1. the 3rader wordmark is on the page,
  2. the document icon set is linked,
  3. every asset those references name actually serves 200.

Point 3 is the one that matters most: a renamed or missing PNG in
``static/image`` renders as a broken image in production but leaves the markup
assertions passing, so the references are fetched rather than just matched.

Run: python _verify_brand.py
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sys
from dataclasses import replace
from datetime import timedelta

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

#: Pages that must carry the brand, by shell. The reset-password page needs a
#: real token, so it is checked through the route that mints one below.
ANON_PAGES = ["/", "/features", "/pricing", "/about", "/contact",
              "/terms", "/privacy", "/risk", "/login", "/register",
              "/forgot-password"]
CLIENT_PAGES = ["/dashboard", "/market", "/setups", "/history", "/analysis",
                "/settings", "/subscription"]
ADMIN_PAGES = ["/console", "/console/signals", "/console/assets",
               "/console/backtests", "/admin", "/admin/clients",
               "/admin/subscriptions", "/admin/payments", "/admin/activity",
               "/admin/accounts"]

failures: list[str] = []


def check(label: str, body: str) -> None:
    if "image/3rader-logo.png" not in body:
        failures.append(f"{label}: no 3rader wordmark")
    if "image/favicon.ico" not in body:
        failures.append(f"{label}: favicon not linked")


def audit_assets(client, pages: list[str]) -> int:
    """Fetch every static asset referenced by these pages; count the unique ones."""
    seen: dict[str, int] = {}
    for page in pages:
        resp = client.get(page)
        if resp.status_code not in (200, 302):
            failures.append(f"{page}: HTTP {resp.status_code}")
            continue
        body = resp.get_data(as_text=True)
        check(page, body)
        for url in re.findall(r'(/static/[^"\']+)', body):
            seen.setdefault(url, 0)
    for url in seen:
        r = client.get(url)
        seen[url] = r.status_code
        if r.status_code != 200:
            failures.append(f"{url}: HTTP {r.status_code}")
    return len(seen)


def sign_in(client, repo, username, role, plan=None):
    user = repo.create_user(username=username, password_hash_or_plain=PASSWORD,
                            role=role, display_name=username,
                            subscribe=(role == ROLE_CLIENT))
    if plan:
        repo.sync_plans(spec_rows())
        p = repo.get_plan_by_key(plan)
        ref = f"verify-{plan}-{user.id}"
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
    resp = client.post("/login", data={"username": username,
                                       "password": PASSWORD, "_csrf": CSRF})
    assert resp.status_code == 302, f"sign-in for {username}: {resp.status_code}"
    return user


def main() -> int:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    repo = Repository(session=sessionmaker(bind=engine, expire_on_commit=False,
                                           future=True)())
    app = create_app(settings=replace(get_settings(), flask_secret_key="verify"),
                     repository=repo, setup_db=False)

    anon = app.test_client()
    assets = audit_assets(anon, ANON_PAGES)

    # The reset page is only reachable with a token; mint one exactly as the
    # forgot-password flow does (only the digest is stored) rather than skipping
    # the shell it uses.
    user = repo.create_user(username="reset-me", password_hash_or_plain=PASSWORD,
                            role=ROLE_CLIENT, display_name="reset-me")
    raw = secrets.token_urlsafe(32)
    repo.create_password_reset(
        user_id=user.id,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=tu.now_utc() + timedelta(minutes=RESET_TTL_MINUTES))
    audit_assets(anon, [f"/reset-password/{raw}"])

    client = app.test_client()
    sign_in(client, repo, "verify-client", ROLE_CLIENT, plan="vip")
    assets += audit_assets(client, CLIENT_PAGES)

    admin = app.test_client()
    sign_in(admin, repo, "verify-admin", ROLE_ADMIN)
    assets += audit_assets(admin, ADMIN_PAGES)

    # The bare request a browser makes unprompted, plus the icon files directly.
    for path in ["/favicon.ico", "/static/image/favicon.ico",
                 "/static/image/favicon-32.png",
                 "/static/image/apple-touch-icon.png",
                 "/static/image/3rader-logo.png",
                 "/static/image/3rader-mark.png"]:
        r = anon.get(path)
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200:
            failures.append(f"{path}: HTTP {r.status_code}")
        elif not ctype.startswith("image/"):
            failures.append(f"{path}: Content-Type {ctype!r}")
        else:
            print(f"  ok  {path:42} {r.status_code} {ctype} {len(r.data)}B")

    total = len(ANON_PAGES) + 1 + len(CLIENT_PAGES) + len(ADMIN_PAGES)
    print(f"\n{total} pages rendered, {assets} static assets referenced")

    if failures:
        print(f"\nFAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — wordmark and icons on every surface, every asset serves 200")
    return 0


if __name__ == "__main__":
    sys.exit(main())
