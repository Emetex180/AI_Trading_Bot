"""ICT trading model state machine (M1 driver, shared by live & backtest).

The model
---------
    1H liquidity purge  (previous-day, session, Asian/London, previous-hour,
                         equal-high/low and swing liquidity)
        └─ 5M CISD confirmed *after the purge candle has closed*
            └─ the FIRST qualifying 1M FVG formed after that CISD
                └─ wait for price to trade back into that FVG
                    └─ a candle closes within the FVG → entry
                       SL = the 5M candle that took the liquidity
                       TP = nearest valid liquidity pull in the trade direction
                          → RR + efficiency → deterministic + risk approval → Signal

Each stage is a separate state and a later one cannot be reached without the
one before it (see the state table below). Two consequences are deliberate and
worth stating, because both were bugs:

* **The purge must be complete.** The sweep still happens inside the 1H candle,
  but the CISD search opens only once that candle has *closed*. A 5M reclaim
  printed while the hour was still forming is not a CISD for this premise, and
  it is never revisited later.
* **A retracement is not an entry.** Price wicking into the gap confirms the
  retracement (``RETRACE_CONFIRMED``); the entry needs a closed candle whose
  *close* sits within the gap. A close beyond the far boundary therefore cannot
  produce a signal whose entry price is outside the FVG.

Zero lookahead: every decision is taken on *closed* candles. The 5M series only
ever contains finalized buckets, an FVG is only reported for a fully-formed
three-candle pattern, and the liquidity snapshot for a purge is built from the
H1 candles strictly *before* the purge candle.

One instance tracks one asset, so setup state is isolated per symbol by
construction: US100 can be waiting for a 1M FVG while EURUSD is still waiting
for a 5M CISD and XAUUSD has no setup at all.

State machine
-------------
Two independent episodes per asset, one per direction, each advancing through

    NO_SETUP → LIQUIDITY_PURGED → CISD_CONFIRMED → FVG_FOUND
             → WAITING_FOR_FVG_RETRACE → RETRACE_CONFIRMED
             → TRADE_CONFIRMED
             ↘ INVALIDATED (any point; resets cleanly)

Exactly one transition happens per closed M1 candle. In particular the tick
that *finds* the first FVG only records it (``FVG_FOUND``) — the retracement
check starts on the following candle, so the FVG's own displacement candle can
never be mistaken for the retracement entry.

The engine is transport-free: it takes closed candles in and hands approved
:class:`~trading.signal_engine.Signal` objects out, so live scanning and
backtesting run the identical logic. Pass a ``log`` callable to receive the
strategy's decision log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from config import Settings, get_settings

from . import cisd as cisd_mod
from . import sessions as sess
from . import time_utils as tu
from .asset_manager import Asset
from .fvg import FVG, classify, closes_inside, fvg_on_tail
from .indicators import atr
from .liquidity import (Level, build_level_snapshot, detect_sweeps, grade_at_least,
                        select_tp_target, strongest_purge)
from .risk_manager import (EfficiencyWeights, RiskManager, compute_rr,
                           efficiency_score, risk_reward_points)
from .signal_engine import (Signal, SetupCandidate, build_setup_id,
                            candidate_to_signal)
from .stream import BarsStream

# --------------------------------------------------------------------------- #
# Setup states
#
# One direction's setup advances strictly in this order, and no stage may be
# reached without the one before it:
#
#     NO_SETUP
#        ↓  1H purge candle CLOSED  (a sweep inside a still-forming hour is not
#        │                           a premise yet)
#     LIQUIDITY_PURGED
#        ↓  5M CISD confirmed *after* that close
#     CISD_CONFIRMED
#        ↓  first qualifying 1M FVG
#     FVG_FOUND
#        ↓  price trades back into the gap
#     RETRACE_CONFIRMED
#        ↓  a candle closes within the gap
#     TRADE_CONFIRMED
#        ↘  INVALIDATED (any point; resets cleanly)
# --------------------------------------------------------------------------- #
NO_SETUP = "NO_SETUP"
LIQUIDITY_PURGED = "LIQUIDITY_PURGED"
CISD_CONFIRMED = "CISD_CONFIRMED"
FVG_FOUND = "FVG_FOUND"
WAITING_FOR_FVG_RETRACE = "WAITING_FOR_FVG_RETRACE"
#: The gap has been traded into. Distinct from TRADE_CONFIRMED on purpose: a
#: wick into the FVG is a *retracement*, not an entry — the entry needs a close
#: back inside the zone, and this state is genuinely reachable on its own.
RETRACE_CONFIRMED = "RETRACE_CONFIRMED"
TRADE_CONFIRMED = "TRADE_CONFIRMED"
INVALIDATED = "INVALIDATED"

# --------------------------------------------------------------------------- #
# Entry models (``FVG_ENTRY_MODEL``)
# --------------------------------------------------------------------------- #
#: Default: the entry candle must close *within* the gap, so the entry price is
#: always a price inside the FVG. A close beyond the far boundary is refused.
ENTRY_MODEL_INSIDE_FVG = "inside_fvg"
#: Opt-in legacy rule: a close back beyond the gap that reclaims it counts,
#: allowing an entry price outside the FVG. Never selected automatically.
ENTRY_MODEL_REACTION_CLOSE = "reaction_close"
ENTRY_MODELS = (ENTRY_MODEL_INSIDE_FVG, ENTRY_MODEL_REACTION_CLOSE)

#: States in which the setup's FVG exists. That is exactly what §3 fixes in
#: place: "the first qualifying FVG is not replaced unless invalidated", so a
#: newer 1H purge must not supersede one of these. Deliberately excludes
#: CISD_CONFIRMED — no gap has formed yet, so a fresh purge there is a better
#: premise rather than a setup being destroyed.
#:
#: RETRACE_CONFIRMED belongs here for the same reason FVG_FOUND does: the gap
#: has been traded into, so the setup is mid-sequence and only an invalidation
#: rule may clear it.
_FVG_ESTABLISHED = frozenset({FVG_FOUND, WAITING_FOR_FVG_RETRACE, RETRACE_CONFIRMED})

# --------------------------------------------------------------------------- #
# Defaults for the few knobs that are not configuration settings
# --------------------------------------------------------------------------- #
#: H1 bars required before a purge is even considered (lets PDH/PDL and the
#: session lookbacks resolve from real history rather than a partial day).
MIN_HISTORY_H1_DEFAULT = 24
#: Equal-high/low tolerance as a multiple of H1 ATR.
EQ_TOLERANCE_ATR_MULT = 0.2
#: Cap on the in-process set of already-emitted setup ids (see _mark_triggered).
TRIGGERED_ID_CAP = 5000


def _direction_label(direction: str) -> str:
    return "bullish" if direction == "buy" else "bearish"


def _side_label(direction: str) -> str:
    return "sell-side" if direction == "buy" else "buy-side"


def _as_list(value) -> list[str]:
    """Normalise a settings value into a list of lowercase names.

    ``ASSET_<NAME>_VALID_ENTRY_SESSIONS`` arrives from the environment as the
    comma-separated string the global setting uses, so it is split here rather
    than iterated character by character.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip().lower() for p in value.split(",") if p.strip()]
    return [str(p).strip().lower() for p in value]


# --------------------------------------------------------------------------- #
# Internal state record
# --------------------------------------------------------------------------- #
@dataclass
class _Episode:
    """One direction's live setup, from the purge to the entry (or invalidation)."""

    direction: str                          # "buy" | "sell"
    state: str = NO_SETUP

    # --- step 1: 1H liquidity purge ------------------------------------- #
    purge_kind: str = ""
    purge_price: float = 0.0
    purge_grade: str = ""
    purge_time_utc: datetime | None = None
    purge_time_ny: datetime | None = None
    purge_close_utc: datetime | None = None
    #: Deepest excursion of the purge candle beyond the swept level.
    purge_sweep_price: float = 0.0
    purge_h1_close: float = 0.0

    # --- step 2: 5M CISD ------------------------------------------------ #
    cisd_tf: str = "M5"
    cisd_time_utc: datetime | None = None
    cisd_time_ny: datetime | None = None
    cisd_close_utc: datetime | None = None
    cisd_close_price: float = 0.0
    sl_anchor_price: float = 0.0            # the 5M liquidity-taking candle
    sl_anchor_time_ny: datetime | None = None
    cisd_waited: int = 0                    # 5M bars since the purge closed

    # --- step 3: 1M FVG -------------------------------------------------- #
    fvg: FVG | None = None
    fvg_min_depth: float = 0.0              # ATR-derived floor in force
    fvg_waited: int = 0                     # M1 bars since the CISD confirmed

    # --- step 4: retracement -------------------------------------------- #
    retrace_waited: int = 0                 # M1 bars since the FVG was found
    retrace_time_ny: datetime | None = None
    retrace_extreme: float = 0.0            # the wick that reached into the gap
    signal_id: str = ""


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class ICTStrategy:
    """Per-asset ICT engine implementing the purge → CISD → FVG → retrace model."""

    def __init__(self, asset: Asset, *, settings: Settings | None = None,
                 server_offset_hours: float | None = None,
                 log: Callable[[str], None] | None = None):
        cfg = settings or get_settings()
        self.asset = asset
        self.stream = BarsStream(server_offset_hours=server_offset_hours)
        self.log = log

        def asset_setting(key: str, fallback):
            return asset.settings_value(key, fallback, cfg)

        # --- sessions / risk -------------------------------------------- #
        self.valid_sessions: list[str] = _as_list(asset_setting(
            "valid_entry_sessions", cfg.valid_entry_sessions))
        self.enforce_sessions: bool = bool(asset_setting(
            "enforce_sessions", cfg.enforce_sessions))
        # The calendar half of "may we trade now?". Resolved to weekday indices
        # here, once, because the per-candle entry gate consults it and
        # re-parsing a name list on every candle would be pure waste.
        self.trading_days: frozenset[int] = sess.parse_trading_days(
            asset_setting("trading_days", getattr(cfg, "trading_days", None)))
        self.min_rr: float = float(asset_setting("min_rr", cfg.min_rr))
        risk_percent: float = float(asset_setting("risk_percent", cfg.risk_percent))
        self.risk = RiskManager(min_rr=self.min_rr, risk_percent=risk_percent)

        # --- timeframes -------------------------------------------------- #
        self.cisd_timeframe: str = str(asset_setting(
            "cisd_timeframe", getattr(cfg, "cisd_timeframe", cisd_mod.DEFAULT_CISD_TIMEFRAME)))
        self.cisd_threshold_hour: int = int(asset_setting(
            "cisd_threshold_hour_ny", cfg.cisd_threshold_hour_ny))

        # --- liquidity / structure --------------------------------------- #
        self.swing_lookback: int = int(asset_setting("swing_lookback", cfg.swing_lookback))
        self.min_history_h1: int = int(asset_setting("min_history_h1", MIN_HISTORY_H1_DEFAULT))
        self.session_lookback_days: int = int(asset_setting(
            "session_lookback_days", cfg.session_lookback_days))
        self.min_tp_grade: str = str(asset_setting(
            "min_tp_liquidity_grade", cfg.min_tp_liquidity_grade)).upper()
        self.conditional_min_grade: str = str(asset_setting(
            "conditional_min_liquidity_grade", cfg.conditional_min_liquidity_grade)).upper()

        # --- stop loss / take profit offsets ------------------------------ #
        # Tick-based floors keep the offsets symbol-correct: 2 ticks is 0.02 on
        # a 2-digit index and 0.00002 on a 5-digit FX pair, with no per-asset
        # "number of points" table.
        self.sl_buffer_atr: float = float(asset_setting("sl_buffer_atr", cfg.sl_buffer_atr))
        self.sl_min_offset_ticks: float = float(asset_setting(
            "sl_min_offset_ticks", cfg.sl_min_offset_ticks))
        self.tp_offset_atr: float = float(asset_setting(
            "tp_liquidity_offset_atr", cfg.tp_liquidity_offset_atr))
        self.tp_offset_ticks: float = float(asset_setting(
            "tp_liquidity_offset_ticks", cfg.tp_liquidity_offset_ticks))

        # --- setup timeouts / validity ------------------------------------ #
        self.max_cisd_candles: int = int(asset_setting(
            "max_cisd_candles", cfg.max_cisd_candles))
        self.fvg_wait_m1: int = int(asset_setting("fvg_wait_m1", cfg.fvg_wait_m1))
        self.retrace_wait_m1: int = int(asset_setting(
            "retrace_wait_m1", cfg.retrace_wait_m1))
        self.fvg_min_atr_frac: float = float(asset_setting(
            "fvg_min_atr_frac", cfg.fvg_min_atr_frac))
        # Which price the entry may be taken at. An unrecognised value falls back
        # to the strict model rather than to the permissive one: a typo in
        # `.env` must never quietly widen the entry rule.
        model = str(asset_setting(
            "fvg_entry_model", getattr(cfg, "fvg_entry_model", ENTRY_MODEL_INSIDE_FVG))
        ).strip().lower()
        self.fvg_entry_model: str = (model if model in ENTRY_MODELS
                                     else ENTRY_MODEL_INSIDE_FVG)
        self.max_signals_per_session: int = int(asset_setting(
            "max_signals_per_session", cfg.max_signals_per_session))
        self.invalidate_on_no_trade_session: bool = bool(asset_setting(
            "invalidate_on_no_trade_session", cfg.invalidate_on_no_trade_session))

        # --- efficiency score -------------------------------------------- #
        self.rr_target: float = float(asset_setting(
            "efficiency_rr_target", cfg.efficiency_rr_target))
        self.atr_target_multiple: float = float(asset_setting(
            "efficiency_atr_target_multiple", cfg.efficiency_atr_target_multiple))
        self.weights = EfficiencyWeights(
            rr=float(asset_setting("efficiency_weight_rr", cfg.efficiency_weight_rr)),
            liquidity=float(asset_setting("efficiency_weight_liquidity",
                                          cfg.efficiency_weight_liquidity)),
            distance=float(asset_setting("efficiency_weight_distance",
                                         cfg.efficiency_weight_distance)),
        )

        # --- per-direction episodes (isolated per asset instance) --------- #
        self._episodes: dict[str, _Episode] = {}
        self._last_state: dict[str, str] = {"buy": NO_SETUP, "sell": NO_SETUP}
        #: setup ids already emitted — a second emission for the same setup is
        #: refused here as well as by the scanner/DB fingerprint guards.
        self._triggered: set[str] = set()
        #: (session key, NY date) -> emitted count, for the per-session cap.
        self._session_counts: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def state(self, direction: str) -> str:
        """Current setup state for a direction (last known when idle)."""
        episode = self._episodes.get(direction)
        return episode.state if episode is not None else self._last_state.get(direction, NO_SETUP)

    def states(self) -> dict[str, str]:
        """``{"buy": state, "sell": state}`` — safe to poll from a dashboard."""
        return {d: self.state(d) for d in ("buy", "sell")}

    @property
    def warm_history_h1(self) -> int:
        return len(self.stream.h1())

    # ------------------------------------------------------------------ #
    # Warm-up
    # ------------------------------------------------------------------ #
    def warm(self, candles: list) -> None:
        """Feed historical closed M1 candles without emitting signals."""
        for c in candles:
            self.stream.add(c)

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #
    def feed(self, candle) -> list[Signal]:
        """Process one newly *closed* M1 candle; return approved signals."""
        update = self.stream.add(candle)
        signals: list[Signal] = []

        # A closed H1 candle is where a liquidity purge is detected.
        if update.h1 is not None:
            self._on_h1_closed(update.h1)

        # Every closed minute may confirm a CISD, form the first FVG, or take
        # price back into it — so advance the state machines unconditionally.
        signals += self._advance(candle)

        return _dedupe_signals(signals)

    # ------------------------------------------------------------------ #
    # Step 1 — 1H liquidity purge
    # ------------------------------------------------------------------ #
    def _on_h1_closed(self, h1_candle) -> None:
        h1_list = self.stream.h1()
        history = h1_list[:-1]                     # exclude the new H1 candle
        if len(h1_list) < self.min_history_h1:
            return

        tol = self._eq_tolerance()
        levels = build_level_snapshot(
            history,
            as_of_date=h1_candle.t_ny.strftime("%Y-%m-%d"),
            as_of_ny=h1_candle.t_ny,
            lookback_swings=self.swing_lookback,
            swing_tolerance=tol,
            session_lookback_days=self.session_lookback_days,
        )
        events = detect_sweeps(h1_candle, levels)

        bullish = strongest_purge(events, "bullish")
        bearish = strongest_purge(events, "bearish")
        if bullish and bearish:
            # Indecision candle: swept liquidity on both sides — not a premise.
            self._log("1H candle swept both sides — ignored")
            return

        if bullish:
            self._start_episode("buy", bullish.level, h1_candle,
                                sweep_price=self._sweep_extreme(bullish))
        elif bearish:
            self._start_episode("sell", bearish.level, h1_candle,
                                sweep_price=self._sweep_extreme(bearish))

    @staticmethod
    def _sweep_extreme(event) -> float:
        """Where price actually went, beyond the swept level.

        ``detect_sweeps`` records the overshoot as a distance from the level, so
        the excursion is that distance measured away from it in the sweep's
        direction — the same number the stop-loss anchor is later built from.
        """
        if event.direction == "bullish":
            return event.level.price - event.distance
        return event.level.price + event.distance

    def _start_episode(self, direction: str, level: Level, h1_candle,
                       sweep_price: float | None = None) -> None:
        current = self._episodes.get(direction)
        if current is not None and current.state in _FVG_ESTABLISHED:
            # The setup already has its qualifying FVG, so that gap is the one
            # that stands: the spec fixes it until it is *invalidated*, and
            # replacement by a newer purge is not one of the invalidation rules.
            # Without this guard a 1H close would silently discard a confirmed
            # setup that was still waiting for its retrace.
            self._log(f"1H {_side_label(direction)} liquidity purged "
                      f"({level.label} @ {level.price}) — ignored: the "
                      f"{current.cisd_tf} setup purged at "
                      f"{current.purge_time_ny:%H:%M} NY already has its FVG "
                      "and has not been invalidated")
            return
        # The purge candle is only ever offered to this method once it has
        # *closed* (`_on_h1_closed` runs off the finalized H1 bucket), so the
        # sweep inside it is complete and the premise is confirmed on entry.
        if sweep_price is None:
            sweep_price = h1_candle.low if direction == "buy" else h1_candle.high
        episode = _Episode(
            direction=direction,
            state=LIQUIDITY_PURGED,
            purge_kind=level.kind,
            purge_price=level.price,
            purge_grade=level.grade,
            purge_time_utc=h1_candle.t_utc,
            purge_time_ny=h1_candle.t_ny,
            purge_close_utc=h1_candle.t_utc + timedelta(hours=1),
            purge_sweep_price=float(sweep_price),
            purge_h1_close=h1_candle.close,
            cisd_tf=cisd_mod.resolve_timeframe(h1_candle.t_ny, self.cisd_timeframe,
                                               self.cisd_threshold_hour),
        )
        self._episodes[direction] = episode
        self._last_state[direction] = LIQUIDITY_PURGED
        self._log_purge_block(episode, h1_candle)
        self._log(f"1H {_side_label(direction)} liquidity purged "
                  f"({level.label} @ {level.price}) — the purge candle has "
                  f"closed ({tu.utc_to_ny(episode.purge_close_utc):%H:%M} NY), "
                  f"so the {episode.cisd_tf} CISD search now begins")

    # ------------------------------------------------------------------ #
    # Per-minute advance — one transition per direction per candle
    # ------------------------------------------------------------------ #
    def _advance(self, candle) -> list[Signal]:
        signals: list[Signal] = []
        minute = tu.minute_of_day(candle.t_ny)

        for direction in ("buy", "sell"):
            episode = self._episodes.get(direction)
            if episode is None:
                continue

            # A pending setup may not survive dead time. The Asian range is a
            # range-building block, and the hours outside every window are a
            # closed market; both are non-entry time, so a setup that reaches
            # either is dropped rather than carried forward.
            #
            # Treating the closed hours the same as a defined NO-trade window
            # preserves the rule's original effect under the five-session table.
            # The old table had an explicit NY Lunch (11:30-13:30) here; its
            # successor is the 11:00-13:00 gap, and without this the very same
            # setup would survive the gap and could enter on an FVG that is by
            # then ~90 minutes stale. The rule only ever *cancels* setups, so
            # widening it cannot manufacture an entry.
            if self.enforce_sessions and self.invalidate_on_no_trade_session:
                mode = sess.session_trade_mode(minute)
                if mode in (sess.TRADE_NO, sess.TRADE_CLOSED):
                    self._invalidate(direction,
                                     "no-trade session" if mode == sess.TRADE_NO
                                     else "outside session")
                    continue

            state = episode.state
            if state == LIQUIDITY_PURGED:
                self._step_cisd(direction, episode)
            elif state == CISD_CONFIRMED:
                self._step_fvg(direction, episode)
            elif state == FVG_FOUND:
                episode.state = WAITING_FOR_FVG_RETRACE
                self._last_state[direction] = WAITING_FOR_FVG_RETRACE
                self._log("Waiting for FVG retracement")
            elif state in (WAITING_FOR_FVG_RETRACE, RETRACE_CONFIRMED):
                # Both stages run the same per-candle check; which one the setup
                # is in is what decides whether an entry may be taken at all.
                signals += self._step_retrace(direction, episode, candle)
        return signals

    # ------------------------------------------------------------------ #
    # Step 2 — 5M CISD
    # ------------------------------------------------------------------ #
    def _step_cisd(self, direction: str, episode: _Episode) -> None:
        series = self.stream.tf(episode.cisd_tf)

        # The CISD search opens when the purge candle *closes*, not when it
        # opens. The sweep itself stays inside the 1H candle — that is where the
        # liquidity was taken — but a 5M reclaim that printed while the hour was
        # still forming happened before the premise existed, so it is not a CISD
        # for this setup and is never revisited later.
        #
        # ``purge_close_utc`` is the open of the first 5M bucket after the purge
        # hour, so a bucket that merely straddles the hour boundary is excluded
        # too: the confirmation must occur entirely after the hour is complete.
        # `after_time_utc` is inclusive, which is what puts that first
        # post-purge bucket in scope and every earlier one out of it.
        after_purge = [c for c in series if c.t_utc >= episode.purge_close_utc]
        episode.cisd_waited = len(after_purge)
        if episode.cisd_waited > self.max_cisd_candles:
            self._invalidate(direction, f"no {episode.cisd_tf} CISD within "
                                        f"{self.max_cisd_candles} candles of the "
                                        "completed purge")
            return

        confirm_candle = cisd_mod.confirm(
            series, episode.purge_price, direction,
            after_time_utc=episode.purge_close_utc,
            params=cisd_mod.CISDParams(max_candles=self.max_cisd_candles),
        )
        if confirm_candle is None:
            return

        period_min = 5 if episode.cisd_tf == "M5" else 15
        episode.cisd_time_utc = confirm_candle.t_utc
        episode.cisd_time_ny = confirm_candle.t_ny
        episode.cisd_close_utc = confirm_candle.t_utc + timedelta(minutes=period_min)
        episode.cisd_close_price = confirm_candle.close
        self._set_sl_anchor(episode, confirm_candle)
        episode.state = CISD_CONFIRMED
        self._last_state[direction] = CISD_CONFIRMED
        self._log_cisd_block(episode, confirm_candle)
        self._log(f"{_direction_label(direction).title()} CISD confirmed on "
                  f"{episode.cisd_tf} — SL anchored to the {episode.cisd_tf} "
                  f"liquidity-taking candle @ {episode.sl_anchor_price}")

    def _set_sl_anchor(self, episode: _Episode, confirm_candle) -> None:
        """Anchor the stop to the 5M candle that *took* the liquidity.

        That is the bar with the deepest excursion beyond the swept level
        between the purge hour and the CISD candle inclusive — normally the CISD
        candle itself, but a deeper earlier wick inside the purge hour is the
        candle that actually took the liquidity, so it wins. All candles
        considered are already closed when the signal is built.
        """
        series = self.stream.tf(episode.cisd_tf)
        window = [c for c in series
                  if episode.purge_time_utc <= c.t_utc <= confirm_candle.t_utc]
        anchor = confirm_candle
        if episode.direction == "buy":
            below = [c for c in window if c.low < episode.purge_price]
            if below:
                anchor = min(below, key=lambda c: c.low)
        else:
            above = [c for c in window if c.high > episode.purge_price]
            if above:
                anchor = max(above, key=lambda c: c.high)

        episode.sl_anchor_price = anchor.low if episode.direction == "buy" else anchor.high
        episode.sl_anchor_time_ny = anchor.t_ny

    # ------------------------------------------------------------------ #
    # Step 3 — first qualifying 1M FVG
    # ------------------------------------------------------------------ #
    def _step_fvg(self, direction: str, episode: _Episode) -> None:
        episode.fvg_waited += 1
        if episode.fvg_waited > self.fvg_wait_m1:
            self._invalidate(direction, f"no 1M FVG within {self.fvg_wait_m1} minutes")
            return

        # FVG detection is expressed in gap direction ("bullish"/"bearish"),
        # not trade direction ("buy"/"sell").
        gap_dir = _direction_label(direction)
        formed = fvg_on_tail(self.stream.m1, direction=gap_dir,
                             after_time_utc=episode.cisd_close_utc)
        if formed is None:
            return

        # A qualifying FVG must clear the minimum depth, expressed as a fraction
        # of 5M ATR. Without it a one-tick gap is a "gap" and the first one the
        # scanner happens to see becomes the setup. A gap that is too thin is
        # not the first qualifying FVG, so keep looking.
        min_depth = self._fvg_min_depth()
        episode.fvg_min_depth = min_depth
        if min_depth > 0 and formed.depth() < min_depth:
            self._log_fvg_block(formed, min_depth, valid=False)
            return

        episode.fvg = formed
        episode.state = FVG_FOUND
        self._last_state[direction] = FVG_FOUND
        self._log_fvg_block(formed, min_depth, valid=True)

    def _fvg_min_depth(self) -> float:
        """The minimum FVG height in price terms; 0.0 disables the filter."""
        if self.fvg_min_atr_frac <= 0:
            return 0.0
        return self._atr_m5() * self.fvg_min_atr_frac

    # ------------------------------------------------------------------ #
    # Step 4 — trade back into the FVG
    # ------------------------------------------------------------------ #
    def _step_retrace(self, direction: str, episode: _Episode, candle) -> list[Signal]:
        episode.retrace_waited += 1
        if episode.retrace_waited > self.retrace_wait_m1:
            self._invalidate(direction, "FVG not traded into in time")
            return []

        # The premise is broken once price closes beyond the level that took
        # the liquidity — the stop level has already failed.
        if direction == "buy" and candle.close < episode.sl_anchor_price:
            self._invalidate(direction, "price closed below the liquidity-taking low")
            return []
        if direction == "sell" and candle.close > episode.sl_anchor_price:
            self._invalidate(direction, "price closed above the liquidity-taking high")
            return []

        state = classify(candle, episode.fvg)
        if state == "invalidated":
            self._invalidate(direction, "FVG invalidated")
            return []

        # ---- stage 4: RETRACEMENT ------------------------------------- #
        # Recorded once, on the first candle whose range reaches the gap. This
        # is a state in its own right — the setup now has its retracement but
        # has *not* been entered — so a wick into the zone can never be
        # converted straight into a fill at some unrelated later close.
        if state == "retraced" and episode.state != RETRACE_CONFIRMED:
            episode.retrace_time_ny = candle.t_ny
            episode.retrace_extreme = (candle.low if direction == "buy"
                                       else candle.high)
            episode.state = RETRACE_CONFIRMED
            self._last_state[direction] = RETRACE_CONFIRMED
            self._log_retrace_block(episode, candle, entered=True)

        # ---- stage 5: ENTRY TRIGGER ----------------------------------- #
        # Unreachable without the retracement above: NO_RETRACE → NO_ENTRY.
        if episode.state != RETRACE_CONFIRMED:
            return []
        if not self._entry_trigger(candle, episode):
            self._log_overshot_entry(candle, episode)
            return []

        signal = self._build_entry_signal(candle, episode)
        if signal is not None:
            self._last_state[direction] = TRADE_CONFIRMED
            self._episodes.pop(direction, None)
            return [signal]
        return []

    def _entry_trigger(self, candle, episode: _Episode) -> bool:
        """May this closed candle be filled? The retracement must already exist.

        Two requirements, both necessary:

        1. the candle closes in the trade's direction (a bullish close for a
           buy), so the entry is a reaction and not a continuation into the gap;
        2. under the default ``inside_fvg`` model, that close sits *within*
           ``[fvg.lower, fvg.upper]`` — the fill is a price inside the gap.

        (2) is what makes an entry outside the gap impossible: a close beyond
        the far boundary is refused here, not merely discouraged elsewhere. Only
        the explicitly opted-in ``reaction_close`` model relaxes it, and that is
        never selected by default.
        """
        fvg = episode.fvg
        bullish = candle.close > candle.open
        if episode.direction == "buy":
            if not bullish:
                return False
        elif candle.close >= candle.open:
            return False

        if self.fvg_entry_model == ENTRY_MODEL_REACTION_CLOSE:
            # Opt-in legacy rule: a close that reclaims the swept level counts
            # even when it lands outside the gap. Permits an entry price beyond
            # the FVG — the operator asked for it explicitly.
            if episode.direction == "buy":
                return candle.close > fvg.midpoint and candle.close > fvg.lower
            return candle.close < fvg.midpoint and candle.close < fvg.upper

        return closes_inside(candle, fvg)

    def _log_overshot_entry(self, candle, episode: _Episode) -> None:
        """Say so when a candle closes clean *through* the gap and is refused.

        This is the shape of the reported USDCHF defect — entry 0.82359 against
        a gap topping out at 0.82353 — so it is worth a line rather than a
        silence: the operator needs to see that the setup reacted in the right
        direction and was still not filled.

        Deliberately narrow. A close that is short of the gap is an ordinary
        waiting minute, and logging those would bury this. Under the opted-in
        ``reaction_close`` model such a close *is* the entry, so nothing is
        logged here either.
        """
        if self.fvg_entry_model == ENTRY_MODEL_REACTION_CLOSE:
            return
        fvg = episode.fvg
        overshot = (candle.close > fvg.upper if episode.direction == "buy"
                    else candle.close < fvg.lower)
        if not overshot:
            return
        self._log_entry_block(episode, candle, candle.close, entry_inside=False)
        self._reject(f"entry {self._fmt(candle.close)} is outside the FVG "
                     f"[{self._fmt(fvg.lower)}, {self._fmt(fvg.upper)}] — the "
                     "retracement into the gap is required first")

    # ------------------------------------------------------------------ #
    # Entry construction
    # ------------------------------------------------------------------ #
    def _build_entry_signal(self, candle, episode: _Episode) -> Signal | None:
        direction = episode.direction
        fvg = episode.fvg
        # The fill is the confirming close, and under the default model the
        # trigger has already proven that close sits inside the gap. The guard
        # below re-states it rather than trusting the call path: an entry price
        # outside its own FVG is the exact defect this rule exists to prevent,
        # so it is refused here even if a future edit loosens the trigger.
        entry = candle.close
        if self.fvg_entry_model == ENTRY_MODEL_INSIDE_FVG and not fvg.contains(entry):
            self._reject(f"entry {entry} is outside the FVG "
                         f"[{fvg.lower}, {fvg.upper}]")
            return None

        atr_m5 = self._atr_m5()
        tick = self._tick_size()

        # ---- stop loss: just beyond the 5M liquidity-taking candle -------- #
        sl_offset = max(self.sl_buffer_atr * atr_m5, self.sl_min_offset_ticks * tick)
        if direction == "buy":
            sl = episode.sl_anchor_price - sl_offset
        else:
            sl = episode.sl_anchor_price + sl_offset

        # ---- take profit: nearest valid liquidity pull -------------------- #
        tp_offset = max(self.tp_offset_atr * atr_m5, self.tp_offset_ticks * tick)
        pick = select_tp_target(entry, direction, self._target_levels(candle),
                                offset=tp_offset, min_grade=self.min_tp_grade)
        if pick is None:
            self._reject("no valid liquidity target")
            return None
        target, tp = pick

        if sl <= 0 or tp <= 0:
            self._reject("invalid SL/TP geometry")
            return None

        # ---- geometry, RR and efficiency, all before any notification ----- #
        risk_points, reward_points = risk_reward_points(entry, sl, tp, direction)
        rr = compute_rr(entry, sl, tp, direction)
        if rr < self.min_rr:
            self._reject(f"RR below minimum ({rr:.2f} < {self.min_rr:.2f})")
            return None

        score = efficiency_score(
            rr=rr, rr_target=self.rr_target,
            liquidity_grade=target.grade,
            reward_points=reward_points, atr=atr_m5,
            atr_target_multiple=self.atr_target_multiple,
            weights=self.weights,
        )

        # ---- session gate -------------------------------------------------- #
        ny = candle.t_ny
        minute = tu.minute_of_day(ny)
        if self.enforce_sessions:
            if not sess.is_trading_day(ny, self.trading_days):
                self._reject(sess.WEEKEND_REASON)
                return None
            conditional_ok = grade_at_least(target.grade, self.conditional_min_grade)
            allowed, reason = sess.entry_permission(
                minute,
                conditional_ok=conditional_ok,
                allowed_sessions=self.valid_sessions or None,
            )
            if not allowed:
                self._reject(reason)
                return None

        session_key = sess.primary_session(minute)
        session_key = session_key.key if session_key else ""
        if self._session_capped(session_key, ny):
            self._reject(f"maximum signals reached for {session_key}")
            return None

        # ---- assemble ------------------------------------------------------ #
        setup_id = build_setup_id(
            self.asset.name, direction,
            episode.purge_time_utc, episode.cisd_time_utc,
            episode.fvg.formation_time_utc,
        )
        if setup_id in self._triggered:
            self._reject("duplicate setup")
            return None

        self._log(f"TP liquidity identified: {target.label} ({target.grade})")
        digits = int(getattr(self.asset, "digits", 0) or 0)
        p = (lambda v: f"{v:.{digits}f}") if digits > 0 else (lambda v: f"{v:g}")
        self._log(f"Entry/SL/TP calculated — Entry {p(entry)} "
                  f"SL {p(sl)} TP {p(tp)}")
        self._log(f"RR = {rr:.2f}  |  Efficiency = {score:.0f}%")

        candidate = SetupCandidate(
            asset=self.asset.name,
            direction=direction,
            entry_price=entry,
            entry_time_utc=candle.t_utc,
            entry_time_ny=ny,
            sl=sl,
            tp=tp,
            session_keys=sess.active_session_keys(minute),
            session_primary=session_key,
            silver_bullet=sess.active_silver_bullet_key(minute),
            macro=sess.active_macro_window_key(minute),
            liquidity_type=episode.purge_kind,
            liquidity_price=episode.purge_price,
            purge_grade=episode.purge_grade,
            purge_time_ny=episode.purge_time_ny,
            cisd_tf=episode.cisd_tf,
            cisd_confirm_time_ny=episode.cisd_time_ny,
            fvg_direction=episode.fvg.direction,
            fvg_lower=episode.fvg.lower,
            fvg_upper=episode.fvg.upper,
            fvg_formation_time_ny=episode.fvg.formation_time_ny,
            structure_extreme_price=episode.sl_anchor_price,
            structure_time_ny=episode.sl_anchor_time_ny,
            target_kind=target.kind,
            target_price=target.price,
            target_grade=target.grade,
            risk_points=risk_points,
            reward_points=reward_points,
            efficiency_score=score,
            setup_id=setup_id,
            state=TRADE_CONFIRMED,
            digits=int(getattr(self.asset, "digits", 0) or 0),
        )
        signal = candidate_to_signal(candidate, self.valid_sessions, self.risk,
                                     allow_session_gate=self.enforce_sessions)
        if signal.status != "APPROVED":
            self._log(f"Setup rejected by validation: {signal.reason}")
            return None

        self._mark_triggered(setup_id, session_key, ny)
        self._log_entry_block(episode, candle, entry, entry_inside=fvg.contains(entry))
        self._log("TRADE CONFIRMED")
        return signal

    # ------------------------------------------------------------------ #
    # Session bookkeeping
    # ------------------------------------------------------------------ #
    def _session_capped(self, session_key: str, ny: datetime) -> bool:
        if self.max_signals_per_session <= 0 or not session_key:
            return False
        key = (session_key, ny.strftime("%Y-%m-%d"))
        return self._session_counts.get(key, 0) >= self.max_signals_per_session

    def _mark_triggered(self, setup_id: str, session_key: str, ny: datetime) -> None:
        if len(self._triggered) >= TRIGGERED_ID_CAP:
            # Bounded memory on a session that runs for weeks. The scanner's
            # RecentSignals window and the DB's unique fingerprint still guard
            # against a repeat, so clearing here is not a correctness risk.
            self._triggered.clear()
        self._triggered.add(setup_id)
        if session_key:
            key = (session_key, ny.strftime("%Y-%m-%d"))
            self._session_counts[key] = self._session_counts.get(key, 0) + 1

    # ------------------------------------------------------------------ #
    # Invalidation
    # ------------------------------------------------------------------ #
    def _invalidate(self, direction: str, reason: str) -> None:
        self._episodes.pop(direction, None)
        self._last_state[direction] = INVALIDATED
        self._log(f"Setup invalidated: {reason}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        if self.log is None:
            return
        try:
            self.log(f"[{self.asset.name}] {message}")
        except Exception:  # a broken log sink must never stop the engine
            pass

    def _reject(self, reason: str) -> None:
        self._log(f"Setup rejected: {reason}")

    # ------------------------------------------------------------------ #
    # Decision blocks
    #
    # One block per confirmed stage, in the model's own vocabulary, so a reader
    # (or a Telegram digest of the event log) can follow a setup from the sweep
    # to the fill without reconstructing it from the candle data. Every line is
    # prefixed with its stage, which keeps the blocks greppable in a long log.
    # ------------------------------------------------------------------ #
    def _fmt(self, value: float) -> str:
        """Format a price to the symbol's own precision."""
        digits = int(getattr(self.asset, "digits", 0) or 0)
        return f"{value:.{digits}f}" if digits > 0 else f"{value:g}"

    @staticmethod
    def _yn(flag: bool) -> str:
        return "YES" if flag else "NO"

    def _log_purge_block(self, episode: _Episode, h1_candle) -> None:
        self._log("PURGE:\n"
                  f"  1H time: {h1_candle.t_ny:%Y-%m-%d %H:%M} NY\n"
                  f"  Liquidity level: {episode.purge_kind} "
                  f"({episode.purge_grade}) @ {self._fmt(episode.purge_price)}\n"
                  f"  Sweep price: {self._fmt(episode.purge_sweep_price)}\n"
                  f"  1H close: {self._fmt(episode.purge_h1_close)}\n"
                  f"  Purge confirmed: {self._yn(True)} (the 1H candle has closed)")

    def _log_cisd_block(self, episode: _Episode, confirm_candle) -> None:
        # The confirmation is only reachable through `_step_cisd`, which filters
        # the series to candles opening at/after the purge close — so "after the
        # completed purge" is structurally true here, not an assumption.
        self._log("CISD:\n"
                  f"  {episode.cisd_tf} time: {confirm_candle.t_ny:%H:%M} NY\n"
                  f"  CISD level: {self._fmt(episode.purge_price)}\n"
                  f"  CISD close: {self._fmt(confirm_candle.close)}\n"
                  f"  CISD after completed purge: {self._yn(True)}\n"
                  f"  Purge candle closed: "
                  f"{tu.utc_to_ny(episode.purge_close_utc):%H:%M} NY")

    def _log_fvg_block(self, formed: FVG, min_depth: float, *, valid: bool) -> None:
        self._log("FVG:\n"
                  f"  1M formation time: {formed.formation_time_ny:%H:%M} NY\n"
                  f"  FVG low: {self._fmt(formed.lower)}\n"
                  f"  FVG high: {self._fmt(formed.upper)}\n"
                  f"  FVG size: {self._fmt(formed.depth())}\n"
                  f"  Minimum required: {self._fmt(min_depth)} "
                  f"({self.fvg_min_atr_frac:g} × 5M ATR)\n"
                  f"  Valid: {self._yn(valid)}")

    def _log_retrace_block(self, episode: _Episode, candle, *, entered: bool) -> None:
        self._log("RETRACEMENT:\n"
                  f"  Retracement candle: {candle.t_ny:%H:%M} NY\n"
                  f"  Retracement "
                  f"{'low' if episode.direction == 'buy' else 'high'}: "
                  f"{self._fmt(episode.retrace_extreme)}\n"
                  f"  FVG zone: [{self._fmt(episode.fvg.lower)}, "
                  f"{self._fmt(episode.fvg.upper)}]\n"
                  f"  Entered FVG: {self._yn(entered)}")

    def _log_entry_block(self, episode: _Episode, candle, entry: float,
                         *, entry_inside: bool) -> None:
        self._log("ENTRY:\n"
                  f"  Entry time: {candle.t_ny:%H:%M} NY\n"
                  f"  Entry price: {self._fmt(entry)}\n"
                  f"  FVG zone: [{self._fmt(episode.fvg.lower)}, "
                  f"{self._fmt(episode.fvg.upper)}]\n"
                  f"  Entry model: {self.fvg_entry_model}\n"
                  f"  Inside FVG: {self._yn(entry_inside)}")

    def _atr_m5(self) -> float:
        return atr(self.stream.m5(), period=14)

    def _eq_tolerance(self) -> float:
        a = atr(self.stream.h1(), period=14)
        return a * EQ_TOLERANCE_ATR_MULT if a > 0 else 0.0

    def _tick_size(self) -> float:
        """The symbol's minimum price increment, or 0.0 when unknown.

        Read from the broker's contract spec when the terminal supplied one
        (live sessions), otherwise derived from the registry's ``digits`` (which
        is what an offline backtest has). Never guessed: an unknown tick size
        contributes nothing to the offset instead of inventing a granularity.
        """
        spec = getattr(self.asset, "spec", None)
        tick = float(getattr(spec, "tick_size", 0.0) or 0.0)
        if tick > 0:
            return tick
        digits = int(getattr(self.asset, "digits", 0) or 0)
        return 10.0 ** (-digits) if digits > 0 else 0.0

    def _target_levels(self, candle) -> list[Level]:
        """Liquidity levels known *before* the current candle (no future data)."""
        h1 = self.stream.h1()
        return build_level_snapshot(
            h1,
            as_of_date=candle.t_ny.strftime("%Y-%m-%d"),
            as_of_ny=candle.t_ny,
            lookback_swings=self.swing_lookback,
            swing_tolerance=self._eq_tolerance(),
            session_lookback_days=self.session_lookback_days,
        )


def _dedupe_signals(signals: list[Signal]) -> list[Signal]:
    seen: set[str] = set()
    out: list[Signal] = []
    for s in signals:
        fp = s.fingerprint()
        if fp not in seen:
            seen.add(fp)
            out.append(s)
    return out
