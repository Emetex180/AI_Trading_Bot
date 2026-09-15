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
from trading.liquidity import Level
from trading.strategy import (
    CISD_CONFIRMED,
    FVG_FOUND,
    INVALIDATED,
    LIQUIDITY_PURGED,
    NO_SETUP,
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

#: The purge hour's own minutes: 08:10 dips, 08:30 sweeps below the 100.0 PDL,
#: 08:44 reclaims it on the M5 bucket, 08:45 closes the hour.
_PURGE_HOUR_BUY = [
    (8, 10, 101.0, 101.1, 100.4, 100.9),
    (8, 30, 100.1, 100.4, 99.6, 100.05),   # sweep below the PDL
    (8, 44, 100.05, 100.5, 99.9, 100.4),   # M5 08:40 bucket closes bullish > 100
    (8, 45, 100.4, 101.2, 100.3, 101.0),   # finalizes the M5 08:40 bucket
]

_QUIET_BUY = (101.0, 101.2, 100.8, 101.1)          # forms no FVG, invalidates nothing

#: A (c0), B (displacement), C (c2) — C's low sits above A's high, leaving the
#: bullish FVG [A.high, C.low] = [101.20, 101.55]. B's low stays under A's high
#: so the pattern is not detected one candle early.
_FVG_BUY = [
    (101.15, 101.20, 101.10, 101.15),
    (101.05, 101.90, 101.00, 101.85),
    (101.80, 101.85, 101.55, 101.75),
]
_RETRACE_BUY = (101.8, 101.85, 101.45, 101.5)      # into the gap, but bearish
_ENTRY_BUY = (101.5, 101.8, 101.45, 101.75)        # bullish reclaim -> ENTRY


def _minutes(start_hm, count, day=DAY2):
    hour, minute = start_hm
    base = _ny(hour, minute, day)
    return [base + timedelta(minutes=i) for i in range(count)]


def build_buy_scenario(fvg_at=(9, 29), quiet_until=None,
                       day1=DAY1, day2=DAY2):
    """Return ``(warm, feed)`` for a BUY that enters 4 minutes after ``fvg_at``.

    ``fvg_at`` is the (hour, minute) the FVG's first candle opens. ``quiet_until``
    optionally extends the flat run before the FVG, which is how the session
    tests push the entry into a later window. ``day1``/``day2`` are days of
    January 2026, which is how the calendar tests land the same geometry on a
    weekend; the defaults are the Mon/Tue pair every other test uses.
    """
    warm = [c(_ny(h, 0, day1), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY1_BUY]
    warm += [c(_ny(h, 0, day2), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY2_BUY]
    warm += [c(_ny(h, m, day2), o, hi, lo, cl) for h, m, o, hi, lo, cl in _PURGE_HOUR_BUY]

    feed = [c(_ny(9, 0, day2), 101.0, 101.3, 100.4, 101.1)]   # closes H1 08
    # Flat run: the state machine is in CISD_CONFIRMED from 09:00 and finds no
    # FVG here, so the first qualifying gap is the one placed at ``fvg_at``.
    last_quiet = quiet_until or _ny(fvg_at[0], fvg_at[1], day2) - timedelta(minutes=1)
    for ts in _minutes((9, 1), 24 * 60, day=day2):
        if ts > last_quiet:
            break
        o, hi, lo, cl = _QUIET_BUY
        feed.append(c(ts, o, hi, lo, cl))

    for i, (o, hi, lo, cl) in enumerate(_FVG_BUY):
        feed.append(c(_ny(fvg_at[0], fvg_at[1], day2) + timedelta(minutes=i), o, hi, lo, cl))
    for o, hi, lo, cl in (_RETRACE_BUY, _ENTRY_BUY):
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

#: 08:30 sweeps above the 100.0 PDH (high 100.4); 08:40 is the bearish CISD that
#: closes back below it; 08:45 closes the hour at 98.8 so the H1 purge confirms.
_PURGE_HOUR_SELL = [
    (8, 10, 98.9, 99.1, 98.8, 99.0),
    (8, 30, 99.2, 100.4, 99.1, 100.2),
    (8, 40, 100.2, 100.3, 99.3, 99.4),   # bearish CISD back below 100
    (8, 45, 99.4, 99.5, 98.7, 98.8),
]

_QUIET_SELL = (98.9, 99.0, 98.8, 98.95)

#: Bearish FVG [C.high, A.low] = [98.6, 99.0].
_FVG_SELL = [
    (99.05, 99.1, 99.0, 99.0),
    (99.0, 99.15, 98.3, 98.4),
    (98.4, 98.6, 98.2, 98.3),
]
_RETRACE_SELL = (98.5, 98.95, 98.5, 98.85)     # into the gap, but bullish
_ENTRY_SELL = (98.85, 98.9, 98.4, 98.5)        # bearish reclaim -> ENTRY


def build_sell_scenario(fvg_at=(9, 29)):
    warm = [c(_ny(h, 0, DAY1), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY1_SELL]
    warm += [c(_ny(h, 0, DAY2), o, hi, lo, cl) for h, o, hi, lo, cl in _DAY2_SELL]
    warm += [c(_ny(h, m, DAY2), o, hi, lo, cl) for h, m, o, hi, lo, cl in _PURGE_HOUR_SELL]

    feed = [c(_ny(9, 0, DAY2), 98.8, 99.0, 98.6, 98.9)]
    last_quiet = _ny(fvg_at[0], fvg_at[1], DAY2) - timedelta(minutes=1)
    for ts in _minutes((9, 1), 24 * 60):
        if ts > last_quiet:
            break
        o, hi, lo, cl = _QUIET_SELL
        feed.append(c(ts, o, hi, lo, cl))

    for i, (o, hi, lo, cl) in enumerate(_FVG_SELL):
        feed.append(c(_ny(fvg_at[0], fvg_at[1], DAY2) + timedelta(minutes=i), o, hi, lo, cl))
    for o, hi, lo, cl in (_RETRACE_SELL, _ENTRY_SELL):
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
    assert sig.entry == pytest.approx(101.75)
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

    assert sig.entry == pytest.approx(98.5)
    assert sig.sl > sig.entry and sig.tp < sig.entry
    # The liquidity-taking candle is the one that spiked to 100.4.
    assert sig.structure_extreme_price == pytest.approx(100.4)
    assert sig.sl > sig.structure_extreme_price
    assert sig.rr >= 1.5
    # Nearest sell-side pull below the entry: the day-1 low at 90.0.
    assert sig.target_price == pytest.approx(90.0)
    assert sig.tp > sig.target_price
    assert sig.session_primary == "ny_am"


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
def test_state_progresses_through_every_stage():
    engine = _engine()
    warm, feed = build_buy_scenario(fvg_at=(9, 4))
    engine.warm(warm)

    seen = []
    for candle in feed:
        engine.feed(candle)
        state = engine.state("buy")
        if not seen or seen[-1] != state:
            seen.append(state)

    assert seen == [
        CISD_CONFIRMED,
        FVG_FOUND,
        WAITING_FOR_FVG_RETRACE,
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
def test_setup_invalidated_in_a_no_trade_session():
    """A setup that reaches its entry inside NY Lunch must not trade.

    The FVG pattern is placed at 11:29 so the retrace entry would fall at 11:33,
    but NY Lunch (11:30-13:30) is a hard no-trade window and the pending setup is
    dropped the moment it opens.
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
    assert engine.state("buy") == WAITING_FOR_FVG_RETRACE


def test_no_trade_premarket_is_rejected_by_the_session_allowlist():
    """Premarket is CONDITIONAL and must also appear in the allow-list."""
    engine = _engine(valid_entry_sessions="london_open,ny_am,ny_pm")
    warm, feed = build_buy_scenario(fvg_at=(9, 5))
    signals = _run(engine, warm, feed)
    assert _approved(signals, "buy") == []


def test_conditional_premarket_allows_a_strong_target():
    """Same setup, but premarket is allow-listed and the target is VERY_HIGH."""
    engine = _engine(valid_entry_sessions="ny_premarket")
    warm, feed = build_buy_scenario(fvg_at=(9, 5))
    signals = _run(engine, warm, feed)

    buys = _approved(signals, "buy")
    assert buys, "a VERY_HIGH target clears the conditional-session bar"
    assert buys[0].session_primary == "ny_premarket"


def test_enforce_sessions_false_lets_a_blocked_window_through():
    engine = _engine(enforce_sessions="false")
    warm, feed = build_buy_scenario(fvg_at=(9, 5))
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
    assert engine2.state("buy") == WAITING_FOR_FVG_RETRACE


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
    warm, feed = build_buy_scenario(fvg_at=(9, 4))
    a.warm(warm)
    b.warm(warm)

    for candle in feed:
        a.feed(candle)

    assert a.state("buy") == TRADE_CONFIRMED
    assert b.state("buy") == NO_SETUP       # never fed, never advanced


def test_two_assets_can_hold_different_states_simultaneously():
    a = _engine("A")
    b = _engine("B")
    warm, feed = build_buy_scenario(fvg_at=(9, 4))
    a.warm(warm)
    b.warm(warm)

    # Everything except the final (entry) minute.
    for candle in feed[:-1]:
        a.feed(candle)
        b.feed(candle)
    assert a.state("buy") == b.state("buy") == WAITING_FOR_FVG_RETRACE

    # Only A receives the entry minute.
    a.feed(feed[-1])
    assert a.state("buy") == TRADE_CONFIRMED    # A traded
    assert b.state("buy") == WAITING_FOR_FVG_RETRACE   # B untouched, still waiting


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
    assert engine.state("buy") == WAITING_FOR_FVG_RETRACE

    episode = engine._episodes["buy"]
    fvg_before = episode.fvg
    purge_before = (episode.purge_kind, episode.purge_price)

    # The same sell-side level, swept again on a later 1H close.
    engine._start_episode(
        "buy",
        Level(kind=episode.purge_kind, price=episode.purge_price,
              time_ny=_ny(10, 0, DAY2)),
        c(_ny(10, 0, DAY2), 101.0, 101.2, 99.5, 100.6))

    assert engine.state("buy") == WAITING_FOR_FVG_RETRACE
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
