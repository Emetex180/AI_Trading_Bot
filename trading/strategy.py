"""ICT strategy state machine (M1 driver, shared by live & backtest).

Pipeline (all decisions on *closed* candles, zero lookahead):

    H1 liquidity purge  ── choose CISD tf (M15 < 09:00 NY, else M5)
        └─ CISD confirmation on M15/M5
            └─ M1 FVG forms after CISD confirm
                └─ retracement into the FVG + valid session
                    └─ SL/TP/RR → deterministic + risk approval → Signal

An instance tracks one asset independently (multi-asset support). Feed closed
M1 candles with :meth:`feed`; approved :class:`Signal` objects come out. History
can be warmed without producing signals by calling ``warm()`` first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import Settings, get_settings

from . import cisd as cisd_mod
from . import sessions as sess
from . import time_utils as tu
from .asset_manager import Asset
from .fvg import FVG, classify, fvg_on_tail
from .indicators import atr
from .liquidity import (build_level_snapshot, detect_sweeps, nearest_target,
                        strongest_purge)
from .risk_manager import RiskManager
from .signal_engine import SetupCandidate, Signal, candidate_to_signal
from .stream import BarsStream

MIN_RR_DEFAULT = 1.5
CISD_THRESHOLD_HOUR_DEFAULT = 9
VALID_SESSIONS_DEFAULT = ["london_open", "ny_am", "london_close", "ny_pm", "power_hour"]
SWING_LOOKBACK_DEFAULT = 3
MIN_HISTORY_H1_DEFAULT = 24
MAX_CISD_CANDLES_DEFAULT = 8
FVG_WAIT_M1_DEFAULT = 90
RETRACE_WAIT_M1_DEFAULT = 90
SL_BUFFER_ATR_DEFAULT = 0.25
EQ_TOLERANCE_ATR_MULT = 0.2


# --------------------------------------------------------------------------- #
# Internal state records
# --------------------------------------------------------------------------- #
@dataclass
class _Premise:
    direction: str
    level_kind: str
    level_price: float
    purge_open_utc: datetime       # the H1 candle that performed the purge
    purge_time_ny: datetime
    extreme_price: float           # H1 candle low (buy) / high (sell)
    cisd_tf: str
    tf_len_at: int = 0             # len(CISD series) when the premise was born

    @property
    def session_ny_date(self) -> str:
        return self.purge_time_ny.strftime("%Y-%m-%d")


@dataclass
class _Confirm:
    direction: str
    level_kind: str
    level_price: float
    cisd_tf: str
    confirm_open_utc: datetime
    confirm_end_utc: datetime      # confirm candle close time
    confirm_time_ny: datetime
    extreme_price: float           # carried from premise for the SL
    purge_time_ny: datetime        # purge (H1) candle NY time
    m1_waited: int = 0


@dataclass
class _FvgWatch:
    direction: str
    fvg: FVG
    level_kind: str
    level_price: float
    extreme_price: float           # for the SL
    cisd_tf: str
    confirm_time_ny: datetime
    purge_time_ny: datetime
    m1_waited: int = 0


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class ICTStrategy:
    """Per-asset ICT engine."""

    def __init__(self, asset: Asset, *, settings: Settings | None = None,
                 server_offset_hours: float | None = None):
        cfg = settings or get_settings()
        self.asset = asset
        self.stream = BarsStream(server_offset_hours=server_offset_hours)

        def asset_setting(key: str, fallback):
            return asset.settings_value(key, fallback, cfg)

        self.valid_sessions: list[str] = [s.lower() for s in asset_setting(
            "valid_entry_sessions", cfg.valid_entry_sessions)]
        self.min_rr: float = float(asset_setting("min_rr", cfg.min_rr))
        self.cisd_threshold_hour: int = int(asset_setting("cisd_threshold_hour_ny",
                                                          cfg.cisd_threshold_hour_ny))
        self.sl_buffer_atr: float = float(asset_setting("sl_buffer_atr", cfg.sl_buffer_atr))
        self.swing_lookback: int = int(asset_setting("swing_lookback", SWING_LOOKBACK_DEFAULT))
        self.min_history_h1: int = int(asset_setting("min_history_h1", MIN_HISTORY_H1_DEFAULT))
        self.max_cisd_candles: int = int(asset_setting("max_cisd_candles", MAX_CISD_CANDLES_DEFAULT))
        self.fvg_wait_m1: int = int(asset_setting("fvg_wait_m1", FVG_WAIT_M1_DEFAULT))
        self.retrace_wait_m1: int = int(asset_setting("retrace_wait_m1", RETRACE_WAIT_M1_DEFAULT))
        risk_percent: float = float(asset_setting("risk_percent", cfg.risk_percent))

        self.risk = RiskManager(min_rr=self.min_rr, risk_percent=risk_percent)

        # Per-direction active episodes.
        self._premise: dict[str, _Premise] = {}
        self._confirm: dict[str, _Confirm] = {}
        self._fvg: dict[str, _FvgWatch] = {}

    # ------------------------------------------------------------------ #
    # Warm-up
    # ------------------------------------------------------------------ #
    def warm(self, candles: list) -> None:
        """Feed historical closed M1 candles without emitting signals."""
        for c in candles:
            self.stream.add(c)

    @property
    def warm_history_h1(self) -> int:
        return len(self.stream.h1())

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #
    def feed(self, candle) -> list[Signal]:
        """Process one newly *closed* M1 candle; return approved signals."""
        update = self.stream.add(candle)
        signals: list[Signal] = []

        if update.h1 is not None:
            signals += self._on_h1_closed(update.h1)

        # Every closed minute may confirm a pending premise, form an FVG, or
        # trigger a retracement entry — so advance unconditionally.
        signals += self._advance(candle)

        # Deduplicate anything emitted this bar (defensive).
        return _dedupe_signals(signals)

    # ------------------------------------------------------------------ #
    # H1 processing -> premises
    # ------------------------------------------------------------------ #
    def _on_h1_closed(self, h1_candle) -> list[Signal]:
        h1_list = self.stream.h1()
        history = h1_list[:-1]                     # exclude the new H1 candle
        signals: list[Signal] = []
        if len(h1_list) < self.min_history_h1:
            return signals

        tol = self._eq_tolerance()
        levels = build_level_snapshot(history, as_of_date=h1_candle.t_ny.strftime("%Y-%m-%d"),
                                      lookback_swings=self.swing_lookback,
                                      swing_tolerance=tol)
        events = detect_sweeps(h1_candle, levels)

        bullish = strongest_purge(events, "bullish")
        bearish = strongest_purge(events, "bearish")
        if bullish and bearish:
            # Indecision candle: swept liquidity on both sides — skip.
            return signals

        if bullish:
            self._reset_episode("buy")
            self._premise["buy"] = self._make_premise("buy", bullish, h1_candle)
        elif bearish:
            self._reset_episode("sell")
            self._premise["sell"] = self._make_premise("sell", bearish, h1_candle)
        return signals

    def _make_premise(self, direction, sweep, h1_candle) -> _Premise:
        tf = cisd_mod.choose_timeframe(h1_candle.t_ny, self.cisd_threshold_hour)
        series = self._tf_series(tf)
        return _Premise(
            direction=direction,
            level_kind=sweep.level.kind,
            level_price=sweep.level.price,
            purge_open_utc=h1_candle.t_utc,
            purge_time_ny=h1_candle.t_ny,
            extreme_price=h1_candle.low if direction == "buy" else h1_candle.high,
            cisd_tf=tf,
            tf_len_at=len(series),
        )

    def _reset_episode(self, direction: str) -> None:
        self._premise.pop(direction, None)
        self._confirm.pop(direction, None)
        self._fvg.pop(direction, None)

    # ------------------------------------------------------------------ #
    # Per-minute advance
    # ------------------------------------------------------------------ #
    def _advance(self, candle) -> list[Signal]:
        signals: list[Signal] = []
        for direction in ("buy", "sell"):
            signals += self._try_confirm(direction)
            signals += self._try_fvg(direction, candle)
        return signals

    def _try_confirm(self, direction: str) -> list[Signal]:
        premise = self._premise.get(direction)
        if premise is None:
            return []
        series = self._tf_series(premise.cisd_tf)

        # Expire a premise that never confirmed within enough CISD candles.
        if len(series) - premise.tf_len_at > self.max_cisd_candles:
            self._premise.pop(direction, None)
            return []

        confirm_candle = cisd_mod.confirm(
            series, premise.level_price, direction,
            after_time_utc=premise.purge_open_utc,
        )
        if confirm_candle is None:
            return []
        # Confirmed.
        period_min = {"M5": 5, "M15": 15}[premise.cisd_tf]
        confirm_end = confirm_candle.t_utc + timedelta(minutes=period_min)
        self._premise.pop(direction, None)
        self._confirm.pop(direction, None)   # one active confirmation per direction
        self._fvg.pop(direction, None)
        self._confirm[direction] = _Confirm(
            direction=direction,
            level_kind=premise.level_kind,
            level_price=premise.level_price,
            cisd_tf=premise.cisd_tf,
            confirm_open_utc=confirm_candle.t_utc,
            confirm_end_utc=confirm_end,
            confirm_time_ny=confirm_candle.t_ny,
            extreme_price=premise.extreme_price,
            purge_time_ny=premise.purge_time_ny,
        )
        return []

    def _try_fvg(self, direction: str, candle) -> list[Signal]:
        confirm = self._confirm.get(direction)
        if confirm is None:
            return []
        signals: list[Signal] = []

        # Expire if no FVG has appeared within the wait window.
        if confirm.m1_waited > self.fvg_wait_m1:
            self._confirm.pop(direction, None)
            return signals
        confirm.m1_waited += 1

        watch = self._fvg.get(direction)
        if watch is None:
            # FVG detection is expressed in gap direction ("bullish"/"bearish"),
            # not trade direction ("buy"/"sell").
            gap_dir = "bullish" if direction == "buy" else "bearish"
            formed = fvg_on_tail(self.stream.m1, direction=gap_dir,
                                 after_time_utc=confirm.confirm_end_utc)
            if formed is not None:
                self._fvg[direction] = _FvgWatch(
                    direction=direction, fvg=formed,
                    level_kind=confirm.level_kind, level_price=confirm.level_price,
                    extreme_price=confirm.extreme_price, cisd_tf=confirm.cisd_tf,
                    confirm_time_ny=confirm.confirm_time_ny,
                    purge_time_ny=confirm.purge_time_ny,
                )
                return signals

        # Check the current candle against an active FVG watch.
        watch = self._fvg.get(direction)
        if watch is None:
            return signals

        state = classify(candle, watch.fvg)
        if state == "invalidated":
            self._fvg.pop(direction, None)
            self._confirm.pop(direction, None)
            return signals

        if watch.m1_waited > self.retrace_wait_m1:
            self._fvg.pop(direction, None)
            self._confirm.pop(direction, None)
            return signals
        watch.m1_waited += 1

        if state == "retraced" and self._is_entry_candle(candle, watch):
            signal = self._build_entry_signal(candle, watch)
            if signal is not None:
                signals.append(signal)
                self._fvg.pop(direction, None)
                self._confirm.pop(direction, None)
        return signals

    # ------------------------------------------------------------------ #
    # Entry construction
    # ------------------------------------------------------------------ #
    def _is_entry_candle(self, candle, watch: _FvgWatch) -> bool:
        fvg = watch.fvg
        if watch.direction == "buy":
            return candle.close > candle.open and candle.close > fvg.midpoint \
                and candle.close > fvg.lower
        return candle.close < candle.open and candle.close < fvg.midpoint \
            and candle.close < fvg.upper

    def _build_entry_signal(self, candle, watch: _FvgWatch) -> Signal | None:
        direction = watch.direction
        entry = candle.close
        # SL: beyond the extreme that took liquidity, buffered by ATR of CISD tf.
        buffer = atr(self._tf_series(watch.cisd_tf), period=14) * self.sl_buffer_atr
        if direction == "buy":
            sl = watch.extreme_price - buffer
        else:
            sl = watch.extreme_price + buffer

        # TP: nearest draw-on-liquidity beyond entry, from *known* levels.
        target = nearest_target(entry, "buy" if direction == "buy" else "sell",
                                self._target_levels(candle))
        if target is None:
            return None
        tp = target.price
        if tp <= 0 or sl <= 0:
            return None

        ny = candle.t_ny
        minute = tu.minute_of_day(ny)
        cand = SetupCandidate(
            asset=self.asset.name,
            direction=direction,
            entry_price=entry,
            entry_time_utc=candle.t_utc,
            entry_time_ny=ny,
            sl=sl,
            tp=tp,
            session_keys=sess.active_session_keys(minute),
            session_primary=sess.primary_session(minute).key if sess.primary_session(minute) else "",
            silver_bullet=sess.active_silver_bullet_key(minute),
            macro=sess.active_macro_window_key(minute),
            liquidity_type=watch.level_kind,
            liquidity_price=watch.level_price,
            purge_time_ny=self._purge_time_ny(direction),
            cisd_tf=watch.cisd_tf,
            cisd_confirm_time_ny=watch.confirm_time_ny,
            fvg_direction=watch.fvg.direction,
            fvg_lower=watch.fvg.lower,
            fvg_upper=watch.fvg.upper,
            fvg_formation_time_ny=watch.fvg.formation_time_ny,
            structure_extreme_price=watch.extreme_price,
        )
        signal = candidate_to_signal(cand, self.valid_sessions, self.risk)
        return signal if signal.status == "APPROVED" else None

    def _purge_time_ny(self, direction: str) -> datetime | None:
        premise = self._premise.get(direction)
        if isinstance(premise, _Premise):
            return premise.purge_time_ny
        confirm = self._confirm.get(direction)
        if isinstance(confirm, _Confirm):
            return confirm.purge_time_ny
        watch = self._fvg.get(direction)
        return watch.purge_time_ny if isinstance(watch, _FvgWatch) else None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _tf_series(self, tf: str):
        return self.stream.tf(tf)

    def _eq_tolerance(self) -> float:
        a = atr(self.stream.h1(), period=14)
        return a * EQ_TOLERANCE_ATR_MULT if a > 0 else 0.0

    def _target_levels(self, candle):
        """Levels known *before* the current candle (no future data)."""
        h1 = self.stream.h1()
        levels = build_level_snapshot(h1, as_of_date=candle.t_ny.strftime("%Y-%m-%d"),
                                      lookback_swings=self.swing_lookback,
                                      swing_tolerance=self._eq_tolerance())
        return levels


def _dedupe_signals(signals: list[Signal]) -> list[Signal]:
    seen: set[str] = set()
    out: list[Signal] = []
    for s in signals:
        fp = s.fingerprint()
        if fp not in seen:
            seen.add(fp)
            out.append(s)
    return out
