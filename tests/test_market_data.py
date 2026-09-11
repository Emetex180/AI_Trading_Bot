"""Market data tests: range fetching, clock conversion, and history probing.

No MT5 anywhere — the ``MetaTrader5`` module reference inside
:mod:`trading.mt5_client` is replaced with a fake that serves a fixed bar store,
and :class:`~trading.market_data.MarketData` is driven through a fake client.

The conversion tests matter more than they look: ``copy_rates_range`` filters
bars on the **broker server** clock while the whole UI works on the **NY**
clock, so a wrong sign here would silently backtest the wrong window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import get_settings
from trading import time_utils as tu
from trading.market_data import MarketData, row_to_candle
from trading import mt5_client as mc

#: Broker clock ahead of UTC. NY is a fixed UTC-4, so NY -> broker is +6h.
OFFSET = 2.0

BASE_EPOCH = 1_700_000_000  # an arbitrary fixed instant; never "now"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeMT5:
    """A fixed store of ``n_bars`` M1 bars, indexed backwards from the newest."""

    def __init__(self, n_bars: int):
        self.n_bars = n_bars
        self.range_calls: list[tuple] = []

    # --- what MT5Client's connection checks need -------------------------- #
    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def last_error(self):
        return (0, "no error")

    def initialize(self, **_kw):
        return True

    def shutdown(self):
        return None

    # --- rates ------------------------------------------------------------ #
    @staticmethod
    def _row(index: int) -> tuple:
        return (BASE_EPOCH + index * 60, 100.0, 101.0, 99.0, 100.5, 1, 0, 0)

    def copy_rates_from_pos(self, symbol, timeframe, pos, count):
        """Newest-first indexing: ``pos`` counts back from the last bar."""
        if self.n_bars == 0 or pos >= self.n_bars:
            return None
        rows = []
        for step in range(count):
            index = self.n_bars - 1 - (pos + step)
            if index < 0:
                break
            rows.append(self._row(index))
        rows.reverse()  # ascending, like the real API
        return rows or None

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        self.range_calls.append((symbol, timeframe, date_from, date_to))
        return []  # "the broker holds no bars in this window"


class _FakeClient:
    """Stands in for :class:`~trading.mt5_client.MT5Client`."""

    def __init__(self, rows=None, n_bars: int = 0):
        self.rows = rows if rows is not None else []
        self.n_bars = n_bars
        self.range_args: list[tuple] = []
        self.pos_args: list[tuple] = []
        self.bounds_args: list[tuple] = []

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        self.range_args.append((symbol, timeframe, date_from, date_to))
        return list(self.rows)

    def copy_rates_from_pos(self, symbol, timeframe, pos, count):
        self.pos_args.append((symbol, timeframe, pos, count))
        return list(self.rows)

    def history_bar_count(self, symbol, timeframe, ceiling=5_000_000):
        self.bounds_args.append((symbol, timeframe))
        return self.n_bars


@pytest.fixture
def fake_mt5(monkeypatch):
    """Install a fake ``MetaTrader5`` module for the duration of one test."""
    def install(n_bars: int) -> _FakeMT5:
        fake = _FakeMT5(n_bars)
        monkeypatch.setattr(mc, "_mt5", fake)
        return fake
    return install


# --------------------------------------------------------------------------- #
# fetch_m1_range — clock conversion
# --------------------------------------------------------------------------- #
def test_fetch_m1_range_converts_utc_range_to_the_broker_clock():
    """The range handed to MT5 must be shifted onto the broker clock.

    NY 2024-01-01 00:00 is UTC 04:00, which is broker 06:00 with a +2 server.
    Passing the raw UTC values would ask for the wrong six hours of history.
    """
    client = _FakeClient()
    market = MarketData(client, OFFSET)

    start_utc = tu.ny_to_utc(datetime(2024, 1, 1, 0, 0))
    end_utc = tu.ny_to_utc(datetime(2024, 1, 1, 23, 59))
    market.fetch_m1_range("XAUUSDm", start_utc, end_utc)

    assert client.range_args == [
        ("XAUUSDm", "M1", datetime(2024, 1, 1, 6, 0), datetime(2024, 1, 2, 5, 59))
    ]


def test_fetch_m1_range_returns_candles_on_the_utc_clock():
    """A broker-clock row must come back as the matching UTC instant."""
    broker_time = datetime(2024, 1, 1, 6, 0)
    epoch = int(broker_time.timestamp())
    client = _FakeClient(rows=[(epoch, 100.0, 101.0, 99.0, 100.5, 1, 0, 0)])
    market = MarketData(client, OFFSET)

    candles = market.fetch_m1_range("XAUUSDm", datetime(2023, 12, 31),
                                    datetime(2024, 1, 5))

    assert len(candles) == 1
    # Row time is a *local* naive datetime by construction; what must hold is
    # that it is not treated as UTC directly.
    expected = tu.broker_to_utc(mc.row_time_to_server_naive(epoch), OFFSET)
    assert candles[0].t_utc == expected


def test_fetch_m1_range_keeps_a_closed_historical_bar():
    """A range ending well in the past has no forming bar to drop."""
    past = datetime(2020, 1, 1, 12, 0)
    epoch = int(past.timestamp())
    client = _FakeClient(rows=[(epoch, 100.0, 101.0, 99.0, 100.5, 1, 0, 0)])
    market = MarketData(client, OFFSET)

    candles = market.fetch_m1_range("XAUUSDm", datetime(2019, 12, 1),
                                    datetime(2020, 1, 2))

    assert len(candles) == 1


def test_fetch_m1_range_drops_the_forming_bar_when_the_range_reaches_now():
    """A range ending right now does contain the still-forming bar; drop it."""
    now_broker = tu.utc_to_broker(tu.now_utc(), OFFSET).replace(second=0, microsecond=0)
    epoch = int(now_broker.timestamp())
    older = int((now_broker - timedelta(minutes=5)).timestamp())
    client = _FakeClient(rows=[
        (older, 100.0, 101.0, 99.0, 100.5, 1, 0, 0),
        (epoch, 100.0, 101.0, 99.0, 100.5, 1, 0, 0),
    ])
    market = MarketData(client, OFFSET)

    candles = market.fetch_m1_range("XAUUSDm", tu.now_utc() - timedelta(days=1),
                                    tu.now_utc())

    assert len(candles) == 1  # the forming bar is gone, the closed one stays


def test_fetch_m1_range_returns_empty_when_the_broker_has_nothing():
    market = MarketData(_FakeClient(rows=[]), OFFSET)
    assert market.fetch_m1_range("XAUUSDm", datetime(2024, 1, 1),
                                 datetime(2024, 1, 2)) == []


# --------------------------------------------------------------------------- #
# probe_m1_bounds
# --------------------------------------------------------------------------- #
def test_probe_reports_no_history_for_an_empty_symbol():
    market = MarketData(_FakeClient(n_bars=0), OFFSET)
    bounds = market.probe_m1_bounds("XAUUSDm")
    assert bounds == {"symbol": "XAUUSDm", "n_bars": 0,
                      "oldest_utc": None, "newest_utc": None}


def test_probe_reports_the_bar_count_and_both_ends():
    client = _FakeClient(rows=[(BASE_EPOCH, 100.0, 101.0, 99.0, 100.5, 1, 0, 0)],
                         n_bars=1234)
    market = MarketData(client, OFFSET)

    bounds = market.probe_m1_bounds("XAUUSDm")

    assert bounds["n_bars"] == 1234
    assert bounds["oldest_utc"] is not None and bounds["newest_utc"] is not None
    # It must ask for the last bar by index, not assume a count of one.
    assert (("XAUUSDm", "M1", 1233, 1)) in client.pos_args


# --------------------------------------------------------------------------- #
# MT5Client.history_bar_count — the binary search
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n_bars", [1, 2, 3, 1000, 4097, 250_000])
def test_history_bar_count_finds_the_exact_bar_count(fake_mt5, n_bars):
    fake_mt5(n_bars)
    client = mc.MT5Client(get_settings())
    client.connect()

    assert client.history_bar_count("XAUUSDm", "M1") == n_bars


def test_history_bar_count_is_zero_when_there_is_no_history(fake_mt5):
    fake_mt5(0)
    client = mc.MT5Client(get_settings())
    client.connect()

    assert client.history_bar_count("XAUUSDm", "M1") == 0


def test_history_bar_count_respects_the_ceiling(fake_mt5):
    """A ceiling must cap the answer rather than loop forever."""
    fake_mt5(50_000)
    client = mc.MT5Client(get_settings())
    client.connect()

    assert client.history_bar_count("XAUUSDm", "M1", ceiling=100) == 100


def test_copy_rates_range_returns_empty_instead_of_raising(fake_mt5):
    """An empty range is a legitimate answer, not a failure."""
    fake_mt5(10)
    client = mc.MT5Client(get_settings())
    client.connect()

    assert client.copy_rates_range("XAUUSDm", "M1", datetime(2024, 1, 1),
                                   datetime(2024, 1, 2)) == []


def test_copy_rates_range_raises_when_mt5_reports_a_failure(fake_mt5):
    """``None`` is a hard failure and must stay distinguishable from 'no data'."""
    fake = fake_mt5(10)
    client = mc.MT5Client(get_settings())
    client.connect()
    fake.copy_rates_range = lambda *a, **kw: None

    with pytest.raises(mc.MT5Error):
        client.copy_rates_range("XAUUSDm", "M1", datetime(2024, 1, 1),
                                datetime(2024, 1, 2))


def test_copy_rates_from_pos_still_raises_on_empty(fake_mt5):
    """The public contract of the positional fetch is unchanged."""
    fake_mt5(0)
    client = mc.MT5Client(get_settings())
    client.connect()

    with pytest.raises(mc.MT5Error):
        client.copy_rates_from_pos("XAUUSDm", "M1", 0, 10)


# --------------------------------------------------------------------------- #
# row_to_candle
# --------------------------------------------------------------------------- #
def test_row_to_candle_uses_the_broker_offset():
    epoch = 1_700_000_000
    server_naive = mc.row_time_to_server_naive(epoch)
    candle = row_to_candle((epoch, 1.0, 2.0, 0.5, 1.5, 7, 0, 0), OFFSET)

    assert candle.t_utc == tu.broker_to_utc(server_naive, OFFSET)
    assert candle.t_ny == tu.utc_to_ny(candle.t_utc)
    assert (candle.open, candle.high, candle.low, candle.close) == (1.0, 2.0, 0.5, 1.5)
    assert candle.volume == 7
