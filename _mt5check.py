"""Independent read of MT5, bypassing every line of the bridge under test.

Deliberately raw MetaTrader5 rather than MT5Client.symbol_tick: if this probe
used the code being verified it would prove nothing about that code.
"""
import json, re, sys
import requests
import MetaTrader5 as mt5

BASE = "http://127.0.0.1:5000"
s = requests.Session()
r = s.get(BASE + "/login", timeout=10)
tok = re.search(r'name="_csrf" value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"username": "_verify",
                              "password": "_verify-PoolCheck-9f3a2b1c",
                              "_csrf": tok}, timeout=10)
api = {row["name"]: row for row in s.get(BASE + "/api/client/market",
                                        timeout=15).json()["rows"]}

if not mt5.initialize():
    print("mt5.initialize FAILED:", mt5.last_error()); sys.exit(1)
try:
    print(f"{'name':9} {'dig':>3} {'dig!':>4} {'pts':>5} {'pts!':>5} "
          f"{'bid_api':>10} {'bid_mt5':>10} {'ask_api':>10} {'ask_mt5':>10}  verdict")
    bad = []
    for name, row in api.items():
        sym = row["symbol"]
        t = mt5.symbol_info_tick(sym)
        i = mt5.symbol_info(sym)
        if t is None or i is None:
            print(f"{name:9} <no tick/info from MT5>")
            bad.append(name); continue
        d_ok = (row["digits"] == i.digits)
        p_ok = (row["spread_points"] == i.spread)
        bid_ok = (row["bid"] is not None
                  and abs(row["bid"] - t.bid) <= 10 ** -i.digits)
        ask_ok = (row["ask"] is not None
                  and abs(row["ask"] - t.ask) <= 10 ** -i.digits)
        v = "OK" if all((d_ok, p_ok, bid_ok, ask_ok)) else "MISMATCH"
        if v != "OK":
            bad.append(name)
        print(f"{name:9} {row['digits']:>3} {i.digits:>4} {str(row['spread_points']):>5} "
              f"{i.spread:>5} {str(row['bid']):>10} {t.bid:>10} "
              f"{str(row['ask']):>10} {t.ask:>10}  {v}")
    print("\nMISMATCHED:", bad or "none")
finally:
    mt5.shutdown()
