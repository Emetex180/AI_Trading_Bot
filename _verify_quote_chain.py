"""MT5 -> scanner -> engine_state -> API -> displayed New York time.

The MT5 half is read with *raw* MetaTrader5 in a third process, so it does not
go through ``MT5Client.symbol_tick`` — the code under test.

Two things are checked per asset:

* the quote the API serves is the quote the broker is actually printing, and
* the New York time shown beside it is the New York time of *that tick*.

The broker's offset is measured here from the terminal as well, because this
process has no engine session to inherit it from: read without it, the epoch is
a broker wall clock and every comparison is out by the offset.
"""
from __future__ import annotations

import re
import sys
from datetime import timedelta

import requests
import MetaTrader5 as mt5

from database.repository import Repository
from trading import time_utils as tu
from trading.mt5_client import MT5Client

BASE = "http://127.0.0.1:5000"
USER, PASSWORD = "_verify", "_verify-PoolCheck-9f3a2b1c"


def api_client() -> requests.Session:
    s = requests.Session()
    r = s.get(BASE + "/login", timeout=10)
    token = re.search(r'name="_csrf" value="([^"]+)"', r.text).group(1)
    s.post(BASE + "/login",
           data={"username": USER, "password": PASSWORD, "_csrf": token},
           timeout=10)
    return s


def main() -> int:
    s = api_client()
    payload = s.get(BASE + "/api/client/market", timeout=20).json()
    status = payload["status"]
    rows = {r["name"]: r for r in payload["rows"]}

    row = Repository().load_engine_state()
    age = (tu.now_utc() - row.heartbeat_utc).total_seconds()
    print("==============================================================")
    print(" engine_state (written by the SCANNER process, pid %s)" % row.pid)
    print("   state=%s active=%s activity=%s heartbeat %.1fs ago"
          % (row.state, row.active, row.activity, age))
    print("   quotes published: %d   prices published: %d"
          % (len(row.quotes or {}), len(row.prices or {})))
    print("==============================================================")
    print(" API /api/client/market (served by the WEB process)")
    print("   engine_alive=%s engine_source=%r engine_pid=%s"
          % (status["engine_alive"], status["engine_source"],
             status["engine_pid"]))
    print("   scanner_running=%s scanner_state=%r scanner_active=%s"
          % (status["scanner_running"], status["scanner_state"],
             status["scanner_active"]))
    print("   mt5_connected=%s last_scan_ny=%r signals_session=%s"
          % (status["mt5_connected"], status["last_scan_ny"],
             status["signals_session"]))
    print("   ny clock=%s %s" % (status["ny_time"], status["ny_zone"]))

    if not mt5.initialize():
        print("mt5.initialize FAILED: %s" % (mt5.last_error(),))
        return 2
    try:
        client = MT5Client.__new__(MT5Client)
        client._connected = True
        offset, detail = client.discover_server_utc_offset_hours(list(rows))
        print("\n broker offset measured in THIS process: %s (%s)"
              % (offset, detail))
        if offset is None:
            print(" refusing to compare times without a measured offset")
            return 2

        print("\n%-8s %-9s %11s %11s %8s %4s  %-16s %-16s %s"
              % ("asset", "symbol", "bid(api)", "bid(live)", "delta_pts",
                 "age", "quote_ny(api)", "quote_ny(true)", "verdict"))
        agree = moved = closed = bad = 0
        for name, a in rows.items():
            symbol = a["symbol"]
            tick = mt5.symbol_info_tick(symbol)
            if tick is None or not tick.bid:
                continue
            points = 10 ** -(a["digits"] or 5)
            delta_pts = round((tick.bid - a["bid"]) / points) if a["bid"] else None
            # Raw MT5 epoch -> broker wall clock -> real UTC -> New York.
            true_utc = tu.broker_to_utc(tu.utc_epoch_to_naive(tick.time), offset)
            true_ny = tu.utc_to_ny(true_utc).strftime("%Y-%m-%d %H:%M")
            tick_age = (tu.now_utc() - true_utc).total_seconds() / 60.0
            time_ok = a["quote_time_ny"] == true_ny
            if tick_age > 20:
                verdict, tag = "MARKET CLOSED - tick is the close print", "closed"
            elif not time_ok:
                verdict, tag = "TIME MISMATCH", "bad"
            elif delta_pts == 0:
                verdict, tag = "AGREES (price + NY time)", "agree"
            else:
                verdict, tag = "AGREES on time; price moved %+d pt since the poll" \
                    % delta_pts, "moved"
            for k, v in (("agree", "agree"), ("moved", "moved"),
                         ("closed", "closed"), ("bad", "bad")):
                pass
            if tag == "agree":
                agree += 1
            elif tag == "moved":
                moved += 1
            elif tag == "closed":
                closed += 1
            else:
                bad += 1
            print("%-8s %-9s %11s %11s %8s %4.1fm  %-16s %-16s %s"
                  % (name, symbol, a["bid"], tick.bid, delta_pts, tick_age,
                     a["quote_time_ny"], true_ny, verdict))
        print("\n time+price agree=%d   time agrees (price moved)=%d   "
              "closed market=%d   MISMATCH=%d" % (agree, moved, closed, bad))
        print(" host UTC now=%s   host NY now=%s %s"
              % (tu.now_utc().strftime("%Y-%m-%d %H:%M:%S"),
                 tu.now_ny().strftime("%Y-%m-%d %H:%M:%S"), tu.ny_zone_abbr()))
        return 0 if bad == 0 else 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
