"""Broker clock discovery: MT5 -> UTC -> New York.

The broker's offset ahead of UTC is *measured* from the terminal rather than
assumed, so these tests pin the measurement — including the trap that makes it
hard, which is that a closed market reports its last tick's clock, not now. An
implementation that skipped the staleness check would happily "discover" a
Sunday-evening offset of minus several hours and shift every session window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trading import time_utils as tu
from trading import mt5_client as mc

UTC = tu._UTC
EPOCH = datetime(1970, 1, 1)


def broker_epoch(server_naive: datetime) -> int:
    """The MT5 epoch for a broker wall-clock time (wall clock read as UTC)."""
    return int((server_naive - EPOCH).total_seconds())


class _FakeMT5:
    """Just enough of the MetaTrader5 module to probe a broker clock."""

    def __init__(self, *, offset_hours: float, symbols, bar_age_minutes: float = 0.0,
                 symbols_reporting: int | None = None, connected: bool = True,
                 bid: float | None = None, ask: float | None = None,
                 spread_points: int | None = None, digits: int = 5):
        self.offset_hours = offset_hours
        self.symbols = list(symbols)
        self.bar_age_minutes = bar_age_minutes
        self.symbols_reporting = symbols_reporting
        self.connected = connected
        self.selected: list[str] = []
        # Quote fields. ``None`` means "the terminal did not report this", which
        # is deliberately different from a reported zero.
        self.bid = bid
        self.ask = ask
        self.spread_points = spread_points
        self.digits = digits

    def _broker_now(self) -> datetime:
        return tu.now_utc() + timedelta(hours=self.offset_hours)

    def terminal_info(self):
        return SimpleNamespace(connected=self.connected)

    def symbols_get(self, *_a):
        return [SimpleNamespace(name=s) for s in self.symbols]

    def symbol_select(self, symbol, _enable):
        self.selected.append(symbol)
        return True

    def symbol_info(self, _symbol):
        return SimpleNamespace(spread=self.spread_points, digits=self.digits)

    def symbol_info_tick(self, symbol):
        if self.symbols_reporting is not None:
            if self.symbols.index(symbol) >= self.symbols_reporting:
                return None
        # A tick is stamped in broker time, so a symbol quoting right now
        # reports the broker's current wall clock.
        return SimpleNamespace(time=broker_epoch(self._broker_now()),
                               bid=self.bid, ask=self.ask)

    def copy_rates_from_pos(self, symbol, _timeframe, _pos, _count):
        # The newest M1 bar opened `bar_age_minutes` ago, in broker time.
        opened = self._broker_now() - timedelta(minutes=self.bar_age_minutes)
        opened = opened.replace(second=0, microsecond=0)
        return [(broker_epoch(opened), 1.0, 1.0, 1.0, 1.0, 1, 0, 0)]


def _client(fake) -> mc.MT5Client:
    """A connected client pointed at the fake module, with clean global state."""
    client = mc.MT5Client.__new__(mc.MT5Client)
    client.settings = SimpleNamespace(mt5_terminal_path="", mt5_login=None,
                                      mt5_password=None, mt5_server=None)
    client._connected = True
    return client


@pytest.fixture(autouse=True)
def _clean_offset_state():
    tu.clear_server_utc_offset()
    yield
    tu.clear_server_utc_offset()


def _install(monkeypatch, fake):
    monkeypatch.setattr(mc, "_mt5", fake)
    return _client(fake)


# --------------------------------------------------------------------------- #
# The measurement itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("offset", [2.0, 3.0, 1.0, 0.0, -4.0, -5.0, 5.5])
def test_discovers_the_offset_the_terminal_reports(monkeypatch, offset):
    """Whatever the broker's clock says, that is what is discovered.

    Swept across the offsets real brokers actually run — no branch of the code
    knows any of these numbers in advance.
    """
    fake = _FakeMT5(offset_hours=offset, symbols=["EURUSD", "USDCAD"])
    client = _install(monkeypatch, fake)

    discovered, detail = client.discover_server_utc_offset_hours(["EURUSD", "USDCAD"])

    assert discovered == offset
    assert "discovered" in detail


def test_discovers_a_summer_offset_that_differs_from_winter(monkeypatch):
    """A broker that shifts its own clock must be re-measured, not remembered."""
    fake = _FakeMT5(offset_hours=3.0, symbols=["EURUSD", "USDCAD"])
    client = _install(monkeypatch, fake)
    assert client.discover_server_utc_offset_hours(["EURUSD", "USDCAD"])[0] == 3.0

    # Same terminal, the broker's DST has since moved it back an hour.
    fake.offset_hours = 2.0
    assert client.discover_server_utc_offset_hours(["EURUSD", "USDCAD"])[0] == 2.0


def test_no_assumed_offset_when_the_market_is_closed(monkeypatch):
    """A weekend clock reading must be rejected, not mistaken for an offset.

    The last tick before a close sits frozen at the close instant, so the
    difference against real UTC keeps growing. Every value in that range looks
    like a plausible offset, which is precisely why it cannot be trusted.
    """
    for hours_closed in (6, 12, 48):
        fake = _FakeMT5(offset_hours=2.0, symbols=["EURUSD", "USDCAD"],
                        bar_age_minutes=hours_closed * 60)
        client = _install(monkeypatch, fake)

        discovered, detail = client.discover_server_utc_offset_hours(["EURUSD", "USDCAD"])

        assert discovered is None, f"{hours_closed}h after close reported an offset"
        assert "stale" in detail


def test_a_single_reporting_symbol_is_not_enough(monkeypatch):
    """One symbol's clock could be a bad print; two agreeing is the bar."""
    fake = _FakeMT5(offset_hours=2.0, symbols=["EURUSD", "USDCAD"],
                    symbols_reporting=1)
    client = _install(monkeypatch, fake)

    discovered, detail = client.discover_server_utc_offset_hours(["EURUSD", "USDCAD"])

    assert discovered is None
    assert "agreement" in detail


def test_disagreeing_symbols_do_not_produce_a_guess(monkeypatch):
    """No majority -> no offset. Returning the mode would be an assumption."""
    fake = _FakeMT5(offset_hours=2.0, symbols=["EURUSD", "USDCAD", "XAUUSD"])

    original = fake.symbol_info_tick

    def skew(symbol):
        tick = original(symbol)
        if symbol == "XAUUSD":
            # XAUUSD's clock is 30 minutes off the others: a stale print.
            return SimpleNamespace(time=tick.time + 1800)
        return tick

    fake.symbol_info_tick = skew
    client = _install(monkeypatch, fake)

    discovered, _detail = client.discover_server_utc_offset_hours(
        ["EURUSD", "USDCAD", "XAUUSD"])

    # EURUSD/USDCAD agree on +2; the 30-minute outlier is snapped away by the
    # quarter-hour grid, so it votes for +2.5 and loses the majority.
    assert discovered == 2.0


def test_an_unreachable_terminal_reports_unknown(monkeypatch):
    """No terminal -> unknown, and the detail must say why."""
    client = _client(None)
    monkeypatch.setattr(mc, "_mt5",
                        _FakeMT5(offset_hours=2.0, symbols=[], connected=False))

    discovered, detail = client.discover_server_utc_offset_hours(["EURUSD"])

    assert discovered is None
    assert "not connected" in detail


# --------------------------------------------------------------------------- #
# Publication into the conversion layer
# --------------------------------------------------------------------------- #
def test_a_discovered_offset_reaches_the_conversions(monkeypatch):
    """Discovering is useless unless broker_to_utc starts using it."""
    fake = _FakeMT5(offset_hours=3.0, symbols=["EURUSD", "USDCAD"])
    client = _install(monkeypatch, fake)

    client.discover_and_publish_server_offset(["EURUSD", "USDCAD"])

    assert tu.server_utc_offset_hours() == 3.0
    assert tu.server_offset_source() == "discovered"
    # Broker 12:00 with a UTC+3 server is real UTC 09:00.
    assert tu.broker_to_utc(datetime(2026, 7, 15, 12, 0)) == \
        datetime(2026, 7, 15, 9, 0)
    # ...and NY 05:00 the same morning (EDT, UTC-4).
    assert tu.broker_to_ny(datetime(2026, 7, 15, 12, 0)) == \
        datetime(2026, 7, 15, 5, 0)


def test_a_failed_discovery_does_not_publish_a_guess(monkeypatch):
    fake = _FakeMT5(offset_hours=2.0, symbols=["EURUSD", "USDCAD"],
                    bar_age_minutes=600)
    client = _install(monkeypatch, fake)

    offset, _detail = client.discover_and_publish_server_offset(["EURUSD", "USDCAD"])

    assert offset is None
    assert tu.server_offset_source() == "unresolved"


def test_a_configured_pin_wins_and_the_disagreement_is_reported(monkeypatch):
    """An explicit ``MT5_SERVER_UTC_OFFSET`` is the operator's call, but loud."""
    fake = _FakeMT5(offset_hours=3.0, symbols=["EURUSD", "USDCAD"])
    client = _install(monkeypatch, fake)
    monkeypatch.setattr(tu, "configured_server_utc_offset", lambda: 2.0)

    offset, detail = client.discover_and_publish_server_offset(["EURUSD", "USDCAD"])

    assert offset == 3.0                 # the measurement is reported honestly
    assert "WARNING" in detail
    assert "disagrees" in detail
    assert tu.server_utc_offset_hours() == 2.0   # ...but the pin still governs


def test_an_unverified_offset_is_reported_as_such(monkeypatch):
    """Nothing configured, nothing discovered -> an ERROR, never silence."""
    monkeypatch.setattr(tu, "configured_server_utc_offset", lambda: None)

    warning = tu.warn_if_server_offset_unverified()

    assert warning is not None and "could NOT be verified" in warning
    # A discovered value silences it.
    tu.set_server_utc_offset(2.0, "discovered")
    assert tu.warn_if_server_offset_unverified() is None


# --------------------------------------------------------------------------- #
# Epoch parsing — the host-timezone leak
# --------------------------------------------------------------------------- #
def test_mt5_epochs_are_read_as_the_broker_wall_clock():
    assert mc.row_time_to_server_naive(broker_epoch(datetime(2026, 7, 15, 12, 0))) \
        == datetime(2026, 7, 15, 12, 0)


def test_the_full_chain_matches_across_both_dst_regimes():
    """MT5 -> UTC -> New York, for the same broker clock in winter and summer.

    The broker clock is identical in both cases; only New York's offset moves.
    A fixed UTC-4 for NY would make the winter row an hour wrong.
    """
    broker = datetime(2026, 1, 15, 14, 0)      # broker UTC+2, winter
    assert tu.broker_to_utc(broker, 2) == datetime(2026, 1, 15, 12, 0)
    assert tu.broker_to_ny(broker, 2) == datetime(2026, 1, 15, 7, 0)   # EST

    broker = datetime(2026, 7, 15, 14, 0)      # broker UTC+2, summer
    assert tu.broker_to_utc(broker, 2) == datetime(2026, 7, 15, 12, 0)
    assert tu.broker_to_ny(broker, 2) == datetime(2026, 7, 15, 8, 0)   # EDT


# --------------------------------------------------------------------------- #
# The quote timestamp — the same chain, on the live tick
#
# ``symbol_info_tick().time`` is stamped on the broker's server clock, exactly
# like a ``copy_rates`` row, so it has to take the same route to the dashboard:
# broker wall clock -> real UTC -> New York. Treated as though the epoch were
# already UTC, every quote the market table shows is out by the broker's whole
# offset — three hours on a UTC+3 server, which is what the dashboard's
# "Quote Time (NY)" was displaying.
# --------------------------------------------------------------------------- #
def _quoted(monkeypatch, *, offset, bid, ask, spread_points, digits=5,
            when=datetime(2026, 7, 15, 12, 0)):
    """A client quoting from a fake terminal whose clock is ``offset`` ahead."""
    monkeypatch.setattr(tu, "now_utc", lambda: when)
    fake = _FakeMT5(offset_hours=offset, symbols=["EURUSD"], bid=bid, ask=ask,
                    spread_points=spread_points, digits=digits)
    client = _install(monkeypatch, fake)
    # What the runner does once per terminal session, from the same probe these
    # tests exercise: the offset is measured, then published process-wide.
    tu.set_server_utc_offset(offset, "discovered")
    return client, fake


def test_a_quote_time_is_real_utc_not_the_brokers_wall_clock(monkeypatch):
    client, fake = _quoted(monkeypatch, offset=3.0, bid=1.09345, ask=1.09355,
                           spread_points=10)

    tick = client.symbol_tick("EURUSD")

    broker_wall = tu.utc_epoch_to_naive(fake.symbol_info_tick("EURUSD").time)
    assert broker_wall == datetime(2026, 7, 15, 15, 0)      # the server's clock
    assert tick["time_utc"] == datetime(2026, 7, 15, 12, 0)  # ...is not UTC
    # The regression itself: the timestamp is not the value the epoch encodes.
    assert tick["time_utc"] != broker_wall
    # ...and on the New York clock the dashboard renders, that is 08:00 EDT.
    assert tu.utc_to_ny(tick["time_utc"]) == datetime(2026, 7, 15, 8, 0)


def test_a_quote_time_matches_the_candle_pipeline_for_the_same_instant(monkeypatch):
    """One conversion, so a quote and the candle beside it cannot disagree.

    The market table shows both: the last closed M1 close the strategy acted on,
    and the live quote. Rendering them through different conversions is how a
    quote ends up dated hours away from the candle it sits next to.
    """
    client, fake = _quoted(monkeypatch, offset=2.0, bid=1.09345, ask=1.09355,
                           spread_points=10)
    epoch = fake.symbol_info_tick("EURUSD").time

    tick = client.symbol_tick("EURUSD")

    assert tick["time_utc"] == tu.broker_to_utc(tu.utc_epoch_to_naive(epoch))


def test_a_quote_time_is_dst_aware_on_the_new_york_side(monkeypatch):
    """Same broker clock, and New York is an hour apart across the switch."""
    winter, _ = _quoted(monkeypatch, offset=3.0, bid=1.09, ask=1.10,
                        spread_points=10, when=datetime(2026, 1, 15, 15, 0))
    winter_tick = winter.symbol_tick("EURUSD")

    summer, _ = _quoted(monkeypatch, offset=3.0, bid=1.09, ask=1.10,
                        spread_points=10, when=datetime(2026, 7, 15, 15, 0))
    summer_tick = summer.symbol_tick("EURUSD")

    # Broker 18:00 -> UTC 15:00 -> 10:00 EST in January, 11:00 EDT in July.
    assert winter_tick["time_utc"] == summer_tick["time_utc"].replace(month=1)
    assert tu.utc_to_ny(winter_tick["time_utc"]) == datetime(2026, 1, 15, 10, 0)
    assert tu.utc_to_ny(summer_tick["time_utc"]) == datetime(2026, 7, 15, 11, 0)


def test_a_quote_with_no_offset_verified_is_not_shifted_by_a_guess(monkeypatch):
    """Unverified means zero here, never an invented offset.

    The dashboard is still read-only in this state, and the live scanner
    refuses to scan at all on an unverified broker clock — so the honest
    reading is the one the terminal gave, flagged as such by the offset source.
    """
    monkeypatch.setattr(tu, "now_utc", lambda: datetime(2026, 7, 15, 12, 0))
    fake = _FakeMT5(offset_hours=0.0, symbols=["EURUSD"], bid=1.09345,
                    ask=1.09355, spread_points=10)
    client = _install(monkeypatch, fake)

    tick = client.symbol_tick("EURUSD")

    assert tick["time_utc"] == datetime(2026, 7, 15, 12, 0)
    assert tu.server_offset_source() == "unresolved"


# --------------------------------------------------------------------------- #
# Zero is a reading, not a missing value
# --------------------------------------------------------------------------- #
def test_a_zero_spread_is_reported_as_zero(monkeypatch):
    """``if points else None`` erased a genuine zero.

    A zero-spread account quotes bid == ask and the broker reports ``spread:
    0``. Rendering that as an em dash says "not read", which is a different and
    false statement about the market — and it is the one figure a reader checks
    before deciding a fill is cheap.
    """
    client, _ = _quoted(monkeypatch, offset=0.0, bid=1.10000, ask=1.10000,
                        spread_points=0)

    tick = client.symbol_tick("EURUSD")

    assert tick["spread"] == 0.0
    assert tick["spread_points"] == 0
    assert tick["bid"] == 1.10000
    assert tick["ask"] == 1.10000
    # Every one of them is present, not ``None``: that is the whole point.
    assert all(tick[k] is not None
               for k in ("bid", "ask", "spread", "spread_points"))


def test_a_crossed_quote_is_unknown_rather_than_a_negative_spread(monkeypatch):
    """A negative difference is impossible, so it is not reported as a number.

    The bid and ask themselves are still published — they were read, and a
    reader can see the crossed print for what it is.
    """
    client, _ = _quoted(monkeypatch, offset=0.0, bid=1.10010, ask=1.10000,
                        spread_points=0)

    tick = client.symbol_tick("EURUSD")

    assert tick["spread"] is None
    assert tick["bid"] == 1.10010 and tick["ask"] == 1.10000


def test_a_field_the_terminal_did_not_report_stays_unknown(monkeypatch):
    """The other direction: absent is still absent, never a fabricated zero."""
    client, _ = _quoted(monkeypatch, offset=0.0, bid=None, ask=None,
                        spread_points=None)

    tick = client.symbol_tick("EURUSD")

    assert tick["bid"] is None and tick["ask"] is None
    assert tick["spread"] is None and tick["spread_points"] is None
