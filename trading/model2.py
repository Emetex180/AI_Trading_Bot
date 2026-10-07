"""Model 2 — session-liquidity purge model on the 15-minute chart.

The model
---------
    a session liquidity level (a completed session high or low, from the previous
    three trading days plus today)
        └─ a closed M15 candle trades *through* that level → the PURGE CANDLE
            └─ price trades back through the far side of the purge candle
                └─ entry at that boundary, immediately (no candle close, no
                   second confirmation candle, no CISD, no FVG, no retracement
                   wait)
                   SL = the far side of the purge candle
                   TP = the nearest still-available session level in the trade
                        direction
                   → deterministic checks → risk approval → Signal

Taking buy-side liquidity (a session **high**) calls for a SELL; taking
sell-side liquidity (a session **low**) calls for a BUY.

Relationship to Model 1
-----------------------
This is an independent engine, not a variant of
:class:`trading.strategy.ICTStrategy`. It shares the *infrastructure* — the
:class:`~trading.stream.BarsStream` candle store, the
:class:`~trading.bars.Candle` model, the New York clock in
:mod:`trading.time_utils`, the :class:`~trading.signal_engine.Signal` schema, the
risk manager and the scanners — but none of Model 1's decisions, and none of
Model 1's session windows (Model 2 has its own table in
:mod:`trading.model2_sessions`). The two engines can therefore run side by side
over the same symbol without either one's state machine being able to disturb
the other's.

Causality (no lookahead)
------------------------
Every rule below is stated so that the answer at candle *T* depends only on
candles that closed at or before *T*:

* A session's extremes enter the liquidity pool only once that session has
  **closed** (:func:`trading.model2_sessions.session_closed_by`). A partly
  formed range is never used, so "today's Asian high" is genuinely unavailable
  while the Asian window is still running.
* The purge is detected on a **closed** M15 candle. Its high and low are
  therefore final when the premise is created, and the entry boundary can never
  move afterwards.
* The take-profit pool is rebuilt **as of the entry instant**, so a level that
  had not yet formed when the trade was taken can never be selected.

The same code drives live scanning and backtesting: closed M1 candles in,
approved :class:`Signal` objects out.

Timeout behaviour
-----------------
A setup has exactly one deadline: the end of the session it was purged in. The
model's hard rule is that an entry must occur in the *same* trading session as
its purge, on the same New York day, so instead of a candle-count timeout there
is a single question — is the clock still inside the purge's own session? — and
"no" is final. An expired setup is dropped, never deferred.

Setup lifecycle
---------------
    WAITING
      ↓  a closed M15 candle takes an available session level
    LIQUIDITY_PURGED → PURGE_CANDLE_IDENTIFIED
      ↓  (the purge candle has closed, so its extremes are final and fixed)
    WAITING_FOR_PURGE_CANDLE_BREAK
      ↓  price trades back through the far side, inside the same session
    ENTRY_TRIGGERED
      ↘  EXPIRED      the originating session ended first
      ↘  INVALIDATED  the entry failed the deterministic or risk checks
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time as _time, timedelta
from typing import Callable

from config import Settings, get_settings

from . import model2_sessions as m2
from .asset_manager import Asset
from .bars import Candle
from .indicators import atr
from .liquidity import Level, select_tp_target, session_extremes
from .risk_manager import (EfficiencyWeights, RiskManager, compute_rr,
                           efficiency_score, risk_reward_points)
from .signal_engine import (APPROVED, MODEL_1, MODEL_2, Signal, build_setup_id)
from .sessions import is_trading_day, parse_trading_days
from .stream import BarsStream

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
#: The only timeframe Model 2 decides on. The purge candle is an M15 candle and
#: nothing else about the model is timeframe-relative.
TIMEFRAME = "M15"

#: The resolution the entry trigger is evaluated at. The scanner feeds closed M1
#: candles, so a break of the purge candle is acted on the minute it happens
#: rather than at an M15 close — which is what "enter immediately" requires.
TRIGGER_TIMEFRAME = "M1"

#: Length of the purge candle, used to place the instant it finishes forming.
M15_MINUTES = 15

# --------------------------------------------------------------------------- #
# Setup states (the lifecycle above, as string constants)
# --------------------------------------------------------------------------- #
WAITING = "WAITING"
LIQUIDITY_PURGED = "LIQUIDITY_PURGED"
#: The purge candle is identified the moment the purge is detected — Model 2
#: never waits for a further candle to qualify it. Kept as its own state so a
#: reader of the dashboard sees the same lifecycle this docstring describes.
PURGE_CANDLE_IDENTIFIED = "PURGE_CANDLE_IDENTIFIED"
WAITING_FOR_PURGE_CANDLE_BREAK = "WAITING_FOR_PURGE_CANDLE_BREAK"
ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
EXPIRED = "EXPIRED"
INVALIDATED = "INVALIDATED"

#: The state a completed signal is filed under — the project's existing terminal
#: vocabulary, so a Model 2 row reads like any other confirmed setup.
TRADE_CONFIRMED = "TRADE_CONFIRMED"

# --------------------------------------------------------------------------- #
# Liquidity sides and directions
# --------------------------------------------------------------------------- #
BUYSIDE = "BUYSIDE"      # a session HIGH resting above price — taking it is bearish
SELLSIDE = "SELLSIDE"    # a session LOW resting below price  — taking it is bullish

#: The direction each purge side calls for.
_DIRECTION_FOR_SIDE: dict[str, str] = {BUYSIDE: "sell", SELLSIDE: "buy"}

#: How a session extreme is expressed as a shared
#: :class:`~trading.liquidity.Level` kind.
_HIGH_KIND = "SESSION_HIGH"
_LOW_KIND = "SESSION_LOW"

#: Cap on the in-process set of already-emitted setup ids, bounding the same
#: structure the strategy module bounds with its own cap.
TRIGGERED_ID_CAP = 5000

#: Default number of *trading* days of history the liquidity pool looks back
#: over, in addition to the current day.
DEFAULT_LOOKBACK_DAYS = 3


# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionLevel:
    """One completed session extreme, with the provenance the model records.

    "Which session high was taken, and from when?" is a question the stored
    signal has to be able to answer, so the answer travels with the level rather
    than being reconstructed afterwards from the price.
    """

    kind: str                  # "SESSION_HIGH" | "SESSION_LOW"
    price: float
    source_session: str        # Model 2 session key, e.g. "m2_london"
    source_label: str          # "London"
    source_date: str           # NY date the session ran on, "YYYY-MM-DD"
    formed_at_ny: datetime     # the instant the session closed

    @property
    def side(self) -> str:
        """``BUYSIDE`` for a high, ``SELLSIDE`` for a low."""
        return BUYSIDE if self.kind == _HIGH_KIND else SELLSIDE

    @property
    def buy_side(self) -> bool:
        return self.kind == _HIGH_KIND

    def as_level(self) -> Level:
        """This level in the shared :class:`~trading.liquidity.Level` form.

        That is the currency :func:`~trading.liquidity.select_tp_target` accepts,
        so take-profit selection is the project's existing implementation rather
        than a second, parallel one.
        """
        return Level(self.kind, self.price, self.formed_at_ny)

    @property
    def label(self) -> str:
        return (f"{self.source_label} {'High' if self.buy_side else 'Low'} "
                f"({self.source_date})")


# --------------------------------------------------------------------------- #
# Internal setup record
# --------------------------------------------------------------------------- #
@dataclass
class _Setup:
    """One pending Model 2 premise, from the purge to the entry (or expiry)."""

    direction: str                 # "buy" | "sell"
    level: SessionLevel            # the session level that was taken
    purge_high: float              # the purge candle's extremes, final at close
    purge_low: float
    purge_time_utc: datetime
    purge_time_ny: datetime
    purge_close_utc: datetime      # when the purge candle finished forming
    session_key: str               # the session the purge occurred in
    session_date: date             # ...and the NY day it ran on
    state: str = PURGE_CANDLE_IDENTIFIED

    @property
    def trigger_price(self) -> float:
        """The price that must be traded through for this setup to fill."""
        return self.purge_low if self.direction == "sell" else self.purge_high

    @property
    def sl_price(self) -> float:
        """The far side of the purge candle — the stop-loss reference."""
        return self.purge_high if self.direction == "sell" else self.purge_low


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Model2Strategy:
    """Per-asset Model 2 engine: session liquidity → purge → break → entry."""

    def __init__(self, asset: Asset, *, settings: Settings | None = None,
                 server_offset_hours: float | None = None,
                 log: Callable[[str], None] | None = None):
        cfg = settings or get_settings()
        self.asset = asset
        self.stream = BarsStream(server_offset_hours=server_offset_hours)
        self.log = log

        def asset_setting(key: str, fallback):
            return asset.settings_value(key, fallback, cfg)

        # --- liquidity pool ---------------------------------------------- #
        self.lookback_days: int = int(asset_setting(
            "model_2_lookback_days",
            getattr(cfg, "model_2_lookback_days", DEFAULT_LOOKBACK_DAYS)))
        self.trading_days: frozenset[int] = parse_trading_days(
            asset_setting("trading_days", getattr(cfg, "trading_days", None)))

        # --- risk --------------------------------------------------------- #
        # The same policy Model 1 answers to: an operator's MIN_RR /
        # RISK_PERCENT apply to every model in the bot, so Model 2 cannot quietly
        # trade geometry the operator has already refused.
        self.min_rr: float = float(asset_setting("min_rr", cfg.min_rr))
        self.risk = RiskManager(
            min_rr=self.min_rr,
            risk_percent=float(asset_setting("risk_percent", cfg.risk_percent)),
        )

        # --- efficiency score (shared formula, shared weights) ------------ #
        self.rr_target: float = float(asset_setting(
            "efficiency_rr_target", cfg.efficiency_rr_target))
        self.atr_target_multiple: float = float(asset_setting(
            "efficiency_atr_target_multiple",
            cfg.efficiency_atr_target_multiple))
        self.weights = EfficiencyWeights(
            rr=float(asset_setting("efficiency_weight_rr",
                                   cfg.efficiency_weight_rr)),
            liquidity=float(asset_setting("efficiency_weight_liquidity",
                                          cfg.efficiency_weight_liquidity)),
            distance=float(asset_setting("efficiency_weight_distance",
                                         cfg.efficiency_weight_distance)),
        )

        # --- setup state --------------------------------------------------- #
        # One pending premise per direction: a buy-side purge produces a SELL
        # premise and a sell-side purge a BUY one, exactly as Model 1 keeps one
        # episode per direction. Two independent events, never one duplicated.
        self._setups: dict[str, _Setup] = {}
        self._last_state: dict[str, str] = {"buy": WAITING, "sell": WAITING}
        self._triggered: set[str] = set()

        #: Session-extreme cache. The expensive part of building the pool is
        #: scanning M15 candles for every session on every day; those prices only
        #: move when a new M15 candle closes, so the scan runs then and every
        #: other call merely *filters* the result by formation time.
        self._level_cache_key: int | None = None
        self._level_cache: list[SessionLevel] = []
        #: The pool's day list only changes when the NY date does.
        self._days_key: date | None = None
        self._days: list[date] = []

    # ------------------------------------------------------------------ #
    # Introspection (read-only; the scanner and dashboard poll these)
    # ------------------------------------------------------------------ #
    def state(self, direction: str) -> str:
        setup = self._setups.get(direction)
        return (setup.state if setup is not None
                else self._last_state.get(direction, WAITING))

    def states(self) -> dict[str, str]:
        """``{"buy": state, "sell": state}`` — the shape the scanner publishes."""
        return {d: self.state(d) for d in ("buy", "sell")}

    @property
    def pending_setups(self) -> int:
        return len(self._setups)

    def session_levels(self, as_of_ny: datetime | None = None
                       ) -> list[SessionLevel]:
        """The liquidity pool, newest session first — a read-only view.

        Defaults to the pool as of the last closed candle, which is what the
        engine itself would use on the next feed. Never consulted by a decision:
        it exists so an operator (or a test) can see *what* is being watched
        without reading the database.
        """
        if as_of_ny is None:
            m15 = self.stream.m15()
            if not m15:
                return []
            as_of_ny = m15[-1].t_ny
        return sorted(self._levels_as_of(as_of_ny),
                      key=lambda lvl: (lvl.source_date, lvl.source_session),
                      reverse=True)

    # ------------------------------------------------------------------ #
    # Warm-up / main entry point
    # ------------------------------------------------------------------ #
    def warm(self, candles: list) -> None:
        """Feed historical closed M1 candles without emitting signals.

        Identical to Model 1's warm-up and for the same reason: the engine is
        brought up to the live boundary with its price history in place, but no
        historical setup is alerted. The session-level cache fills itself on the
        first M15 close, so warm-up stays a pure stream load.
        """
        for candle in candles:
            self.stream.add(candle)

    def feed(self, candle: Candle) -> list[Signal]:
        """Process one newly *closed* M1 candle; return approved signals."""
        update = self.stream.add(candle)
        signals: list[Signal] = []

        # A closed M15 candle is where a session level is taken.
        if update.m15 is not None:
            self._on_m15_closed(update.m15)

        # Every closed minute may break the far side of the purge candle.
        signals += self._check_break(candle)
        return signals

    # ------------------------------------------------------------------ #
    # The liquidity pool
    # ------------------------------------------------------------------ #
    def _pool_days(self, today: date) -> list[date]:
        """Today plus the ``lookback_days`` trading days before it."""
        if self._days_key == today:
            return self._days
        self._days = [today] + self._previous_trading_days(today,
                                                           self.lookback_days)
        self._days_key = today
        return self._days

    def _previous_trading_days(self, day: date, count: int) -> list[date]:
        """The ``count`` most recent trading days strictly before ``day``.

        Weekends are skipped rather than counted, which is what "the previous
        three **trading** days" means: on a Monday the pool reaches back over
        Thursday and Friday rather than into a closed Saturday.
        """
        out: list[date] = []
        cursor = day
        # A bounded walk: even with a pathological TRADING_DAYS a fortnight is
        # more than enough to find `count` trading days.
        for _ in range(max(count * 4, 8)):
            cursor -= timedelta(days=1)
            if is_trading_day(datetime.combine(cursor, _time(0, 0)),
                              self.trading_days):
                out.append(cursor)
                if len(out) >= count:
                    break
        return out

    def _rebuild_level_cache(self) -> None:
        """Every session extreme with data over the pool's day window.

        Availability is *not* decided here — this is the superset, and
        :meth:`_levels_as_of` filters it by formation time. Keeping the two
        apart keeps the scan off the per-candle path while leaving the causality
        rule in exactly one place, so the expensive part and the correct part
        cannot drift out of step.
        """
        m15 = self.stream.m15()
        if not m15:
            self._level_cache = []
            return

        days = self._pool_days(m15[-1].t_ny.date())
        pool = self._m15_from(m15, min(days))

        cached: list[SessionLevel] = []
        for day in days:
            day_key = day.strftime("%Y-%m-%d")
            for window in m2.MODEL_2_SESSIONS:
                extremes = session_extremes(pool, window, day_key)
                if extremes is None:
                    continue
                high, low = extremes
                closed_at = m2.session_bounds_ny(day, window)[1]
                cached.append(SessionLevel(_HIGH_KIND, high, window.key,
                                           window.label, day_key, closed_at))
                cached.append(SessionLevel(_LOW_KIND, low, window.key,
                                           window.label, day_key, closed_at))
        self._level_cache = cached

    @staticmethod
    def _m15_from(m15: list[Candle], oldest: date) -> list[Candle]:
        """The tail of the M15 series reaching back to ``oldest`` inclusive.

        The pool window is a few days wide, so handing ``session_extremes`` only
        those candles keeps each rebuild proportional to the window rather than
        to however long the process has been running. The series is in time
        order, so the cut point is found from the end.
        """
        for i in range(len(m15) - 1, -1, -1):
            if m15[i].t_ny.date() < oldest:
                return m15[i + 1:]
        return m15

    def _levels_as_of(self, as_of_ny: datetime) -> list[SessionLevel]:
        """The liquidity pool that had actually formed by ``as_of_ny``.

        A session extreme is usable only once its session has closed
        (``formed_at_ny <= as_of_ny``), which is the model's no-lookahead rule
        stated as a single comparison. Levels whose session is still running are
        excluded even though :meth:`_rebuild_level_cache` has already measured
        them.
        """
        key = len(self.stream.m15())
        if key != self._level_cache_key:
            self._rebuild_level_cache()
            self._level_cache_key = key
        return [lvl for lvl in self._level_cache if lvl.formed_at_ny <= as_of_ny]

    # ------------------------------------------------------------------ #
    # Step 1 — the purge (detected on a closed M15 candle)
    # ------------------------------------------------------------------ #
    def _on_m15_closed(self, candle: Candle) -> None:
        # Every Model 2 window opens and closes on a quarter hour (20:00, 24:00,
        # 01:00, 06:00, 07:00, 11:00, 13:00, 17:00), so a 15-minute candle can
        # never straddle a boundary and its opening instant alone settles which
        # session it belongs to.
        window = m2.session_at(candle.t_ny)
        if window is None:
            # Outside every defined session there is no premise to build, and
            # therefore no valid entry either.
            return

        levels = self._levels_as_of(candle.t_ny)

        # Strict comparisons: "trades above" / "trades below". A candle that
        # merely *touches* a level leaves it resting and is not a purge.
        buyside = [lvl for lvl in levels
                   if lvl.buy_side and candle.high > lvl.price]
        sellside = [lvl for lvl in levels
                    if not lvl.buy_side and candle.low < lvl.price]

        if buyside:
            self._start_setup(_DIRECTION_FOR_SIDE[BUYSIDE],
                              self._furthest(buyside, BUYSIDE), candle, window)
        if sellside:
            self._start_setup(_DIRECTION_FOR_SIDE[SELLSIDE],
                              self._furthest(sellside, SELLSIDE), candle, window)

    @staticmethod
    def _furthest(levels: list[SessionLevel], side: str) -> SessionLevel:
        """Which of several levels one candle took is the one that counts.

        A candle can sweep a cascade of resting levels at once (a London low and
        a previous day's NY AM low sitting a few points apart). The level
        anchored on is the one furthest along the sweep — the highest high of an
        upward sweep, the lowest low of a downward one — which is the deepest
        liquidity actually removed. Ties on price break towards the more
        recently formed level, and that is decisive: two sessions on two days
        close at different instants, so one candle and one level set always give
        one answer.

        Choosing here rather than in the caller is what keeps a cascade to
        exactly **one** setup, and therefore what stops a single market event
        from becoming several duplicate trades.
        """
        price = (max if side == BUYSIDE else min)(lvl.price for lvl in levels)
        tied = [lvl for lvl in levels if lvl.price == price]
        return max(tied, key=lambda lvl: lvl.formed_at_ny)

    def _start_setup(self, direction: str, level: SessionLevel, candle: Candle,
                     window) -> None:
        """Create (or replace) this direction's pending setup."""
        existing = self._setups.get(direction)
        if existing is not None:
            # A fresher purge supersedes a premise that has not filled yet. The
            # older one is only ever *replaced*, never carried alongside, so a
            # direction holds at most one live premise and a cascade of purges
            # cannot become a flurry of entries.
            self._log(f"a {level.label} level was purged while a {direction} "
                      f"setup from {existing.purge_time_ny:%H:%M} NY was still "
                      f"pending — replacing it")

        setup = _Setup(
            direction=direction,
            level=level,
            purge_high=candle.high,
            purge_low=candle.low,
            purge_time_utc=candle.t_utc,
            purge_time_ny=candle.t_ny,
            purge_close_utc=candle.t_utc + timedelta(minutes=M15_MINUTES),
            session_key=window.key,
            session_date=candle.t_ny.date(),
        )
        self._setups[direction] = setup
        self._last_state[direction] = PURGE_CANDLE_IDENTIFIED
        self._log_setup_block(setup, window)

    # ------------------------------------------------------------------ #
    # Step 2 — the break of the purge candle (evaluated on each closed M1)
    # ------------------------------------------------------------------ #
    def _check_break(self, candle: Candle) -> list[Signal]:
        signals: list[Signal] = []
        for direction in ("buy", "sell"):
            setup = self._setups.get(direction)
            if setup is None:
                continue

            # The purge candle is only identified once it has *closed*, so its
            # far side can only be broken afterwards. Inside the purge candle the
            # condition is arithmetically impossible anyway — an M1 low can never
            # undercut the low of the M15 candle containing it — but stating it
            # keeps the rule explicit and keeps a mid-candle re-feed from being
            # read as a trigger.
            if candle.t_utc < setup.purge_close_utc:
                continue

            if not self._session_still_open(setup, candle.t_ny):
                self._expire(direction, "the session it was purged in ended "
                                        "before the purge candle was broken")
                continue

            triggered = (candle.low < setup.purge_low if direction == "sell"
                         else candle.high > setup.purge_high)
            if not triggered:
                setup.state = WAITING_FOR_PURGE_CANDLE_BREAK
                self._last_state[direction] = WAITING_FOR_PURGE_CANDLE_BREAK
                continue

            signal = self._build_signal(setup, candle)
            self._setups.pop(direction, None)
            if signal is not None:
                self._last_state[direction] = ENTRY_TRIGGERED
                signals.append(signal)
            else:
                self._last_state[direction] = INVALIDATED
        return signals

    def _session_still_open(self, setup: _Setup, ny_dt: datetime) -> bool:
        """Is ``ny_dt`` inside the very session the purge happened in?

        The hard rule of the model: an entry may only be taken in its own
        session, on its own New York day. 05:59 is a valid entry for a London
        purge and 06:00 is not — the window itself answers the question, so
        there is no separate deadline to drift out of step with the session
        table.
        """
        if ny_dt.date() != setup.session_date:
            return False
        window = m2.MODEL_2_SESSION_INDEX.get(setup.session_key)
        if window is None:  # pragma: no cover — keys come from the table itself
            return False
        return window.contains(ny_dt.hour * 60 + ny_dt.minute)

    # ------------------------------------------------------------------ #
    # Entry construction
    # ------------------------------------------------------------------ #
    def _build_signal(self, setup: _Setup, candle: Candle) -> Signal | None:
        direction = setup.direction
        entry = setup.trigger_price
        sl = setup.sl_price
        entry_ny = candle.t_ny

        if not is_trading_day(entry_ny, self.trading_days):
            self._reject("not a trading day")
            return None

        # ---- take profit: the nearest still-available session level ------- #
        # Rebuilt as of *this* instant, so a level that had not yet formed when
        # the purge happened, or that formed between the purge and the break, is
        # handled correctly either way.
        pool = [lvl.as_level() for lvl in self._levels_as_of(entry_ny)]
        pick = select_tp_target(entry, direction, pool, offset=0.0)
        if pick is None:
            # The model is explicit: no valid session liquidity in the trade's
            # direction means the setup is refused, not given a fixed fallback.
            self._reject("no session liquidity in the trade direction to target")
            return None
        target, tp = pick

        if entry <= 0 or sl <= 0 or tp <= 0:
            self._reject("invalid SL/TP geometry")
            return None

        # ---- geometry, ratio and risk: Model 1's own gates ---------------- #
        risk_points, reward_points = risk_reward_points(entry, sl, tp, direction)
        rr = compute_rr(entry, sl, tp, direction)
        if rr <= 0:
            self._reject("invalid SL/TP geometry")
            return None
        if rr < self.min_rr:
            self._reject(f"RR below the minimum ({rr:.2f} < {self.min_rr:.2f})")
            return None
        decision = self.risk.approve(entry, sl, tp, direction)
        if not decision.approved:
            self._reject(decision.reason)
            return None

        setup_id = build_setup_id(self.asset.name, direction,
                                  setup.purge_time_utc, candle.t_utc, None,
                                  model=MODEL_2)
        if setup_id in self._triggered:
            self._reject("duplicate setup")
            return None

        score = efficiency_score(
            rr=rr,
            rr_target=self.rr_target,
            liquidity_grade=target.grade,
            reward_points=reward_points,
            atr=atr(self.stream.m15(), period=14),
            atr_target_multiple=self.atr_target_multiple,
            weights=self.weights,
        )

        self._mark_triggered(setup_id)
        self._log_entry_block(setup, candle, entry, sl, tp, target, rr, score)

        return Signal(
            asset=self.asset.name,
            direction=direction,
            entry=entry,
            sl=sl,
            tp=tp,
            entry_time_utc=candle.t_utc,
            entry_time_ny=entry_ny,
            session_keys=[setup.session_key],
            session_primary=setup.session_key,
            silver_bullet=None,
            macro=None,
            # The level that was *taken*, and its strength.
            liquidity_type=setup.level.kind,
            liquidity_price=setup.level.price,
            purge_grade=setup.level.as_level().grade,
            purge_time_ny=setup.purge_time_ny,
            # Model 2 has no CISD and no FVG. The CISD field carries the
            # timeframe the purge itself was detected on — the only confirmation
            # this model has — and the FVG columns are left at their defaults
            # rather than filled with something that did not happen, so neither
            # the alert nor the database claims a step the model does not take.
            cisd_tf=TIMEFRAME,
            cisd_confirm_time_ny=setup.purge_time_ny,
            structure_extreme_price=sl,
            structure_time_ny=setup.purge_time_ny,
            target_kind=target.kind,
            target_price=target.price,
            target_grade=target.grade,
            risk_points=risk_points,
            reward_points=reward_points,
            efficiency_score=score,
            setup_id=setup_id,
            state=TRADE_CONFIRMED,
            digits=int(getattr(self.asset, "digits", 0) or 0),
            rr=rr,
            status=APPROVED,
            risk_approved=True,
            alert_only=True,
            model=MODEL_2,
            model_meta=self._meta_json(setup, candle),
        )

    def _meta_json(self, setup: _Setup, candle: Candle) -> str:
        """The Model 2 provenance payload, as a JSON string.

        Stored as text rather than through the ORM's JSON type so the column
        behaves identically on SQLite and PostgreSQL — the two backends this
        project targets — with no type cast in the additive-migration DDL.
        """
        return json.dumps({
            "model": MODEL_2,
            "timeframe": TIMEFRAME,
            "direction": setup.direction,
            "liquidity_side": setup.level.side,
            "liquidity_level": setup.level.price,
            "liquidity_source_session": setup.level.source_session,
            "liquidity_source_session_label": setup.level.source_label,
            "liquidity_source_date": setup.level.source_date,
            "purge_candle_time_ny": setup.purge_time_ny.isoformat(),
            "purge_candle_high": setup.purge_high,
            "purge_candle_low": setup.purge_low,
            "originating_session": setup.session_key,
            "entry_trigger_candle_ny": candle.t_ny.isoformat(),
            "setup_status": TRADE_CONFIRMED,
        }, sort_keys=True)

    # ------------------------------------------------------------------ #
    # Bookkeeping
    # ------------------------------------------------------------------ #
    def _mark_triggered(self, setup_id: str) -> None:
        if len(self._triggered) >= TRIGGERED_ID_CAP:
            self._triggered.clear()
        self._triggered.add(setup_id)

    def _expire(self, direction: str, reason: str) -> None:
        """Drop a pending setup. An expired setup can never trigger later."""
        self._setups.pop(direction, None)
        self._last_state[direction] = EXPIRED
        self._log(f"setup EXPIRED — {reason}")

    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        if self.log is None:
            return
        try:
            self.log(f"[{self.asset.name}] [M2] {message}")
        except Exception:  # a broken log sink must never stop the engine
            pass

    def _reject(self, reason: str) -> None:
        self._log(f"setup rejected: {reason}")

    def _fmt(self, value: float) -> str:
        digits = int(getattr(self.asset, "digits", 0) or 0)
        return f"{value:.{digits}f}" if digits > 0 else f"{value:g}"

    def _log_setup_block(self, setup: _Setup, window) -> None:
        self._log(
            f"PURGE — {setup.level.label} @ {self._fmt(setup.level.price)} "
            f"({setup.level.side})\n"
            f"  M15 purge candle: {setup.purge_time_ny:%Y-%m-%d %H:%M} NY  "
            f"H {self._fmt(setup.purge_high)} / L {self._fmt(setup.purge_low)}\n"
            f"  Session: {window.label}\n"
            f"  Expected: {setup.direction.upper()} on a break of "
            f"{self._fmt(setup.trigger_price)} (SL {self._fmt(setup.sl_price)})")

    def _log_entry_block(self, setup: _Setup, candle: Candle, entry: float,
                         sl: float, tp: float, target: Level, rr: float,
                         score: float) -> None:
        anchor = "high" if setup.direction == "sell" else "low"
        self._log(
            f"ENTRY — {setup.direction.upper()} {self._fmt(entry)} at "
            f"{candle.t_ny:%Y-%m-%d %H:%M} NY\n"
            f"  Stop loss {self._fmt(sl)} (purge candle {anchor})\n"
            f"  Take profit {self._fmt(tp)} ({target.kind})\n"
            f"  RR {rr:.2f}  |  Efficiency {score:.0f}%")


# --------------------------------------------------------------------------- #
# Reading a signal's model tag back
# --------------------------------------------------------------------------- #
def model_label(signal: Signal) -> str:
    """The model tag of a signal, defaulting to Model 1 when unset."""
    return getattr(signal, "model", "") or MODEL_1


def model_meta(signal: Signal) -> dict:
    """The decoded Model 2 provenance payload, or ``{}`` for any other model."""
    raw = getattr(signal, "model_meta", "") or ""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def is_model_2(signal: Signal) -> bool:
    return model_label(signal) == MODEL_2


__all__ = [
    "MODEL_1", "MODEL_2", "TIMEFRAME", "TRIGGER_TIMEFRAME", "Model2Strategy",
    "SessionLevel",
    "WAITING", "LIQUIDITY_PURGED", "PURGE_CANDLE_IDENTIFIED",
    "WAITING_FOR_PURGE_CANDLE_BREAK", "ENTRY_TRIGGERED", "EXPIRED",
    "INVALIDATED", "TRADE_CONFIRMED", "BUYSIDE", "SELLSIDE",
    "is_model_2", "model_label", "model_meta",
]
