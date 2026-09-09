"""End-to-end strategy engine test with a fully synthetic BUY scenario.

Constructs two NY days of M1 data (with minute gaps allowed) that produce:

  PDL(100.0) day1 -> day2 H1 sweep below 100 to 99.6 & reclaim close
  -> M15 CISD confirmation (08:30 bucket) -> M1 bullish FVG -> retrace entry.

Everything is closed-candle, deterministic, and requires no MT5.
"""
from datetime import datetime

from trading.asset_manager import Asset
from trading.strategy import ICTStrategy

from conftest import c


def _ny(hour, minute, day):
    return datetime(2026, 1, day, hour, minute)


def build_scenario():
    """Returns (warm_candles, feed_candles)."""
    warm = []
    # ---------------- Day 1 (2026-01-05): sets PDH 108.0 / PDL 100.0 ---------
    day1 = [
        (0, 101.0, 101.5, 100.0, 101.2),
        (1, 101.2, 101.4, 101.0, 101.2),
        (2, 101.2, 101.4, 100.9, 101.1),
        (3, 101.1, 101.3, 100.9, 101.1),
        (4, 101.1, 108.0, 101.0, 101.2),   # day1 high 108.0
        (5, 101.2, 101.4, 101.0, 101.2),
        (6, 101.2, 101.3, 101.0, 101.1),
    ]
    for h, o, hi, lo, cl in day1:
        warm.append(c(_ny(h, 0, 5), o, hi, lo, cl))

    # ---------------- Day 2 morning (2026-01-06) -----------------------------
    day2_morning = [
        (0, 101.1, 101.5, 100.7, 101.3),
        (1, 101.3, 101.4, 100.9, 101.2),
        (2, 101.2, 101.4, 100.8, 101.1),
        (3, 101.1, 101.3, 100.8, 101.2),
        (4, 101.2, 101.4, 100.9, 101.2),
        (5, 101.2, 101.3, 100.9, 101.1),
        (6, 101.1, 101.3, 100.8, 101.1),
        (7, 101.1, 101.2, 100.6, 100.9),
        (8, 100.9, 101.1, 100.6, 101.0),     # closes H1 07; opens purge hour
    ]
    for h, o, hi, lo, cl in day2_morning:
        warm.append(c(_ny(h, 0, 6), o, hi, lo, cl))

    # Purge hour 08 content + M15 08:30 confirm bucket (all warm, detection off).
    purge_phase = [
        (8, 10, 101.0, 101.1, 100.4, 100.9),
        (8, 30, 100.1, 100.4, 99.6, 100.05),   # sweep below PDL
        (8, 44, 100.05, 100.5, 99.9, 100.4),   # M15 bucket closes bullish >100
        (8, 45, 100.4, 101.2, 100.3, 101.0),   # finalizes M15 08:30 bucket
    ]
    for h, m, o, hi, lo, cl in purge_phase:
        warm.append(c(_ny(h, m, 6), o, hi, lo, cl))

    # ---------------- Detection phase from NY 09:00 ---------------------------
    feed = [
        (9, 0, 101.0, 101.3, 100.4, 101.1),   # closes H1 08 -> purge premise
        (9, 1, 101.1, 101.3, 100.9, 101.2),
        (9, 2, 101.2, 101.3, 100.9, 101.1),
        (9, 3, 101.1, 101.3, 100.95, 101.2),
        (9, 4, 101.2, 101.55, 101.1, 101.3),
        (9, 5, 101.3, 101.4, 101.15, 101.35),   # A
        (9, 6, 101.35, 101.9, 101.3, 101.8),    # B displacement
        (9, 7, 101.8, 101.85, 101.55, 101.75),  # C -> bullish FVG [101.4,101.55]
        (9, 8, 101.75, 101.9, 101.6, 101.8),
        (9, 9, 101.8, 101.9, 101.5, 101.55),    # retrace into zone (bearish bar)
        (9, 10, 101.55, 101.75, 101.45, 101.7), # bullish reclaim -> ENTRY
    ]
    feed = [c(_ny(h, m, 6), o, hi, lo, cl) for h, m, o, hi, lo, cl in feed]
    return warm, feed


def test_buy_signal_full_pipeline():
    asset = Asset(name="TEST", broker_symbol="TEST", enabled=True,
                  overrides={"min_history_h1": "5"})
    engine = ICTStrategy(asset)
    warm, feed = build_scenario()
    engine.warm(warm)

    signals = []
    for candle in feed:
        signals += engine.feed(candle)

    buys = [s for s in signals if s.direction == "buy" and s.status == "APPROVED"]
    assert buys, f"expected an approved BUY signal, got {signals}"
    sig = buys[0]
    assert sig.cisd_tf == "M15"                      # purge before 09:00 NY
    assert sig.liquidity_type == "PDL"
    assert sig.fvg_direction == "bullish"
    assert sig.session_primary == "ny_am"
    assert sig.rr >= 1.5                             # risk manager gate passed
    assert sig.entry > sig.sl and sig.tp > sig.entry
    # The purge extreme must sit below the swept level (buy premise).
    assert sig.liquidity_price == 100.0
    assert sig.sl < sig.liquidity_price


def test_no_signals_without_history():
    asset = Asset(name="TEST", broker_symbol="TEST", enabled=True,
                  overrides={"min_history_h1": "5000"})
    engine = ICTStrategy(asset)
    warm, feed = build_scenario()
    engine.warm(warm)
    out = []
    for candle in feed:
        out += engine.feed(candle)
    assert out == []  # min history not satisfied => no premature signals
