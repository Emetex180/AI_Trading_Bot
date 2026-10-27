"""Bring the stored plan catalogue in line with :mod:`app.plans`.

Why this is a script and not a startup step
-------------------------------------------
``Repository.sync_plans`` only *inserts* plan keys it has never seen. That is
deliberate: an operator who edits a plan through the admin area must keep that
edit across restarts. The flip side is that the public pages (rendered from
``app.plans.PLANS``) and the checkout and subscription page (read from the
stored ``plans`` rows) can disagree after a pricing or copy revision until the
stored row is updated.

This script is the explicit, auditable revision step. It is idempotent and
aligns each catalogue plan's **price, currency, description and feature
bullets** with :mod:`app.plans`. Names, levels, ordering and payment history are
left alone. Payments already recorded keep their own ``amount_minor``, so no
historical record is rewritten.

Run it once after a price or plan-copy change:

Usage
-----
    python scripts/sync_plan_catalogue.py            # dry run, prints the diff
    python scripts/sync_plan_catalogue.py --apply    # writes the changes

Run it against whatever database ``DATABASE_URL`` points at. It does not touch
Flutterwave, any AWS host, or the trading engine.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The script lives in scripts/; make ``app`` and ``config`` importable when it
# is run directly from the repository root or from anywhere else.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.plans import PLANS, format_minor  # noqa: E402
from config import get_settings  # noqa: E402
from database.repository import Repository, get_engine  # noqa: E402


def _one_line(text: str, width: int = 58) -> str:
    """A description is prose; the diff prints one line of it."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def _differences(row, spec) -> list[str]:
    """The fields on ``row`` that differ from the catalogue ``spec``."""
    diffs = []
    if (row.price_minor, (row.currency or "").upper()) != \
            (spec.price_minor, spec.currency.upper()):
        diffs.append(f"price {format_minor(row.price_minor, row.currency)}"
                     f" -> {format_minor(spec.price_minor, spec.currency)}")
    if (row.description or "") != spec.description:
        diffs.append(f"description: {_one_line(row.description)!r}"
                     f" -> {_one_line(spec.description)!r}")
    if list(row.features or []) != list(spec.features):
        diffs.append(f"features: {len(row.features or [])} bullets"
                     f" -> {len(spec.features)} bullets")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="write the catalogue values to the database (default: dry run)")
    args = parser.parse_args(argv)

    cfg = get_settings()
    engine = get_engine(cfg)
    repo = Repository(settings=cfg, engine=engine)
    changed = 0
    try:
        for spec in PLANS:
            row = repo.get_plan_by_key(spec.key)
            if row is None:
                print(f"{spec.key}: not in the database — start the app once "
                      f"to seed the catalogue, then re-run this script.")
                continue

            diffs = _differences(row, spec)
            if not diffs:
                print(f"{spec.key}: already in sync "
                      f"({format_minor(row.price_minor, row.currency)})")
                continue

            changed += 1
            print(f"{spec.key}:")
            for line in diffs:
                print(f"  {line}")
            if args.apply:
                repo.set_plan_fields(
                    spec.key,
                    price_minor=spec.price_minor,
                    currency=spec.currency,
                    description=spec.description,
                    features=list(spec.features),
                )

        if not args.apply and changed:
            print(f"\nDry run: {changed} plan(s) would change. "
                  f"Re-run with --apply to write.")
        elif not args.apply:
            print("\nDry run: nothing to change.")
        else:
            print(f"\nUpdated {changed} plan(s).")
        return 0
    finally:
        repo.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
