"""Model 2: three-calendar-day session liquidity -> M15 purge -> M5 CISD.

Only completed sessions from today and the two preceding NY calendar days are
eligible. Every breach consumes that session extreme, even outside a session.
The completed M15 purge candle supplies the immutable stop reference. Only full
M5 candles opening after that purge closes can confirm the shared wick/reclaim
CISD definition. No opposite-side break or FVG is required.

Historical replay enters at the confirmed M5 close (a zero-spread bar-close
simulation, not a tick fill). Live scanners wait for a fresh bid/ask observation
and revalidate geometry at ask for BUY / bid for SELL. Neither path carries a
setup past its originating session, a stop breach or the CISD waiting window.

TP is the nearest unconsumed session extreme on the correct side of the proposed
entry, chosen BEFORE RR validation (>= 1.50, or a stricter configured minimum).
The scanner persists consumption tombstones in the existing event table; replay
never restores entries anchored to those spent levels. Model 1 is independent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite
from typing import Callable

from config import Settings, get_settings, model_enabled

from . import cisd as cisd_mod

from . import model2_sessions as m2
from .asset_manager import Asset
from .bars import Candle, epoch_minute
from .indicators import atr
from .liquidity import Level, select_tp_target, session_extremes
from .risk_manager import (EfficiencyWeights, RiskDecision, RiskManager, compute_rr,
                           efficiency_score, risk_reward_points)
from .signal_engine import (APPROVED, MODEL_1, MODEL_2, Signal, build_setup_id)
from .sessions import is_trading_day, parse_trading_days
from . import time_utils as tu
from .stream import BarsStream

# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
#: Purge and confirmation timeframes are fixed, independent of Model 1 settings.
TIMEFRAME = "M15"
CISD_TIMEFRAME = "M5"
TRIGGER_TIMEFRAME = CISD_TIMEFRAME

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
WAITING_FOR_CISD = "WAITING_FOR_CISD"
CISD_CONFIRMED = "CISD_CONFIRMED"
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

#: Calendar days INCLUDING today. This is a strategy invariant, not a tuning knob.
DEFAULT_LOOKBACK_DAYS = 3
MINIMUM_RR = 1.50


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
    def identity(self) -> str:
        """Stable across replay, independent of reconstructed price precision."""
        return f"{self.source_date}/{self.source_session}/{self.kind}"

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
    stop_price: float             # buffered, tick-aligned M15 anchor; immutable
    confirmation: Candle | None = None
    state: str = PURGE_CANDLE_IDENTIFIED

    @property
    def sl_price(self) -> float:
        """Stop beyond the original M15 extreme, never the CISD candle."""
        return self.stop_price


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Model2RiskManager(RiskManager):
    """Shared policy with decimal-price geometry for Model 2's exact RR floor."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.min_rr = Decimal(str(max(MINIMUM_RR, self.min_rr)))

    def approve(self, entry, sl, tp, direction):
        if not all(isfinite(p) and p > 0 for p in (entry, sl, tp)):
            return RiskDecision(False, "invalid_sl_tp")
        return super().approve(*(Decimal(str(p)) for p in (entry, sl, tp)), direction)


class Model2Strategy:
    """Per-asset Model 2 engine: session liquidity → purge → CISD → entry."""

    def __init__(self, asset: Asset, *, settings: Settings | None = None,
                 server_offset_hours: float | None = None,
                 log: Callable[[str], None] | None = None,
                 quote_entries: bool = False):
        cfg = settings or get_settings()
        self.asset = asset
        self.stream = BarsStream(server_offset_hours=server_offset_hours)
        self.log = log

        def asset_setting(key: str, fallback):
            return asset.settings_value(key, fallback, cfg)

        # --- liquidity pool ---------------------------------------------- #
        self.enabled = model_enabled(cfg, MODEL_2, asset)
        self.quote_entries = quote_entries
        self.max_cisd_candles = max(1, int(asset_setting("max_cisd_candles", cfg.max_cisd_candles)))
        self.sl_min_offset_ticks = max(1.0, float(asset_setting(
            "sl_min_offset_ticks", cfg.sl_min_offset_ticks)))
        self.quote_max_age = max(5.0, 2 * cfg.scanner_poll_interval_ms / 1000.0)
        # Keep the legacy settings field readable, but never widen/shorten this
        # model's specified three-calendar-day window.
        self.lookback_days = DEFAULT_LOOKBACK_DAYS
        self.trading_days: frozenset[int] = parse_trading_days(
            asset_setting("trading_days", getattr(cfg, "trading_days", None)))

        # --- risk --------------------------------------------------------- #
        # The same policy Model 1 answers to: an operator's MIN_RR /
        # RISK_PERCENT apply to every model in the bot, so Model 2 cannot quietly
        # trade geometry the operator has already refused.
        configured_rr = float(asset_setting("min_rr", cfg.min_rr))
        if not isfinite(configured_rr):
            raise ValueError("Model 2 MIN_RR must be finite")
        self.min_rr = max(MINIMUM_RR, configured_rr)
        self.risk = Model2RiskManager(
            min_rr=self.min_rr,
            risk_percent=float(asset_setting("risk_percent", cfg.risk_percent)),
            spec=getattr(asset, "spec", None),
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
        self._consumed: dict[str, datetime] = {}
        self._bucket_purges: dict[datetime, list[SessionLevel]] = {}
        self._closed_m15: list[Candle] = []
        self._last_quote: tuple | None = None
        self._as_of_utc: datetime | None = None
        if tu.NY_TZ is None:
            raise ValueError("Model 2 requires America/New_York timezone data")

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
            if self._as_of_utc is None:
                return []
            as_of_ny = tu.utc_to_ny(self._as_of_utc)
        return sorted(self._levels_as_of(as_of_ny),
                      key=lambda lvl: (lvl.source_date, lvl.source_session),
                      reverse=True)

    # ------------------------------------------------------------------ #
    # Warm-up / main entry point
    # ------------------------------------------------------------------ #
    def warm(self, candles: list) -> None:
        """Replay consumption/expiry too, but never publish historical signals."""
        live = self.quote_entries
        self.quote_entries = False
        try:
            for candle in sorted(candles, key=lambda c: c.t_utc):
                self.feed(candle)
        finally:
            self.quote_entries = live

    def feed(self, candle: Candle) -> list[Signal]:
        """Advance only closed M1 information; confirm at the actual M5 close."""
        last = self.stream.last_m1()
        if last is not None and candle.t_utc <= last.t_utc:
            return []
        self.stream.add(candle)
        if not self.enabled:
            return []
        observed = candle.t_utc + timedelta(minutes=1)
        # Stops and pool consumption during confirmation are known before entry.
        self._invalidate_stops(candle)
        self._consume(candle, observed)
        self._finalize_m15(candle)
        signals = self._confirm_m5(candle)
        self._as_of_utc = max(observed, self._as_of_utc or observed)
        self.expire_at(self._as_of_utc)
        return signals

    def _finalize_m15(self, candle: Candle) -> None:
        """Finalize on minute 14's close, without waiting for minute 15 to close.

        Local to Model 2: do not change Model 1's BarsStream cadence. Partial
        buckets cannot establish exact purge extremes; their levels stay spent.
        """
        start = candle.t_utc - timedelta(minutes=epoch_minute(candle.t_utc) % 15)
        for old in list(self._bucket_purges):
            if old < start:
                self._bucket_purges.pop(old)
        if epoch_minute(candle.t_utc) % 15 != 14:
            return
        minutes = self.stream.m1[-15:]
        if len(minutes) != 15 or any(
                c.t_utc != start + timedelta(minutes=i)
                for i, c in enumerate(minutes)):
            self._bucket_purges.pop(start, None)
            return
        bar = Candle(start, tu.utc_to_ny(start), minutes[0].open,
                     max(c.high for c in minutes), min(c.low for c in minutes),
                     minutes[-1].close, sum(c.volume for c in minutes))
        self._closed_m15.append(bar)
        self._on_m15_closed(bar)

    def _consume(self, candle: Candle, observed_utc: datetime) -> None:
        """Spend only breached levels, inside OR outside a trading session."""
        start = candle.t_utc - timedelta(
            minutes=epoch_minute(candle.t_utc) % 15,
            seconds=candle.t_utc.second, microseconds=candle.t_utc.microsecond)
        for level in self._levels_as_of(candle.t_ny):
            crossed = (candle.high > level.price if level.buy_side
                       else candle.low < level.price)
            if crossed and level.identity not in self._consumed:
                self._consumed[level.identity] = observed_utc
                self._bucket_purges.setdefault(start, []).append(level)

    def feed_quote(self, *, t_utc: datetime, bid: float, ask: float) -> list[Signal]:
        """Observe an existing runner quote. Bid matches the broker OHLC chart;
        proposed market entry is ask for BUY and bid for SELL, never the trigger.
        """
        self.expire_at(t_utc)
        if not self.enabled or not all(isfinite(p) and p > 0 for p in (bid, ask)):
            return []
        if ask < bid or (self._as_of_utc is not None and t_utc < self._as_of_utc):
            return []
        key = (t_utc, bid, ask)
        if self._last_quote is not None and (
                key == self._last_quote or t_utc < self._last_quote[0]):
            return []
        self._last_quote = key
        candle = Candle(t_utc, tu.utc_to_ny(t_utc), bid, bid, bid, bid)
        self._invalidate_stops(Candle(t_utc, candle.t_ny, bid, ask, bid, bid))
        self._consume(candle, t_utc)
        signals = []
        for direction, setup in list(self._setups.items()):
            if setup.confirmation is None:
                continue
            confirmed_at = setup.confirmation.t_utc + timedelta(minutes=5)
            if not 0 <= (t_utc - confirmed_at).total_seconds() <= self.quote_max_age:
                continue
            signal = self._finish_entry(setup, candle,
                                        ask if direction == "buy" else bid,
                                        live_quote=True)
            if signal is not None:
                signals.append(signal)
        self._as_of_utc = t_utc
        return signals

    def expire_at(self, t_utc: datetime) -> None:
        """Clock-driven expiry also works when no candles/quotes arrive."""
        ny = tu.utc_to_ny(t_utc)
        for direction, setup in list(self._setups.items()):
            if not self._session_still_open(setup, ny):
                self._expire(direction, "the originating session ended")
            elif setup.confirmation is not None:
                closed = setup.confirmation.t_utc + timedelta(minutes=5)
                if (t_utc - closed).total_seconds() > self.quote_max_age:
                    self._expire(direction, "no fresh quote after M5 CISD")
            else:
                deadline = setup.purge_close_utc + timedelta(minutes=5 * self.max_cisd_candles)
                # The runner checks the clock BEFORE polling newly closed bars.
                # Give the final eligible M5 close its normal quote-freshness
                # budget to arrive, but never accept a candle after the deadline.
                processed_deadline = self._as_of_utc is not None and self._as_of_utc >= deadline
                if t_utc >= deadline and (processed_deadline or
                        (t_utc - deadline).total_seconds() > self.quote_max_age):
                    self._expire(direction, "no M5 CISD within the waiting window")

    def consumption_state(self) -> dict:
        """Small durable tombstone snapshot; no pending entries are restored."""
        if self._as_of_utc is not None:
            oldest = min(self._pool_days(tu.utc_to_ny(self._as_of_utc).date())).isoformat()
            self._consumed = {key: stamp for key, stamp in self._consumed.items()
                              if key.split("/", 1)[0] >= oldest}
        return {"version": 1, "consumed": {
            key: stamp.isoformat() for key, stamp in sorted(self._consumed.items())}}

    def restore_consumption(self, state: dict) -> None:
        if state.get("version") != 1:
            raise ValueError("Unsupported Model 2 consumption state")
        self._consumed.update({key: datetime.fromisoformat(stamp)
                               for key, stamp in state["consumed"].items()})
        self._setups.clear()
        self._bucket_purges.clear()

    # ------------------------------------------------------------------ #
    # The liquidity pool
    # ------------------------------------------------------------------ #
    def _pool_days(self, today: date) -> list[date]:
        """Today, yesterday, and the day before, including weekends."""
        if self._days_key != today:
            self._days = [today - timedelta(days=i) for i in range(self.lookback_days)]
            self._days_key = today
        return self._days

    def _rebuild_level_cache(self) -> None:
        """Every session extreme with data over the pool's day window.

        Availability is *not* decided here — this is the superset, and
        :meth:`_levels_as_of` filters it by formation time. Keeping the two
        apart keeps the scan off the per-candle path while leaving the causality
        rule in exactly one place, so the expensive part and the correct part
        cannot drift out of step.
        """
        m15 = self._closed_m15
        if not m15:
            self._level_cache = []
            return

        days = self._pool_days(m15[-1].t_ny.date())
        pool = self._m15_from(m15, min(days))

        cached: list[SessionLevel] = []
        observed_buckets = {c.t_utc for c in pool}
        for day in days:
            day_key = day.strftime("%Y-%m-%d")
            for window in m2.MODEL_2_SESSIONS:
                opened_at, closed_at = m2.session_bounds_ny(day, window)
                # A warm-up starting mid-session cannot know that full range.
                if self.stream.m1[0].t_utc > tu.ny_to_utc(opened_at):
                    continue
                opened_utc, closed_utc = tu.ny_to_utc(opened_at), tu.ny_to_utc(closed_at)
                count = int((closed_utc - opened_utc).total_seconds() // 900)
                if any(opened_utc + timedelta(minutes=15 * i) not in observed_buckets
                       for i in range(count)):
                    # A missing part of the session is unknown, not a smaller
                    # high/low. UTC bucket coverage also handles repeated DST hours.
                    continue
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
        key = len(self._closed_m15)
        if key != self._level_cache_key:
            self._rebuild_level_cache()
            self._level_cache_key = key
        days = {day.isoformat() for day in self._pool_days(as_of_ny.date())}
        as_of_utc = tu.ny_to_utc(as_of_ny)
        return [lvl for lvl in self._level_cache
                if lvl.source_date in days and lvl.formed_at_ny <= as_of_ny
                and (lvl.identity not in self._consumed
                     or self._consumed[lvl.identity] > as_of_utc)]

    # ------------------------------------------------------------------ #
    # Step 1 — the purge (detected on a closed M15 candle)
    # ------------------------------------------------------------------ #
    def _on_m15_closed(self, candle: Candle) -> None:
        # Every Model 2 window opens and closes on a quarter hour (20:00, 24:00,
        # 01:00, 06:00, 07:00, 11:00, 13:00, 17:00), so a 15-minute candle can
        # never straddle a boundary and its opening instant alone settles which
        # session it belongs to.
        levels = self._bucket_purges.pop(candle.t_utc, [])
        window = m2.session_at(candle.t_ny)
        if window is None or not is_trading_day(candle.t_ny, self.trading_days):
            # Consumption was already recorded, including out-of-session bars.
            return
        buyside = [lvl for lvl in levels if lvl.buy_side]
        sellside = [lvl for lvl in levels if not lvl.buy_side]

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
                      f"setup from {tu.format_ny_12h(existing.purge_time_ny)} NY was still "
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
            stop_price=self._stop_price(candle, direction),
        )
        self._setups[direction] = setup
        self._last_state[direction] = PURGE_CANDLE_IDENTIFIED
        self._log_setup_block(setup, window)

    # ------------------------------------------------------------------ #
    # Step 2 — closed M5 CISD, using the unchanged shared reclaim detector
    # ------------------------------------------------------------------ #
    def _invalidate_stops(self, candle: Candle) -> None:
        for direction, setup in list(self._setups.items()):
            if candle.t_utc < setup.purge_close_utc:
                continue
            hit = (candle.low <= setup.sl_price if direction == "buy"
                   else candle.high >= setup.sl_price)
            if hit:
                self._setups.pop(direction)
                self._last_state[direction] = INVALIDATED
                self._reject("M15-anchored stop breached before entry")

    def _confirm_m5(self, candle: Candle) -> list[Signal]:
        if epoch_minute(candle.t_utc) % 5 != 4:
            return []
        minutes = self.stream.m1[-5:]
        start = candle.t_utc - timedelta(minutes=4)
        # A partial aggregate is not a confirmed candle, including after gaps.
        if len(minutes) != 5 or any(
                c.t_utc != start + timedelta(minutes=i)
                for i, c in enumerate(minutes)):
            return []
        bar = Candle(start, tu.utc_to_ny(start), minutes[0].open,
                     max(c.high for c in minutes), min(c.low for c in minutes),
                     minutes[-1].close, sum(c.volume for c in minutes))
        closed = candle.t_utc + timedelta(minutes=1)
        signals = []
        for direction, setup in list(self._setups.items()):
            if setup.confirmation is not None or start < setup.purge_close_utc:
                continue
            if (not self._session_still_open(setup, tu.utc_to_ny(closed))
                    or closed > setup.purge_close_utc + timedelta(minutes=5 * self.max_cisd_candles)):
                self._expire(direction, "session or M5 CISD waiting window ended")
                continue
            setup.state = WAITING_FOR_CISD
            confirmed = cisd_mod.confirm(
                [bar], setup.level.price, direction,
                after_time_utc=setup.purge_close_utc,
                params=cisd_mod.CISDParams(max_candles=1))
            if confirmed is None:
                continue
            setup.confirmation = bar
            setup.state = CISD_CONFIRMED
            self._log(f"{direction.upper()} M5 CISD confirmed at "
                      f"{tu.format_ny_stamp(tu.utc_to_ny(closed))} NY")
            if not self.quote_entries:
                observation = Candle(closed, tu.utc_to_ny(closed), bar.close,
                                     bar.close, bar.close, bar.close)
                signal = self._finish_entry(setup, observation, bar.close)
                if signal is not None:
                    signals.append(signal)
        return signals

    def _finish_entry(self, setup: _Setup, candle: Candle, entry: float, *,
                      live_quote: bool = False) -> Signal | None:
        signal = self._build_signal(setup, candle, entry=entry, live_quote=live_quote)
        self._setups.pop(setup.direction, None)
        self._last_state[setup.direction] = ENTRY_TRIGGERED if signal else INVALIDATED
        return signal

    def _stop_price(self, candle: Candle, direction: str) -> float:
        """At least one tick beyond M15, using the existing offset setting.

        Round outward onto the broker tick grid; registry digits are the same
        offline fallback Model 1 uses. Unknown precision cannot invent a stop.
        """
        spec = getattr(self.asset, "spec", None)
        tick = float(getattr(spec, "tick_size", 0.0) or 0.0)
        digits = int(spec.digits if spec is not None else self.asset.digits)
        if tick <= 0 and digits > 0:
            tick = 10.0 ** -digits
        if not isfinite(tick) or tick <= 0:
            return float("nan")
        step = Decimal(str(tick))
        offset = Decimal(str(self.sl_min_offset_ticks)).to_integral_value(rounding=ROUND_CEILING)
        anchor = Decimal(str(candle.low if direction == "buy" else candle.high)) / step
        value = ((anchor.to_integral_value(rounding=ROUND_FLOOR) - offset) if direction == "buy"
                 else (anchor.to_integral_value(rounding=ROUND_CEILING) + offset)) * step
        return self._price(float(value))

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
    def _build_signal(self, setup: _Setup, candle: Candle, *,
                      entry: float | None = None, live_quote: bool = False
                      ) -> Signal | None:
        direction = setup.direction
        if setup.confirmation is None or not self._session_still_open(setup, candle.t_ny):
            return None
        entry = self._price(setup.confirmation.close if entry is None else entry)
        sl = self._price(setup.sl_price)
        entry_ny = candle.t_ny
        if (not all(isfinite(p) and p > 0 for p in (entry, sl))
                or not (sl < setup.purge_low if direction == "buy" else sl > setup.purge_high)):
            self._reject("invalid SL/TP geometry")
            return None

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
        tp = self._price(tp)

        if not isfinite(tp) or tp <= 0:
            self._reject("invalid SL/TP geometry")
            return None

        # ---- geometry, ratio and risk: Model 1's own gates ---------------- #
        risk_points, reward_points = risk_reward_points(entry, sl, tp, direction)
        # Decimal prices avoid rejecting exactly 1.50 because binary subtraction
        # turned, for example, 0.30 / 0.20 into 1.4999999999999998.
        prices = tuple(Decimal(str(p)) for p in (entry, sl, tp))
        rr = float(compute_rr(*prices, direction))
        if not all(isfinite(p) and p > 0 for p in (risk_points, reward_points, rr)):
            self._reject("invalid SL/TP geometry")
            return None
        if rr < self.min_rr:
            self._reject(f"RR below the minimum ({rr:.2f} < {self.min_rr:.2f})")
            return None
        decision = self.risk.approve(*prices, direction)
        if not decision.approved:
            self._reject(decision.reason)
            return None

        setup_id = build_setup_id(self.asset.name, direction,
                                  setup.purge_time_utc, None, None,
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
            # M5 confirms; the immutable M15 candle still supplies the stop.
            # As with Model 1, this field labels the confirming candle's OPEN.
            cisd_tf=CISD_TIMEFRAME,
            cisd_confirm_time_ny=setup.confirmation.t_ny,
            structure_extreme_price=(setup.purge_low if direction == "buy" else setup.purge_high),
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
            model_meta=self._meta_json(setup, candle, live_quote=live_quote),
        )

    def _price(self, value: float) -> float:
        """Match Executor's broker-digits normalization before RR approval."""
        spec = getattr(self.asset, "spec", None)
        digits = int(spec.digits if spec is not None else self.asset.digits)
        return round(float(value), digits) if digits > 0 else float(value)

    def _meta_json(self, setup: _Setup, candle: Candle, *,
                   live_quote: bool = False) -> str:
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
            "entry_trigger": "M5_CISD",
            "cisd_timeframe": CISD_TIMEFRAME,
            "cisd_candle_time_ny": setup.confirmation.t_ny.isoformat(),
            "cisd_confirmed_at_ny": tu.utc_to_ny(
                setup.confirmation.t_utc + timedelta(minutes=5)).isoformat(),
            "entry_basis": "live_quote" if live_quote else "historical_m5_close",
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
            f"  M15 purge candle: {tu.format_ny_stamp(setup.purge_time_ny)} NY  "
            f"H {self._fmt(setup.purge_high)} / L {self._fmt(setup.purge_low)}\n"
            f"  Session: {window.label}\n"
            f"  Expected: {setup.direction.upper()} after closed M5 CISD "
            f"(M15-anchored SL {self._fmt(setup.sl_price)})")

    def _log_entry_block(self, setup: _Setup, candle: Candle, entry: float,
                         sl: float, tp: float, target: Level, rr: float,
                         score: float) -> None:
        anchor = "high" if setup.direction == "sell" else "low"
        self._log(
            f"ENTRY — {setup.direction.upper()} {self._fmt(entry)} at "
            f"{tu.format_ny_stamp(candle.t_ny)} NY\n"
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
    "WAITING_FOR_CISD", "CISD_CONFIRMED", "CISD_TIMEFRAME", "ENTRY_TRIGGERED", "EXPIRED",
    "INVALIDATED", "TRADE_CONFIRMED", "BUYSIDE", "SELLSIDE",
    "is_model_2", "model_label", "model_meta",
]
