"""End-to-end strategy engine tests.

Synthetic, closed-candle, MT5-free scenarios that drive the full model:

    1H liquidity purge → M5 CISD → first qualifying 1M FVG → retrace entry

The BUY and SELL scenarios are built by one generator that places the same
price geometry at a caller-chosen minute of the NY day, which lets a test put
the entry inside a specific session window (NY AM, NY Lunch, NY Premarket)
without hand-writing a second day of candles.

Timestamps are constructed on the **NY clock** (``c()`` takes NY time and the
stream keys buckets on UTC), so the scenarios exercise the real conversion path
rather than a pre-shifted approximation.
"""
from datetime import datetime, timedelta

import pytest

from trading.asset_manager import Asset
from trading.fvg import FVG, classify, closes_inside, entered, fvg_at_three
from trading.liquidity import Level
from trading.strategy import (
    CISD_CONFIRMED,
    FVG_FOUND,
    INVALIDATED,
    LIQUIDITY_PURGED,
    NO_SETUP,
    RETRACE_CONFIRMED,
    TRADE_CONFIRMED,
    WAITING_FOR_FVG_RETRACE,
    ICTStrategy,
)

from conftest import c


DAY1 = 5      # 2026-01-05 — establishes PDH 108.0 / PDL 100.0
DAY2 = 6      # 2026-01-06


def _ny(hour, minute, day):
    return datetime(2026, 1, day, hour, minute)


# --------------------------------------------------------------------------- #
# Scenario generator — BUY
# --------------------------------------------------------------------------- #
#: Day 1 hourly candles: hour 4 prints the 108.0 high, hour 5 the 100.0 low.
_DAY1_BUY = [
    (0, 101.0, 101.5, 100.8, 101.2),
    (1, 101.2, 101.4, 101.0, 101.2),
    (2, 101.2, 101.4, 100.9, 101.1),
    (3, 101.1, 101.3, 100.9, 101.1),
    (4, 101.1, 108.0, 101.0, 101.2),   # day-1 high
    (5, 101.2, 101.4, 100.0, 101.2),   # day-1 low
    (6, 101.2, 101.3, 101.0, 101.1),
]

#: Day 2 hourly candles 00:00-08:00, drifting sideways above the day-1 low.
_DAY2_BUY = [
    (0, 101.1, 101.5, 100.7, 101.3),
    (1, 101.3, 101.4, 100.9, 101.2),
    (2, 101.2, 101.4, 100.8, 101.1),
    (3, 101.1, 101.3, 100.8, 101.2),
    (4, 101.2, 101.4, 100.9, 101.2),
    (5, 101.2, 101.3, 100.9, 101.1),
    (6, 101.1, 101.3, 100.8, 101.1),
    (7, 101.1, 101.2, 100.6, 100.9),
    (8, 100.9, 101.1, 100.6, 101.0),
]

#: The purge hour's own minutes: 08:10 dips, 08:30 sweeps below the 100.0 PDL
#: (low 99.6 — which is also the level the stop is anchored to), 08:44 reclaims
#: it, and 08:45 closes the hour.
#:
#: The 08:44 reclaim is kept here deliberately. It is a genuine bullish reclaim
#: of the swept level and it was the CISD under the old rule — but the 1H candle
#: it sits inside had not closed yet, so it must now confirm nothing. See
#: ``test_a_cisd_before_the_purge_candle_closes_is_rejected``.
_PURGE_HOUR_BUY = [
    (8, 10, 101.0, 101.1, 100.4, 100.9),
    (8, 30, 100.1, 100.4, 99.6, 100.05),   # sweep below the PDL
    (8, 44, 100.05, 100.5, 99.9, 100.4),   # reclaim inside the still-open hour
    (8, 45, 100.4, 101.2, 100.3, 101.0),   # finalizes the H1 08:00 candle
]

#: The first 5M bucket to *open* after the purge hour closed (09:00-09:05 NY).
#: That is the earliest candle which may confirm the purge, so it is where a
#: valid CISD lives: 09:01 dips back below the swept 100.0 and the bucket closes
#: bullish at 100.8. Its first minute is also the tick that finalizes the H1
#: 08:00 candle, which is what starts the episode.
_CISD_BUCKET_BUY = [
    (9, 0, 100.6, 100.8, 100.2, 100.5),
    (9, 1, 100.5, 100.6, 99.8, 100.0),
    (9, 2, 100.0, 100.4, 99.9, 100.3),
    (9, 3, 100.3, 100.6, 100.1, 100.5),
    (9, 4, 100.5, 100.9, 100.4, 100.8),
]

#: Flat run between the CISD and the FVG. The low of 100.4 keeps every
#: consecutive triple from leaving a gap — a low above the previous high *is* an
#: FVG — so the first qualifying gap really is the one placed at ``fvg_at``.
_QUIET_BUY = (101.0, 101.2, 100.4, 101.1)

#: A (c0), B (displacement), C (c2) — C's low sits above A's high, leaving the
#: bullish FVG [A.high, C.low] = [101.20, 101.55]. B's low stays under A's high
#: so the pattern is not detected one candle early.
_FVG_BUY = [
    (101.15, 101.20, 101.10, 101.15),
    (101.05, 101.90, 101.00, 101.85),
    (101.80, 101.85, 101.55, 101.75),
]
#: One transition candle: reaches nothing, invalidates nothing. This is the tick
#: on which FVG_FOUND becomes WAITING_FOR_FVG_RETRACE.
_SETTLE_BUY = (101.70, 101.80, 101.60, 101.75)
#: A wick into the gap — the low of 101.30 is inside [101.20, 101.55] — on a
#: *bearish* body, so it confirms the retracement and deliberately stops there.
#: RETRACE_CONFIRMED, not TRADE_CONFIRMED.
_RETRACE_BUY = (101.60, 101.62, 101.30, 101.40)
#: Bullish body closing *inside* the gap at 101.45 -> the entry trigger.
_ENTRY_BUY = (101.35, 101.50, 101.25, 101.45)


def _minutes(start_hm, count, day=DAY2):
    hour, minute = start_hm
    base = _ny(hour, minute, day)
    return [base + timedelta(minutes=i) for i in range(count)]


def build_buy_scenario(fvg_at=(9, 29), quiet_until=None,
                       day1=DAY1, day2=DAY2):
    """Return ``(warm, feed)`` for a BUY that enters five minutes after ``fvg_at``.

    ``fvg_at`` is the (hour, minute) the FVG's first candle opens, and must be at
    least 09:07 NY: the CISD bucket occupies 09:00-09:05 and the pattern needs a
    settled candle on either side of it. Five minutes then covers the three
    pattern candles, one transition candle, the retracement candle and the entry.
    ``quiet_until`` optionally extends the flat run before the FVG, which is how
    the session tests push the entry into a later window. ``day1``/``day2`` are
    days of January 2026, which is how the calendar tests land the same geometry
    on a weekend; the defaults are the Mon/Tue pair every other test uses.
    """
    warm = [c(_ny(h, 0, day1), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY1_BUY]
    warm += [c(_ny(h, 0, day2), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY2_BUY]
    warm += [c(_ny(h, m, day2), o, hi, lo, cl) for h, m, o, hi, lo, cl in _PURGE_HOUR_BUY]

    # 09:00 closes the H1 08:00 candle (so the purge is detected there) and opens
    # the 09:00-09:05 bucket, whose remaining minutes are the CISD.
    feed = [c(_ny(h, m, day2), o, hi, lo, cl) for h, m, o, hi, lo, cl in _CISD_BUCKET_BUY]

    last_quiet = quiet_until or _ny(fvg_at[0], fvg_at[1], day2) - timedelta(minutes=1)
    for ts in _minutes((9, 5), 24 * 60, day=day2):
        if ts > last_quiet:
            break
        feed.append(c(ts, *_QUIET_BUY))

    for i, (o, hi, lo, cl) in enumerate(_FVG_BUY):
        feed.append(c(_ny(fvg_at[0], fvg_at[1], day2) + timedelta(minutes=i), o, hi, lo, cl))
    for o, hi, lo, cl in (_SETTLE_BUY, _RETRACE_BUY, _ENTRY_BUY):
        feed.append(c(feed[-1].t_ny + timedelta(minutes=1), o, hi, lo, cl))
    return warm, feed


# --------------------------------------------------------------------------- #
# Scenario generator — SELL
# --------------------------------------------------------------------------- #
_DAY1_SELL = [
    (0, 92.0, 92.5, 91.0, 92.2),
    (1, 92.2, 92.4, 91.5, 92.2),
    (2, 92.2, 92.4, 91.6, 92.0),
    (3, 92.0, 92.3, 91.6, 92.0),
    (4, 92.0, 100.0, 91.8, 92.2),   # day-1 high 100.0
    (5, 92.2, 92.4, 90.0, 91.9),    # day-1 low 90.0
    (6, 91.9, 92.1, 91.7, 92.0),
]

_DAY2_SELL = [
    (0, 98.8, 99.0, 98.6, 98.9),
    (1, 98.9, 99.0, 98.6, 98.8),
    (2, 98.8, 99.1, 98.6, 98.9),
    (3, 98.9, 99.0, 98.7, 98.8),
    (4, 98.8, 99.1, 98.6, 98.9),
    (5, 98.9, 99.0, 98.7, 98.8),
    (6, 98.8, 99.0, 98.6, 98.8),
    (7, 98.8, 99.1, 98.7, 98.9),
    (8, 98.9, 99.0, 98.7, 98.8),
]

#: 08:30 sweeps above the 100.0 PDH (high 100.4 — the stop anchor), 08:40 is a
#: bearish reclaim of it *inside the still-open hour* (so it must no longer
#: confirm), and 08:45 closes the hour at 98.8 so the H1 purge confirms.
_PURGE_HOUR_SELL = [
    (8, 10, 98.9, 99.1, 98.8, 99.0),
    (8, 30, 99.2, 100.4, 99.1, 100.2),
    (8, 40, 100.2, 100.3, 99.3, 99.4),   # reclaim inside the still-open hour
    (8, 45, 99.4, 99.5, 98.7, 98.8),
]

#: The first 5M bucket after the purge hour closed: 09:01 spikes back above the
#: swept 100.0 and the bucket closes bearish at 98.9.
_CISD_BUCKET_SELL = [
    (9, 0, 99.4, 99.6, 99.2, 99.3),
    (9, 1, 99.3, 100.2, 99.3, 99.8),
    (9, 2, 99.8, 99.9, 99.4, 99.5),
    (9, 3, 99.5, 99.7, 99.1, 99.2),
    (9, 4, 99.2, 99.4, 98.8, 98.9),
]

#: High 99.3 is above the previous low (98.8), so no triple leaves a bearish gap.
_QUIET_SELL = (98.9, 99.3, 98.8, 98.95)

#: Bearish FVG [C.high, A.low] = [98.40, 98.80].
#:
#: The gap sits *below* the day's London low (98.6, built from the 02:00-05:00
#: hours above), deliberately: the retracement entry has to land under it or that
#: low becomes the nearest sell-side pull and the take profit collapses onto the
#: entry. With the entry at 98.50 the nearest valid target is the day-1 low, as
#: the pipeline test asserts.
_FVG_SELL = [
    (98.85, 98.90, 98.80, 98.80),
    (98.80, 98.95, 98.10, 98.20),
    (98.20, 98.40, 98.00, 98.10),
]
_SETTLE_SELL = (98.15, 98.25, 98.05, 98.10)    # reaches nothing, invalidates nothing
_RETRACE_SELL = (98.50, 98.70, 98.45, 98.65)   # into the gap, but bullish -> retrace only
_ENTRY_SELL = (98.60, 98.68, 98.40, 98.50)     # bearish close inside the gap -> ENTRY


def build_sell_scenario(fvg_at=(9, 29)):
    warm = [c(_ny(h, 0, DAY1), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY1_SELL]
    warm += [c(_ny(h, 0, DAY2), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY2_SELL]
    warm += [c(_ny(h, m, DAY2), o, hi, lo, cl) for h, m, o, hi, lo, cl in _PURGE_HOUR_SELL]

    feed = [c(_ny(h, m, DAY2), o, hi, lo, cl) for h, m, o, hi, lo, cl in _CISD_BUCKET_SELL]
    last_quiet = _ny(fvg_at[0], fvg_at[1], DAY2) - timedelta(minutes=1)
    for ts in _minutes((9, 5), 24 * 60):
        if ts > last_quiet:
            break
        feed.append(c(ts, *_QUIET_SELL))

    for i, (o, hi, lo, cl) in enumerate(_FVG_SELL):
        feed.append(c(_ny(fvg_at[0], fvg_at[1], DAY2) + timedelta(minutes=i), o, hi, lo, cl))
    for o, hi, lo, cl in (_SETTLE_SELL, _RETRACE_SELL, _ENTRY_SELL):
        feed.append(c(feed[-1].t_ny + timedelta(minutes=1), o, hi, lo, cl))
    return warm, feed


def build_scenario():
    """The canonical BUY scenario, with the entry at 09:33 NY (NY AM).

    Shared with the scanner and backtest suites: the entry deliberately lands in
    a session the spec marks unambiguously tradeable, so those tests do not
    depend on whether NY Premarket appears in the configured allow-list.
    """
    return build_buy_scenario()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
#: Every session the spec marks tradeable, pinned explicitly so a test never
#: depends on the developer's `.env` VALID_ENTRY_SESSIONS.
_ALL_SESSIONS = "london_open,ny_premarket,ny_am,london_close,ny_pm"


def _engine(asset_name="TEST", **overrides):
    """An engine that skips the long history warm-up and logs nothing."""
    settings = {"min_history_h1": "5", "valid_entry_sessions": _ALL_SESSIONS}
    settings.update(overrides)
    asset = Asset(name=asset_name, broker_symbol=asset_name, enabled=True,
                  digits=2, overrides=settings)
    return ICTStrategy(asset)


def _run(engine, warm, feed):
    engine.warm(warm)
    signals = []
    for candle in feed:
        signals += engine.feed(candle)
    return signals


def _approved(signals, direction):
    return [s for s in signals if s.direction == direction and s.status == "APPROVED"]


# --------------------------------------------------------------------------- #
# BUY pipeline
# --------------------------------------------------------------------------- #
def test_buy_signal_full_pipeline():
    engine = _engine()
    warm, feed = build_buy_scenario()
    signals = _run(engine, warm, feed)

    buys = _approved(signals, "buy")
    assert buys, "expected an approved BUY signal"
    sig = buys[0]

    # --- lineage ------------------------------------------------------- #
    assert sig.liquidity_type == "PDL"
    assert sig.liquidity_price == 100.0      # the swept level
    assert sig.purge_grade == "VERY_HIGH"    # previous-day low
    assert sig.cisd_tf == "M5"               # the model fixes CISD to M5
    assert sig.fvg_direction == "bullish"
    assert sig.fvg_lower == pytest.approx(101.20)
    assert sig.fvg_upper == pytest.approx(101.55)
    assert sig.setup_id                     # deterministic identity stamped

    # --- geometry ------------------------------------------------------ #
    # The entry is the confirming close, and it sits inside the gap: 101.45 is
    # within [101.20, 101.55]. An entry outside the FVG is not reachable.
    assert sig.entry == pytest.approx(101.45)
    assert sig.fvg_lower <= sig.entry <= sig.fvg_upper
    assert sig.entry > sig.sl and sig.tp > sig.entry
    # SL is anchored to the 5M candle that took the liquidity (low 99.6).
    assert sig.structure_extreme_price == pytest.approx(99.6)
    assert sig.sl < sig.structure_extreme_price
    assert sig.risk_points == pytest.approx(sig.entry - sig.sl)
    assert sig.reward_points == pytest.approx(sig.tp - sig.entry)
    assert sig.rr >= 1.5
    # TP is the nearest valid buy-side liquidity pull — the day-1 high.
    assert sig.target_kind == "PDH"
    assert sig.target_price == pytest.approx(108.0)
    assert sig.tp < sig.target_price         # placed slightly before the level

    # --- scoring / session --------------------------------------------- #
    assert 0 < sig.efficiency_score <= 100
    assert sig.session_primary == "ny_am"
    assert sig.state == "TRADE_CONFIRMED"
    assert sig.risk_approved is True


def test_sell_signal_full_pipeline():
    engine = _engine()
    warm, feed = build_sell_scenario()
    signals = _run(engine, warm, feed)

    sells = _approved(signals, "sell")
    assert sells, "expected an approved SELL signal"
    sig = sells[0]

    assert sig.liquidity_type == "PDH"
    assert sig.liquidity_price == 100.0
    assert sig.purge_grade == "VERY_HIGH"
    assert sig.fvg_direction == "bearish"
    assert sig.setup_id

    assert sig.entry == pytest.approx(98.50)
    assert sig.fvg_lower <= sig.entry <= sig.fvg_upper
    assert sig.sl > sig.entry and sig.tp < sig.entry
    # The liquidity-taking candle is the one that spiked to 100.4.
    assert sig.structure_extreme_price == pytest.approx(100.4)
    assert sig.sl > sig.structure_extreme_price
    assert sig.rr >= 1.5
    # Nearest sell-side pull below the entry: the day-1 low at 90.0. The London
    # low at 98.6 sits *above* the entry, so it is not a target for a short.
    assert sig.target_price == pytest.approx(90.0)
    assert sig.tp > sig.target_price
    assert sig.session_primary == "ny_am"


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
def test_state_progresses_through_every_stage():
    engine = _engine()
    warm, feed = build_buy_scenario(fvg_at=(9, 10))
    engine.warm(warm)

    seen = []
    for candle in feed:
        engine.feed(candle)
        state = engine.state("buy")
        if not seen or seen[-1] != state:
            seen.append(state)

    # Every stage is observed on its own candle, in order — including
    # LIQUIDITY_PURGED, which is now visible because the CISD may not be sought
    # inside the purge hour, and RETRACE_CONFIRMED, which the scenario produces
    # via a wick into the gap on a candle that does not qualify as an entry.
    assert seen == [
        LIQUIDITY_PURGED,
        CISD_CONFIRMED,
        FVG_FOUND,
        WAITING_FOR_FVG_RETRACE,
        RETRACE_CONFIRMED,
        TRADE_CONFIRMED,
    ]


def test_idle_state_is_no_setup():
    engine = _engine()
    # Nothing but a few candles: no purge has been detected for either side.
    engine.warm([c(_ny(9, m, DAY2), 100.0, 100.2, 99.8, 100.1) for m in range(10)])
    assert engine.states() == {"buy": NO_SETUP, "sell": NO_SETUP}


def test_engine_exposes_state_per_direction():
    engine = _engine()
    warm, feed = build_buy_scenario()
    _run(engine, warm, feed)
    states = engine.states()
    assert states["buy"] == TRADE_CONFIRMED   # the setup completed and alerted
    assert states["sell"] == NO_SETUP


def test_no_signals_without_history():
    engine = _engine(min_history_h1="5000")
    warm, feed = build_buy_scenario()
    assert _run(engine, warm, feed) == []


# --------------------------------------------------------------------------- #
# Invalidation rules
# --------------------------------------------------------------------------- #
def test_setup_reaching_entry_in_the_midday_gap_never_trades():
    """A setup that would enter inside 11:00-13:00 must not produce a signal.

    The FVG is placed at 11:29 so the retrace entry falls at 11:33, inside the
    gap between NY AM (07:00-11:00) and NY PM (13:00-15:00). The minute is
    ``outside_session``, so the entry is refused; the gap is also longer than
    ``retrace_wait_m1``, so the pending setup expires before NY PM opens rather
    than carrying a stale FVG across two dead hours.
    """
    engine = _engine(fvg_wait_m1="400")
    warm, feed = build_buy_scenario(fvg_at=(11, 29))
    signals = _run(engine, warm, feed)

    assert _approved(signals, "buy") == []
    assert engine.state("buy") == INVALIDATED


def test_setup_invalidated_when_no_fvg_forms_in_time():
    """The FVG search is time-boxed; an expired setup is dropped, not held."""
    engine = _engine(fvg_wait_m1="5")
    warm, feed = build_buy_scenario(fvg_at=(9, 29))
    signals = _run(engine, warm, feed)

    assert signals == []
    assert engine.state("buy") == INVALIDATED


def test_no_signal_when_the_target_is_behind_the_entry():
    """With no resting liquidity ahead of price there is nothing to target."""
    engine = _engine(min_tp_liquidity_grade="VERY_HIGH")
    # Removing the day-1 high leaves only weak levels above the entry, so a
    # VERY_HIGH-only target search finds nothing.
    warm, feed = build_buy_scenario()
    warm = [candle for candle in warm
            if not (candle.t_ny.day == DAY1 and candle.t_ny.hour == 4)]
    signals = _run(engine, warm, feed)
    assert _approved(signals, "buy") == []


def test_rr_gate_rejects_a_setup_below_the_minimum():
    engine = _engine(min_rr="50")
    warm, feed = build_buy_scenario()
    signals = _run(engine, warm, feed)

    assert signals == []          # nothing clears a 50:1 reward:risk
    # The setup got as far as the retracement and stopped there: an RR refusal
    # leaves the episode alive rather than invalidating it.
    assert engine.state("buy") == RETRACE_CONFIRMED


def test_ny_am_is_rejected_when_the_allowlist_excludes_it():
    """A session missing from ``VALID_ENTRY_SESSIONS`` can never enter.

    The scenario enters at 09:05, inside NY AM (07:00-11:00). Leaving ``ny_am``
    out of the allow-list must refuse it even though the window is open and
    tradeable.
    """
    engine = _engine(valid_entry_sessions="london_open,ny_pm")
    warm, feed = build_buy_scenario(fvg_at=(9, 10))
    signals = _run(engine, warm, feed)
    assert _approved(signals, "buy") == []


def test_ny_am_allowlisted_approves():
    """Same setup, with NY AM allow-listed: it is an outright ``yes`` session."""
    engine = _engine(valid_entry_sessions="ny_am")
    warm, feed = build_buy_scenario(fvg_at=(9, 10))
    signals = _run(engine, warm, feed)

    buys = _approved(signals, "buy")
    assert buys, "NY AM is a tradeable session and is allow-listed"
    assert buys[0].session_primary == "ny_am"


def test_a_legacy_allowlist_still_approves_ny_am():
    """``ny_premarket`` was folded into NY AM; the alias must keep it working."""
    engine = _engine(valid_entry_sessions="london_open,ny_premarket")
    warm, feed = build_buy_scenario(fvg_at=(9, 10))
    signals = _run(engine, warm, feed)
    assert _approved(signals, "buy"), "ny_premarket must resolve to ny_am"


def test_enforce_sessions_false_lets_a_blocked_window_through():
    engine = _engine(enforce_sessions="false")
    warm, feed = build_buy_scenario(fvg_at=(9, 10))
    signals = _run(engine, warm, feed)
    assert _approved(signals, "buy")


# --------------------------------------------------------------------------- #
# Duplicate prevention
# --------------------------------------------------------------------------- #
def test_duplicate_setup_is_never_emitted_twice():
    """Re-feeding the same scenario must not produce a second alert.

    A second engine over the identical candles yields the same deterministic
    setup id, and the engine refuses to emit it twice — the guard behind the
    scanner's duplicate suppression.
    """
    engine = _engine()
    warm, feed = build_buy_scenario()
    signals = _run(engine, warm, feed)
    assert len(_approved(signals, "buy")) == 1

    # Poison the guard: pretend this setup was already emitted, then re-run the
    # exact candles that produced it.
    setup_id = _approved(signals, "buy")[0].setup_id
    engine2 = _engine()
    engine2.warm(warm)
    engine2._triggered.add(setup_id)
    for candle in feed:
        engine2.feed(candle)
    assert engine2._triggered == {setup_id}      # nothing new was stamped
    assert engine2.state("buy") == RETRACE_CONFIRMED


def test_setup_id_is_stable_across_engines():
    a = _approved(_run(_engine(), *build_buy_scenario()), "buy")[0]
    b = _approved(_run(_engine(), *build_buy_scenario()), "buy")[0]
    assert a.setup_id == b.setup_id
    assert a.fingerprint() == a.setup_id


def test_setup_id_differs_for_a_different_cisd_candle():
    """A different CISD means a different setup, even at the same entry time."""
    from trading.signal_engine import build_setup_id

    t = datetime(2026, 1, 6, 14, 0)
    base = build_setup_id("TEST", "buy", t, t, t)
    assert base != build_setup_id("TEST", "buy", t, t + timedelta(minutes=5), t)
    assert base != build_setup_id("TEST", "sell", t, t, t)
    assert base != build_setup_id("OTHER", "buy", t, t, t)


# --------------------------------------------------------------------------- #
# Per-asset isolation
# --------------------------------------------------------------------------- #
def test_setups_are_isolated_per_engine():
    """Two engines over identical candles keep entirely separate setups.

    The engine is instantiated per asset, so this is what guarantees that one
    symbol's purge cannot open a setup on another: same candles in, and the
    engine whose history requirement is unmet stays idle while the other trades.
    """
    active = _engine("ACTIVE")
    starved = _engine("STARVED", min_history_h1="5000")
    warm, feed = build_buy_scenario()

    active.warm(warm)
    starved.warm(warm)

    signals = []
    for candle in feed:
        signals += active.feed(candle)
        starved.feed(candle)

    assert _approved(signals, "buy"), "the engine with history must trade"
    assert active.state("buy") == TRADE_CONFIRMED
    assert starved.state("buy") == NO_SETUP


def test_episode_state_is_not_shared_between_engines():
    """The per-direction episodes live on the instance, not the class."""
    a = _engine("A")
    b = _engine("B")
    warm, feed = build_buy_scenario(fvg_at=(9, 10))
    a.warm(warm)
    b.warm(warm)

    for candle in feed:
        a.feed(candle)

    assert a.state("buy") == TRADE_CONFIRMED
    assert b.state("buy") == NO_SETUP       # never fed, never advanced


def test_two_assets_can_hold_different_states_simultaneously():
    a = _engine("A")
    b = _engine("B")
    warm, feed = build_buy_scenario(fvg_at=(9, 10))
    a.warm(warm)
    b.warm(warm)

    # Everything except the final (entry) minute.
    for candle in feed[:-1]:
        a.feed(candle)
        b.feed(candle)
    assert a.state("buy") == b.state("buy") == RETRACE_CONFIRMED

    # Only A receives the entry minute.
    a.feed(feed[-1])
    assert a.state("buy") == TRADE_CONFIRMED    # A traded
    assert b.state("buy") == RETRACE_CONFIRMED  # B untouched, still waiting


# --------------------------------------------------------------------------- #
# A confirmed setup is protected from a later purge
# --------------------------------------------------------------------------- #
def test_a_confirmed_setup_is_not_replaced_by_a_later_purge():
    """§3: the first qualifying FVG stands until an invalidation rule clears it.

    A fresh 1H purge of the same side must not silently discard a setup whose
    FVG has formed and which is waiting for its retrace.
    """
    lines = []
    engine = _engine()
    engine.log = lines.append
    warm, feed = build_buy_scenario()

    _run(engine, warm, feed[:-1])              # everything but the entry minute
    assert engine.state("buy") == RETRACE_CONFIRMED

    episode = engine._episodes["buy"]
    fvg_before = episode.fvg
    purge_before = (episode.purge_kind, episode.purge_price)

    # The same sell-side level, swept again on a later 1H close.
    engine._start_episode(
        "buy",
        Level(kind=episode.purge_kind, price=episode.purge_price,
              time_ny=_ny(10, 0, DAY2)),
        c(_ny(10, 0, DAY2), 101.0, 101.2, 99.5, 100.6))

    assert engine.state("buy") == RETRACE_CONFIRMED
    assert engine._episodes["buy"].fvg == fvg_before
    assert (engine._episodes["buy"].purge_kind,
            engine._episodes["buy"].purge_price) == purge_before
    assert any("ignored" in m and "already has its FVG" in m for m in lines), \
        f"the refusal was not logged: {lines}"


def test_the_refusal_names_the_setup_it_protected():
    lines = []
    engine = _engine()
    engine.log = lines.append
    warm, feed = build_buy_scenario()
    _run(engine, warm, feed[:-1])

    episode = engine._episodes["buy"]
    engine._start_episode(
        "buy",
        Level(kind=episode.purge_kind, price=episode.purge_price,
              time_ny=_ny(10, 0, DAY2)),
        c(_ny(10, 0, DAY2), 101.0, 101.2, 99.5, 100.6))

    refusal = [m for m in lines if "ignored" in m][-1]
    assert episode.cisd_tf in refusal          # which timeframe confirmed it
    assert f"{episode.purge_time_ny:%H:%M}" in refusal   # and when it purged


@pytest.mark.parametrize("prior_state", [LIQUIDITY_PURGED, CISD_CONFIRMED])
def test_a_setup_with_no_fvg_yet_is_still_replaced_by_a_newer_purge(prior_state):
    """Before a gap exists nothing is fixed, so a newer purge does retake.

    Covers both stages that precede the FVG — waiting for the CISD, and waiting
    for the gap to form. The state is set directly because *which* states are
    protected is the thing under test, not how the engine reached them.
    """
    engine = _engine()
    engine._start_episode("buy", Level("PREV_HOUR_LOW", 100.9, _ny(8, 0, DAY2)),
                          c(_ny(9, 0, DAY2), 101.0, 101.2, 100.4, 101.1))
    episode = engine._episodes["buy"]
    episode.state = prior_state
    engine._last_state["buy"] = prior_state

    engine._start_episode("buy", Level("PDL", 100.0, _ny(9, 0, DAY2)),
                          c(_ny(10, 0, DAY2), 100.9, 101.1, 99.9, 101.0))

    replaced = engine._episodes["buy"]
    assert engine.state("buy") == LIQUIDITY_PURGED
    assert replaced is not episode
    assert (replaced.purge_kind, replaced.purge_price) == ("PDL", 100.0)


# --------------------------------------------------------------------------- #
# The trading calendar
#
# The live loop sleeps over the weekend, so these are defence in depth: a
# hand-run `python run.py scan` on a Saturday, or a host with a wrong clock, must
# not trade a closed market. See trading.sessions for the calendar itself.
# --------------------------------------------------------------------------- #
WEEKEND_DAY1 = 10   # 2026-01-10, a Saturday
WEEKEND_DAY2 = 11   # 2026-01-11, a Sunday


def test_the_weekend_fixture_is_actually_a_weekend():
    assert _ny(9, 0, WEEKEND_DAY1).weekday() == 5
    assert _ny(9, 0, WEEKEND_DAY2).weekday() == 6


def test_a_setup_on_a_sunday_is_refused_for_being_the_weekend():
    """Identical geometry to the canonical scenario, moved onto a Sunday.

    Nothing about the setup changes except the day, so an approval here would
    mean the calendar is not being consulted at all.
    """
    engine = _engine()
    lines = []
    engine.log = lines.append
    warm, feed = build_buy_scenario(day1=WEEKEND_DAY1, day2=WEEKEND_DAY2)
    assert _approved(_run(engine, warm, feed), "buy") == []

    refusals = [m for m in lines if "rejected" in m.lower()]
    assert refusals, f"the refusal was not logged: {lines}"
    assert "weekend" in refusals[-1].lower()


def test_the_same_sunday_setup_is_approved_when_the_calendar_allows_it():
    """Proves the calendar is what refused it, not the geometry.

    Same candles, same Sunday — only ``TRADING_DAYS`` changes. Without this the
    test above would still pass if the scenario had simply stopped producing a
    valid setup.
    """
    engine = _engine(trading_days="sun")
    warm, feed = build_buy_scenario(day1=WEEKEND_DAY1, day2=WEEKEND_DAY2)
    approved = _approved(_run(engine, warm, feed), "buy")
    assert len(approved) == 1
    assert approved[0].status == "APPROVED"


def test_the_canonical_weekday_scenario_is_unaffected():
    """The gate must not refuse a normal Tuesday."""
    engine = _engine()
    warm, feed = build_buy_scenario()
    assert len(_approved(_run(engine, warm, feed), "buy")) == 1


# --------------------------------------------------------------------------- #
# §12 A-F: the rules this fix exists for, one test per rule
#
# A  a CISD inside the still-open purge hour is rejected and never reused
# B  a wick into the gap is a retracement, and it is a state of its own
# C  a close beyond the gap is not an entry
# D  a close inside the gap is the entry, and the entry price sits in the gap
# E  a one-tick gap is not a qualifying FVG unless the floor is configured off
# F  no stage is reachable from a candle that had not closed yet
# --------------------------------------------------------------------------- #
def test_a_a_cisd_inside_the_still_open_purge_hour_is_rejected():
    """§A: the purge *hour* may contain the sweep. It may not contain the CISD.

    The canonical purge hour prints a textbook bullish reclaim of the swept PDL
    at 08:44 — bucket low 99.9 under 100.0, close 100.4 back above it. Under the
    old rule that candle was the CISD. The 1H candle it sits inside had not
    closed, so it confirms nothing: the setup must still be waiting once that
    bucket and the hour are both final.
    """
    engine = _engine()
    warm, feed = build_buy_scenario()
    engine.warm(warm)

    # 09:00-09:04. The purge hour has closed, so its 08:40/08:45 buckets are long
    # final and the in-hour reclaim has had every chance to be picked up.
    for candle in feed[:5]:
        engine.feed(candle)
    assert engine.state("buy") == LIQUIDITY_PURGED

    # 09:05 closes the first 5M bucket to *open* after the purge hour.
    engine.feed(feed[5])
    assert engine.state("buy") == CISD_CONFIRMED
    episode = engine._episodes["buy"]
    assert episode.cisd_time_ny == _ny(9, 0, DAY2)      # not 08:40 and not 08:45
    assert episode.cisd_close_price == pytest.approx(100.8)


def test_a_the_rejected_in_hour_cisd_is_never_reused():
    """§A: "If the CISD happened before the 1H candle closed, IGNORE it."

    Same purge, but from 09:00 on price sits flat above the swept 100.0, so no
    *post-purge* CISD ever forms. The 08:44 reclaim already printed and is a
    perfectly good reclaim of the level — if it were still reachable the setup
    would trade on it. It must instead time out.
    """
    engine = _engine()
    warm, _feed = build_buy_scenario()
    engine.warm(warm)

    start = _ny(9, 0, DAY2)
    for minute in range(60):
        engine.feed(c(start + timedelta(minutes=minute), *_QUIET_BUY))

    assert engine.state("buy") == INVALIDATED


#: The USDCHF geometry from the forensic report: an M1 gap of 0.82344-0.82353,
#: the retracement lows of 0.82349 / 0.82344, and the entry that was taken at
#: 0.82359 — nine ticks *above* the gap's upper boundary.
_USDCHF_LOWER = 0.82344
_USDCHF_UPPER = 0.82353
_USDCHF_ENTRY = 0.82359


def _usdchf_gap() -> FVG:
    return FVG(direction="bullish", lower=_USDCHF_LOWER, upper=_USDCHF_UPPER,
               formation_time_utc=_ny(9, 6, DAY2), formation_time_ny=_ny(9, 6, DAY2))


def test_b_a_wick_into_the_usdchf_gap_is_a_retracement():
    """§B: the reported retracement is real, and it is not an entry.

    The M1 low of 0.82349 sits inside the gap. That is the retracement event,
    and it stands on its own — the candle closed back at 0.82356, outside the
    zone, so it is emphatically not a fill.
    """
    fvg = _usdchf_gap()
    wick = c(_ny(9, 20, DAY2), 0.82355, 0.82358, 0.82349, 0.82356)

    assert entered(wick, fvg) is True
    assert classify(wick, fvg) == "retraced"
    assert closes_inside(wick, fvg) is False


def test_b_the_retracement_is_its_own_state_before_any_entry():
    """§3/§10: RETRACE_CONFIRMED is reachable with no entry taken.

    The canonical scenario reaches it on a bearish candle that wicks into the
    gap. A retracement wick must never be converted straight into an entry at
    some later, unrelated close.
    """
    engine = _engine()
    warm, feed = build_buy_scenario()
    _run(engine, warm, feed[:-1])              # everything but the entry minute

    assert engine.state("buy") == RETRACE_CONFIRMED
    episode = engine._episodes["buy"]
    assert episode.state == RETRACE_CONFIRMED
    assert episode.retrace_time_ny == _ny(9, 33, DAY2)
    assert episode.retrace_extreme == pytest.approx(101.30)


def test_c_a_close_beyond_the_gap_is_not_an_entry():
    """§C: the USDCHF defect — a close above the FVG is not a fill.

    The gap is [101.20, 101.55]. The candle closes bullishly at 101.75, beyond
    the upper boundary, exactly as the reported signal did (entry 0.82359
    against a gap topping out at 0.82353). Nothing may be emitted, and the setup
    stays retraced so a later candle can still fill it properly.
    """
    engine = _engine()
    warm, feed = build_buy_scenario()
    _run(engine, warm, feed[:-1])
    assert engine.state("buy") == RETRACE_CONFIRMED

    out = engine.feed(c(_ny(9, 34, DAY2), 101.60, 101.80, 101.55, 101.75))

    assert out == [], "an entry was taken at a price outside its own FVG"
    assert engine.state("buy") == RETRACE_CONFIRMED


def test_c_an_unrecognised_entry_model_falls_back_to_the_strict_one():
    """A typo must never quietly widen the entry rule to the permissive model."""
    engine = _engine(fvg_entry_model="reactionclose")
    assert engine.fvg_entry_model == "inside_fvg"


def test_d_a_bullish_close_inside_the_gap_is_the_entry():
    """§D: the fill is a price inside the zone — 0.82350, not 0.82359."""
    fvg = _usdchf_gap()
    inside = c(_ny(9, 21, DAY2), 0.82350, 0.82352, 0.82344, 0.82350)
    above = c(_ny(9, 21, DAY2), 0.82352, 0.82362, 0.82350, _USDCHF_ENTRY)

    assert closes_inside(inside, fvg) is True
    assert closes_inside(above, fvg) is False

    # ...and the engine's own entry price obeys the same rule.
    engine = _engine()
    warm, feed = build_buy_scenario()
    sig = _approved(_run(engine, warm, feed), "buy")[0]
    assert sig.fvg_lower == pytest.approx(101.20)
    assert sig.fvg_upper == pytest.approx(101.55)
    assert sig.entry == pytest.approx(101.45)
    assert sig.fvg_lower <= sig.entry <= sig.fvg_upper


#: A gap one tick tall: c2's low clears c0's high by exactly 0.01.
_TINY_FVG_BUY = [
    (101.15, 101.20, 101.10, 101.15),
    (101.05, 101.25, 101.00, 101.22),
    (101.22, 101.25, 101.21, 101.23),
]


def _to_the_fvg_slot(feed, rows):
    """The canonical CISD + quiet prefix, then ``rows`` where the FVG would go."""
    prefix = feed[:29]                          # 09:00-09:28 inclusive
    start = _ny(9, 29, DAY2)
    return prefix + [c(start + timedelta(minutes=i), *row)
                     for i, row in enumerate(rows)]


def test_e_a_one_tick_gap_is_not_a_qualifying_fvg():
    """§E: a gap thinner than the configured fraction of 5M ATR is noise.

    The alternative is worse than it sounds: without a floor the first
    single-tick gap after the CISD becomes the setup, and the "FVG" it produces
    is a spread artefact rather than displacement.
    """
    assert _tiny_rows_gap_depth() == pytest.approx(0.01)

    engine = _engine()
    engine.warm(build_buy_scenario()[0])
    for candle in _to_the_fvg_slot(build_buy_scenario()[1], _TINY_FVG_BUY):
        engine.feed(candle)

    assert engine.state("buy") == CISD_CONFIRMED
    assert engine._episodes["buy"].fvg is None
    assert engine._episodes["buy"].fvg_min_depth > 0.01


def test_e_the_same_gap_qualifies_when_the_floor_is_configured_off():
    """The filter is a configured floor, not a hard-coded rejection."""
    engine = _engine(fvg_min_atr_frac="0")
    engine.warm(build_buy_scenario()[0])
    for candle in _to_the_fvg_slot(build_buy_scenario()[1], _TINY_FVG_BUY):
        engine.feed(candle)

    assert engine.state("buy") == FVG_FOUND
    assert engine._episodes["buy"].fvg.depth() == pytest.approx(0.01)


def _tiny_rows_gap_depth() -> float:
    candles = [c(_ny(9, 29, DAY2) + timedelta(minutes=i), *row)
               for i, row in enumerate(_TINY_FVG_BUY)]
    formed = fvg_at_three(*candles)
    return formed.depth()


def test_f_an_fvg_is_not_visible_before_its_third_candle_closes():
    """§F: the pattern only exists once the candle that completes it closed."""
    engine = _engine()
    warm, feed = build_buy_scenario()
    _run(engine, warm, feed[:31])               # through 09:30 — two thirds in

    assert engine.state("buy") == CISD_CONFIRMED
    assert engine._episodes["buy"].fvg is None

    engine.feed(feed[31])                       # 09:31 closes the pattern
    assert engine.state("buy") == FVG_FOUND
    assert engine._episodes["buy"].fvg.formation_time_ny == _ny(9, 31, DAY2)


def test_f_the_state_after_n_candles_never_depends_on_candle_n_plus_one():
    """§F: no look-ahead, stated as a property of the whole run.

    Replaying the feed and recording the state after every candle must agree,
    candle for candle, with a fresh engine that was only ever handed that
    prefix. Any decision that peeked at a later candle would show up here as a
    divergence.
    """
    warm, feed = build_buy_scenario()

    engine = _engine()
    engine.warm(warm)
    observed = []
    for candle in feed:
        engine.feed(candle)
        observed.append(engine.states())

    for n in range(1, len(feed) + 1):
        fresh = _engine()
        fresh.warm(warm)
        for candle in feed[:n]:
            fresh.feed(candle)
        assert fresh.states() == observed[n - 1], (
            f"the state after {n} candles depended on a later candle")
