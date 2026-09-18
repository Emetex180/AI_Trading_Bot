"""Presentation helpers shared by the console, the client platform and admin.

Extracted from :mod:`app.web` when the client platform arrived and needed the
same price precision, the same New York rendering and the same status wording.
Kept as pure functions of their arguments (plus, where unavoidable, the current
app's configuration) so a template filter, a JSON API and a page render all
produce the same string for the same value — the alternative was a client page
that formatted a price differently from the console, which reads as a bug.

Nothing here queries the database or touches MT5. The only ambient read is the
asset registry in :func:`digits_for`, which is file-backed and cached.
"""
from __future__ import annotations

import os
from datetime import datetime
from math import isinf

from flask import current_app

from trading import time_utils as tu

#: Asset name -> price decimals, memoised against the registry's mtime.
_DIGITS_CACHE: dict[str, tuple[float, dict[str, int]]] = {}


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #
def num(value, digits: int = 4):
    """Fixed-precision number, or ``""`` when there is nothing to show."""
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def money(value, digits: int = 2):
    """An account figure with thousands separators.

    ``None`` means "never read from the terminal", which is deliberately not the
    same as a zero balance — the tile must be able to show an em dash rather than
    claim the account is empty.
    """
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def profit_factor(value):
    """Render a profit factor, collapsing an infinite (no-loss) value to ∞."""
    if value is None:
        return "-"
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return str(value)
    if isinf(fv):
        return "∞"
    return f"{fv:.2f}"


def ratio(value, digits: int = 2):
    """A risk/reward ratio, rendered as the ``1:2.4`` form the UI uses."""
    if value is None:
        return "—"
    try:
        return f"1:{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------------- #
# New York time
# --------------------------------------------------------------------------- #
def ny_str(naive_utc, fmt: str = "%Y-%m-%d %H:%M"):
    """Render a naive-UTC datetime on the New York clock (DST-aware)."""
    if naive_utc is None:
        return ""
    return tu.utc_to_ny(naive_utc).strftime(fmt)


def ny_time(naive_utc, fmt: str = "%H:%M"):
    """New York time of day, for tables where the date is implied."""
    return ny_str(naive_utc, fmt)


def ny_date(naive_utc, fmt: str = "%Y-%m-%d"):
    return ny_str(naive_utc, fmt)


def ny_zone_label(naive_utc=None) -> str:
    """``EDT`` / ``EST`` for an instant, so a displayed time states its zone.

    A bare "14:30" is ambiguous for half the year. The abbreviation is derived
    from the zone database rather than assumed, so it stays right across the DST
    boundary instead of being hard-coded to one offset.

    Read through :func:`trading.time_utils.ny_zone_abbr` rather than by
    formatting a NY wall clock: that clock is naive, and ``strftime("%Z")`` on a
    naive datetime is empty.
    """
    return tu.ny_zone_abbr(naive_utc)


def span(start, end) -> str:
    """Human duration between two datetimes, e.g. ``84 days, 3 h``.

    A window is stated as two dates, which does not tell you whether it covers a
    fortnight or two years — the figure that decides whether a result is worth
    reading. Days are dropped once the span is under a day so an intraday replay
    reads as hours and minutes rather than as "0 days".
    """
    if start is None or end is None or end <= start:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    days, rem = divmod(minutes, 60 * 24)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''}, {hours} h"
    if hours:
        return f"{hours} h, {mins} min"
    return f"{mins} min"


def iso_dt(value) -> datetime | None:
    """Parse an ISO timestamp stored in ``summary_json``, or ``None``.

    The summary carries these as strings because it is persisted as JSON, but
    every template renders them through the ``ny`` filter, which operates on
    datetimes. Converting here keeps that filter strict rather than teaching it
    to guess at strings.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Asset precision
# --------------------------------------------------------------------------- #
def digits_map() -> dict[str, int]:
    """Asset-name -> price decimals, from the registry the app is configured with.

    Cached against the registry file's mtime, so adding an asset through the
    dashboard shows up on the next render without a cache-busting call here.
    """
    try:
        cfg = current_app.config.get("CFG")
    except Exception:  # outside a request/app context (e.g. unit-testing a filter)
        cfg = None

    try:
        from trading.asset_manager import AssetManager

        path = str(getattr(cfg, "assets_file", "") or "")
        try:
            stamp = os.path.getmtime(path) if path else 0.0
        except OSError:
            stamp = 0.0
        cached = _DIGITS_CACHE.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]

        digits: dict[str, int] = {}
        for entry in AssetManager(settings=cfg).list_assets():
            if entry.digits:
                digits[entry.name.upper()] = int(entry.digits)
        _DIGITS_CACHE[path] = (stamp, digits)
        return digits
    except Exception:
        return {}  # an unreadable registry must not break page rendering


def digits_for(asset: str | None, default: int = 4) -> int:
    """Price decimals for an asset.

    Four decimals is right for EURUSD and wrong for USDJPY (3), gold (2) and an
    index (1–2). The registry is authoritative, and it is what the scanner and
    backtester read, so a displayed price matches the traded one.
    """
    return digits_map().get((asset or "").strip().upper(), default)


def price(value, asset: str | None = None):
    """Render a price with the asset's own precision."""
    return num(value, digits_for(asset))


# --------------------------------------------------------------------------- #
# Statuses
# --------------------------------------------------------------------------- #
_STATUS_LABELS = {
    "APPROVED": "Approved", "REJECTED": "Rejected", "PENDING": "Pending",
    "SENT": "Sent", "SKIPPED": "Skipped", "FAILED": "Failed",
    "AI_UNAVAILABLE": "AI Unavailable",
}


def status_label(status: str) -> str:
    return _STATUS_LABELS.get(status or "", status or "-")
