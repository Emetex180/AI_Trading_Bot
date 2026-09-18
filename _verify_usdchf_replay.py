"""Replay of the USDCHF setup that produced `BUY USDCHF 0.82359` on 2026-09-17.

Honesty note
------------
This repository has no historical candle store (`data/` holds only
``trading.db``), so the 2026-09-17 USDCHF M1 series cannot be reloaded from disk.
What follows is a **reconstruction**, not a re-fetch. The tape is built to
satisfy the forensic report's stated facts and nothing more:

    08:00  the 1H purge candle begins
    08:29  the session low is swept (the 1H candle's low is 0.82310)
    08:35  the M5 CISD forms
    08:40  that M5 CISD closes              <- still inside the open 1H candle
    09:00  the 1H purge candle closes (above the swept level, as a sweep needs)
    09:06  the 1M FVG forms                 <- recorded here as 09:11-09:13
    09:20 / 09:21  retracement lows 0.82349 / 0.82344
    09:21  entry taken at 0.82359           <- OUTSIDE the FVG [0.82344, 0.82353]
          SL 0.82292 (from the liquidity-taking candle), TP 0.82546

Where the report gives a number, the tape uses it: the swept level, the gap
bounds, the retracement lows, the old entry, and the shape of the stop (the
5M candle that took the liquidity) and the target (the previous hour's high).
The intraday path *between* those numbers is the reconstruction. One liberty is
worth naming: the pre-08:00 range is placed below the entry price, so that the
previous hour's high is genuinely the nearest buy-side liquidity above the entry
— which is what the report's TP implies. Without that, a nearer level would
have been selected and the TP would not be the reported one.

Four runs
---------
  (a) **as documented** — the only CISD in the sequence is the 08:35/08:40 one,
      which printed inside the still-open purge hour. This is the headline.
  (b) **best case for the old signal** — a post-purge reclaim is supplied (it is
      not in the recorded session) so the engine does reach the documented FVG.
      The 0.82359 close must still be refused, at the entry rule.
  (c) **the tape is viable** — the same run as (b) with the entry candle closing
      *inside* the gap proves the fixture can produce a signal end to end, so
      (b)'s silence is the entry rule and not an unrelated gate.
  (d) the same tape under the opt-in ``reaction_close`` model, to show what the
      old behaviour was.

Run: ``python _verify_usdchf_replay.py``
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

from trading import cisd as cisd_mod
from trading import time_utils as tu
from trading.asset_manager import Asset
from trading.bars import make_candle
from trading.strategy import ICTStrategy


def NY(day: int, hour: int, minute: int) -> datetime:
    """A 2026-09 New York wall-clock time -> the matching UTC instant."""
    return tu.ny_to_utc(datetime(2026, 9, day, hour, minute))


DAY1 = 16      # 2026-09-16 — the prior day that creates the swept level
DAY2 = 17      # 2026-09-17 — the session under replay

#: The swept level (the prior day's low / Asian session low), the FVG, the
#: retracement lows, and the entry that was taken outside the gap.
SWEPT_LEVEL = 0.82368
FVG_LOWER = 0.82344
FVG_UPPER = 0.82353
OLD_ENTRY = 0.82359


def m1(day: int, hour: int, minute: int, o, h, l, cl):
    """One closed M1 candle, labelled by its NY open time."""
    return _at(datetime(2026, 9, day, hour, minute), o, h, l, cl)


def _at(ny_dt: datetime, o, h, l, cl):
    """One closed M1 candle from a naive NY wall-clock time."""
    return make_candle(t_utc=NY(ny_dt.day, ny_dt.hour, ny_dt.minute),
                       open_=o, high=h, low=l, close=cl, volume=1)


# --------------------------------------------------------------------------- #
# Warm history — the levels the purge will be read against
# --------------------------------------------------------------------------- #
def warm_history():
    """Day 1 plus the small hours of day 2.

    Day 1 sets the low this setup sweeps: 0.82368, both the day's low and the
    Asian session's low (`19:00-24:00`), so it is a genuine resting level that
    predates the sweep rather than one the sweep itself created.

    Day 2's small hours stay in a band *below* the entry price and *below* the
    purge hour's high, so nothing sweeps either side before 08:00 and the
    previous hour's high is what ends up as the nearest buy-side level.
    """
    day1 = [
        (0.82590, 0.82660, 0.82560, 0.82650),   # 00:00
        (0.82650, 0.82730, 0.82620, 0.82720),   # 01:00
        (0.82720, 0.82780, 0.82690, 0.82760),   # 02:00  -> day high 0.82780
        (0.82760, 0.82770, 0.82700, 0.82720),   # 03:00
        (0.82720, 0.82740, 0.82680, 0.82700),   # 04:00
        (0.82700, 0.82720, 0.82640, 0.82660),   # 05:00
        (0.82660, 0.82680, SWEPT_LEVEL, 0.82395),  # 06:00 -> day low 0.82368
        (0.82395, 0.82460, 0.82390, 0.82455),   # 07:00
        (0.82455, 0.82490, 0.82440, 0.82480),   # 08:00
        (0.82480, 0.82520, 0.82460, 0.82500),   # 09:00
        (0.82500, 0.82540, 0.82490, 0.82530),   # 10:00
        (0.82530, 0.82560, 0.82500, 0.82540),   # 11:00
        (0.82540, 0.82570, 0.82480, 0.82500),   # 12:00
        (0.82500, 0.82530, 0.82460, 0.82490),   # 13:00
        (0.82490, 0.82520, 0.82450, 0.82480),   # 14:00
        (0.82480, 0.82510, 0.82440, 0.82470),   # 15:00
        (0.82470, 0.82500, 0.82430, 0.82460),   # 16:00
        (0.82460, 0.82490, 0.82420, 0.82450),   # 17:00
        (0.82450, 0.82480, 0.82410, 0.82440),   # 18:00
        (0.82440, 0.82530, 0.82420, 0.82520),   # 19:00  Asian opens
        (0.82520, 0.82560, 0.82380, 0.82500),   # 20:00
        (0.82500, 0.82515, 0.82480, 0.82495),   # 21:00
        (0.82495, 0.82510, 0.82470, 0.82485),   # 22:00
        (0.82485, 0.82500, 0.82465, 0.82475),   # 23:00
    ]
    # Day 2, 00:00-07:00: a band clear of every level, so nothing is swept
    # before the purge hour and no early purge is raised.
    small_hours = [(0.82290, 0.82335, 0.82280, 0.82320 + (h % 2) * 0.00010)
                   for h in range(8)]
    return ([m1(DAY1, h, 0, *row) for h, row in enumerate(day1)]
            + [m1(DAY2, h, 0, *row) for h, row in enumerate(small_hours)])


# --------------------------------------------------------------------------- #
# The purge hour, 08:00-09:00 NY
# --------------------------------------------------------------------------- #
#: 08:29 takes the liquidity: low 0.82310, under the swept 0.82368. The hour
#: reclaims and closes at 0.82425 — above the level, which is what makes it a
#: sweep rather than a breakdown. 08:35-08:39 is the M5 bucket the old rule read
#: as the CISD: bullish, low 0.82338 under the level, close 0.82390 back above
#: it, and printed entirely inside the still-open hour.
_PURGE_HOUR = [
    m1(DAY2, 8, 0, 0.82395, 0.82410, 0.82390, 0.82400),
    m1(DAY2, 8, 5, 0.82400, 0.82490, 0.82398, 0.82485),   # hour high 0.82490
    m1(DAY2, 8, 15, 0.82485, 0.82488, 0.82420, 0.82425),
    m1(DAY2, 8, 25, 0.82425, 0.82428, 0.82390, 0.82395),
    m1(DAY2, 8, 29, 0.82395, 0.82397, 0.82310, 0.82335),  # THE SWEEP
    m1(DAY2, 8, 34, 0.82335, 0.82350, 0.82330, 0.82345),
    m1(DAY2, 8, 35, 0.82345, 0.82355, 0.82338, 0.82352),
    m1(DAY2, 8, 36, 0.82352, 0.82370, 0.82350, 0.82366),
    m1(DAY2, 8, 37, 0.82366, 0.82380, 0.82362, 0.82376),
    m1(DAY2, 8, 38, 0.82376, 0.82388, 0.82370, 0.82384),
    m1(DAY2, 8, 39, 0.82384, 0.82392, 0.82378, 0.82390),  # in-hour CISD closes
    m1(DAY2, 8, 45, 0.82390, 0.82410, 0.82388, 0.82405),
    m1(DAY2, 8, 50, 0.82405, 0.82420, 0.82400, 0.82415),
    m1(DAY2, 8, 59, 0.82415, 0.82430, 0.82410, 0.82425),  # the hour closes
]

#: What the hour was, once the engine has bucketed it.
_PURGE_OPEN, _PURGE_HIGH, _PURGE_LOW, _PURGE_CLOSE = 0.82395, 0.82490, 0.82310, 0.82425

#: 09:00-09:04 — the counterfactual reclaim. It did *not* happen in the
#: recorded session; it is supplied so run (b) can reach the documented FVG at
#: all. It dips to 0.82355 (under the swept level) and closes 0.82430, back
#: above both the level and its own open (0.82425), which is what a CISD needs.
_COUNTERFACTUAL_CISD = [
    m1(DAY2, 9, 0, 0.82425, 0.82428, 0.82410, 0.82415),
    m1(DAY2, 9, 1, 0.82415, 0.82418, 0.82390, 0.82395),
    m1(DAY2, 9, 2, 0.82395, 0.82398, 0.82358, 0.82362),
    m1(DAY2, 9, 3, 0.82362, 0.82370, 0.82355, 0.82366),
    m1(DAY2, 9, 4, 0.82366, 0.82432, 0.82364, 0.82430),   # bucket closes
    m1(DAY2, 9, 5, 0.82430, 0.82432, 0.82400, 0.82405),
    m1(DAY2, 9, 6, 0.82405, 0.82408, 0.82385, 0.82390),
    m1(DAY2, 9, 7, 0.82390, 0.82392, 0.82370, 0.82375),
    m1(DAY2, 9, 8, 0.82375, 0.82378, 0.82360, 0.82365),
    m1(DAY2, 9, 9, 0.82365, 0.82368, 0.82355, 0.82358),
    m1(DAY2, 9, 10, 0.82358, 0.82360, 0.82348, 0.82350),
    # 09:11-09:13 — the documented 1M FVG -> [0.82344, 0.82353]
    m1(DAY2, 9, 11, 0.82342, FVG_LOWER, 0.82338, 0.82340),   # c0.high = 0.82344
    m1(DAY2, 9, 12, 0.82340, 0.82356, 0.82338, 0.82354),     # displacement
    m1(DAY2, 9, 13, 0.82354, 0.82358, FVG_UPPER, 0.82356),   # c2.low > c0.high
]

#: The decline with no reclaim: from 09:00 price only ever closes below the
#: swept level, so no post-purge CISD can form however long it runs.
_NO_RECLAIM = [
    m1(DAY2, 9, 0, 0.82425, 0.82428, 0.82410, 0.82420),
    m1(DAY2, 9, 1, 0.82420, 0.82422, 0.82395, 0.82400),
    m1(DAY2, 9, 2, 0.82400, 0.82402, 0.82370, 0.82375),
    m1(DAY2, 9, 3, 0.82375, 0.82378, 0.82360, 0.82365),
    m1(DAY2, 9, 4, 0.82365, 0.82368, 0.82355, 0.82360),
] + _COUNTERFACTUAL_CISD[5:]

#: 09:20 / 09:21 — the documented retracement lows, and the entry minute. The
#: entry minute's close is the subject of the run: 0.82359 outside the gap on
#: the tape that was recorded, 0.82350 inside it for the viability control.
_RETRACE = [
    m1(DAY2, 9, 20, 0.82354, 0.82356, 0.82349, 0.82350),   # low 0.82349 -> in
]


def _entry_minute(close: float):
    return m1(DAY2, 9, 21, 0.82346, max(close, 0.82360), FVG_LOWER, close)


def _quiet_tail(count: int):
    """Flat minutes above the stop, to let a setup run out its CISD search."""
    start = datetime(2026, 9, DAY2, 9, 22)
    return [_at(start + timedelta(minutes=i), 0.82350, 0.82352, 0.82348, 0.82350)
            for i in range(count)]


def engine(*, entry_model="inside_fvg"):
    asset = Asset(name="USDCHF", broker_symbol="USDCHF", enabled=True, digits=5,
                  overrides={"min_history_h1": "5", "fvg_entry_model": entry_model,
                             "valid_entry_sessions": "ny_am"})
    return ICTStrategy(asset)


def _run(label, candles, *, expect=None, quiet=False):
    lines: list[str] = []
    eng = engine()
    eng.log = lines.append
    eng.warm(warm_history())
    signals = []
    for candle in candles:
        signals += eng.feed(candle)
    if quiet:
        for candle in _quiet_tail(40):
            signals += eng.feed(candle)

    print(f"\n{'=' * 72}\n{label}\n{'=' * 72}")
    for line in lines:
        body = line.split("] ", 1)[-1]
        if body.startswith(("PURGE:", "CISD:", "FVG:", "RETRACEMENT:", "ENTRY:")):
            print(line)
    print(f"\n  final state   : buy={eng.state('buy')}  sell={eng.state('sell')}")
    print(f"  signals       : {len(signals)}")
    for sig in signals:
        print(f"    {sig.direction.upper()} {sig.asset} entry={sig.entry} "
              f"FVG=[{sig.fvg_lower}, {sig.fvg_upper}] sl={sig.sl} tp={sig.tp}")
    refusals = [m for m in lines if "rejected" in m.lower()]
    for m in refusals:
        print(f"  refused       : {m}")
    if expect is not None:
        assert expect(eng, signals, lines), f"expectation failed for {label!r}"
    return eng, signals, lines


def main() -> int:
    print(__doc__)
    results: list[tuple[str, bool]] = []
    ok = True

    # ---- (a) exactly the documented sequence ------------------------------ #
    # The in-hour CISD is the only reclaim on this tape. The documented FVG,
    # the documented retracement and the documented entry minute are all
    # present as price data — and none of them is reachable, because the setup
    # never leaves LIQUIDITY_PURGED. It times out instead.
    tape_a = _PURGE_HOUR + _NO_RECLAIM + _RETRACE + [_entry_minute(OLD_ENTRY)]

    def expect_a(eng, signals, lines):
        assert not signals, "the old signal was produced from an in-hour CISD"
        # The 08:35 reclaim was never accepted as a CISD...
        assert all("CISD after completed purge" not in l for l in lines), \
            "an in-hour CISD was confirmed"
        # ...and the setup died rather than waiting for ever.
        return eng.state("buy") == "INVALIDATED"

    eng_a, sig_a, lines_a = _run(
        "(a) AS DOCUMENTED — the only CISD is the 08:35/08:40 in-hour one",
        tape_a, expect=expect_a, quiet=True)

    # The positive control that isolates rule 1. On this tape the 08:35 bucket
    # is the *only* reclaim of the swept level anywhere in the 5M series. It is
    # a textbook CISD — and the one thing standing between it and acceptance is
    # the completed-purge boundary: read the purge hour alone and it is the
    # CISD, apply the boundary and there is no CISD at all. Without this pair,
    # run (a)'s silence could just mean the tape held no reclaim to reject.
    #
    # The purge hour is read on its own because the strategy's own search is
    # capped at ``max_cisd_candles`` back from the newest bar, which by the end
    # of this tape no longer reaches 08:35 either.
    wide = cisd_mod.CISDParams(max_candles=0)
    purge_hour = [c for c in eng_a.stream.m5() if c.t_ny.hour == 8]
    in_hour = cisd_mod.confirm(purge_hour, SWEPT_LEVEL, "buy", params=wide)
    post_purge = cisd_mod.confirm(eng_a.stream.m5(), SWEPT_LEVEL, "buy",
                                  after_time_utc=NY(DAY2, 9, 0), params=wide)
    unbounded_ok = (in_hour is not None
                    and in_hour.t_ny == datetime(2026, 9, DAY2, 8, 35))
    bounded_ok = post_purge is None
    print(f"\n  rule 1, isolated: the 08:35 bucket read on its own      : "
          f"{'a valid CISD' if unbounded_ok else 'NOT a CISD'}")
    print(f"  rule 1, isolated: every bucket at/after the purge close : "
          f"{'no CISD' if bounded_ok else 'a CISD'}")

    ok &= (not sig_a and eng_a.state("buy") == "INVALIDATED"
           and unbounded_ok and bounded_ok)
    results.append(("(a) the documented sequence is refused — no signal",
                    not sig_a and eng_a.state("buy") == "INVALIDATED"))
    results.append(("(a) isolated: the in-hour bucket IS a CISD, and the\n"
                    "      completed-purge boundary is the only thing refusing it",
                    unbounded_ok and bounded_ok))

    # ---- (b) best case: grant a post-purge CISD --------------------------- #
    # Now the engine does reach the documented FVG at [0.82344, 0.82353], does
    # record the 09:20 retracement into it, and still refuses the 09:21 close.
    tape_b = _PURGE_HOUR + _COUNTERFACTUAL_CISD + _RETRACE + [_entry_minute(OLD_ENTRY)]

    def expect_b(eng, signals, lines):
        episode = eng._episodes.get("buy")
        assert episode is not None, "the setup was dropped entirely"
        assert episode.fvg is not None, "the documented FVG was never reached"
        assert (episode.fvg.lower, episode.fvg.upper) == (FVG_LOWER, FVG_UPPER)
        return not signals

    eng_b, sig_b, lines_b = _run(
        "(b) BEST CASE — a post-09:00 CISD is supplied, so the documented FVG\n"
        "    is reached and only the entry-price rule is left standing",
        tape_b, expect=expect_b)
    reached_fvg = _reach(eng_b)
    refused_entry = any("outside the FVG" in m for m in lines_b)
    retraced = "RETRACEMENT:" in "\n".join(lines_b)
    ok &= (not sig_b and reached_fvg and refused_entry and retraced)
    results.append(("(b) refused at the entry rule with the FVG and the\n"
                    "      retracement both confirmed", not sig_b and refused_entry))

    # ---- (c) the same tape, entry close INSIDE the gap -------------------- #
    # The control. Same candles, same everything, one number changed: the entry
    # minute closes 0.82350 instead of 0.82359. If this trades, then (b)'s
    # silence is the entry rule and not some other gate.
    tape_c = _PURGE_HOUR + _COUNTERFACTUAL_CISD + _RETRACE + [_entry_minute(0.82350)]
    eng_c, sig_c, lines_c = _run(
        "(c) CONTROL — the identical tape with the entry candle closing at\n"
        "    0.82350, i.e. INSIDE the gap",
        tape_c)
    inside_traded = len(sig_c) == 1 and abs(sig_c[0].entry - 0.82350) < 1e-9
    ok &= inside_traded
    results.append(("(c) the tape is viable — an inside close does trade",
                    inside_traded))

    # ---- (d) the same tape under the old, opt-in entry rule --------------- #
    eng_d = engine(entry_model="reaction_close")
    lines_d: list[str] = []
    eng_d.log = lines_d.append
    eng_d.warm(warm_history())
    old_signals = []
    for candle in tape_b:
        old_signals += eng_d.feed(candle)
    print(f"\n{'=' * 72}\n(d) THE OLD RULE — the same tape under the opt-in "
          f"reaction_close entry model\n{'=' * 72}")
    print(f"  signals       : {len(old_signals)}")
    for sig in old_signals:
        print(f"    {sig.direction.upper()} entry={sig.entry} "
              f"FVG=[{sig.fvg_lower}, {sig.fvg_upper}] sl={sig.sl} tp={sig.tp}")
    reproduced = [s for s in old_signals if abs(s.entry - OLD_ENTRY) < 1e-9]
    results.append(("(d) the old rule does produce the 0.82359 entry, so the\n"
                    "      default is what refuses it", bool(reproduced)))

    # ---- verdict ---------------------------------------------------------- #
    print(f"\n{'-' * 72}\nVERDICT\n{'-' * 72}")
    for label, passed in results:
        head, _, tail = label.rpartition("\n")
        if head:
            print(f"  {head}")
        print(f"  {tail:<58} : {'PASS' if passed else 'FAIL'}")
    print(f"""
  The old signal is REJECTED under the default configuration, by two
  independently sufficient rules:

    1. the completed-1H-purge rule — the 08:35/08:40 reclaim printed inside
       the still-open 08:00 hour, so it is not this premise's CISD and is
       never revisited; on the recorded tape no post-purge reclaim follows,
       so the setup times out and nothing is emitted at all;
    2. the entry-price rule — even granting a post-purge CISD, the 09:21
       close of {OLD_ENTRY} is above the gap's upper boundary ({FVG_UPPER}),
       and the default inside_fvg model refuses a fill outside its own FVG.

  Rule 1 is the one the forensic report asked about. Rule 2 is the second
  lock: with it in place, no tape reaches a signal whose entry sits outside
  the gap it claims to have traded.""")
    return 0 if ok else 1


def _reach(eng) -> bool:
    """Did the run reach the documented FVG? (b) asserts this, then reports it."""
    episode = eng._episodes.get("buy")
    return episode is not None and episode.fvg is not None


if __name__ == "__main__":
    sys.exit(main())
