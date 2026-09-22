"""Same independent read, but selecting each symbol into Market Watch first.

`symbol_info`/`symbol_info_tick` return None for a symbol the terminal is not
subscribed to, so the first pass reported two indices as absent when they may
simply have been unselected. `MT5Client.ensure_symbol` does this selection in
the real path, so omitting it made the probe less faithful, not more.
"""
import re, sys
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
    print("=== the two indices the first pass could not read ===")
    for name in ("GER40", "JP225"):
        row = api[name]
        sym = row["symbol"]
        sel = mt5.symbol_select(sym, True)
        i = mt5.symbol_info(sym)
        t = mt5.symbol_info_tick(sym)
        print(f"\n{name}: api symbol={sym!r} symbol_select={sel}")
        if i is None:
            print("  symbol_info -> None (not offered under this name)")
        else:
            print(f"  symbol_info: digits={i.digits} spread={i.spread} "
                  f"bid={i.bid} ask={i.ask} visible={i.visible}")
        if t is None:
            print("  symbol_info_tick -> None")
        else:
            print(f"  symbol_info_tick: bid={t.bid} ask={t.ask} last={t.last} "
                  f"time={t.time}")
        print(f"  API row: price={row['price']} bid={row['bid']} "
              f"ask={row['ask']} spread={row['spread']} "
              f"spread_points={row['spread_points']} digits={row['digits']}")
    print("\n=== every symbol, selected first ===")
    for name, row in api.items():
        sym = row["symbol"]
        mt5.symbol_select(sym, True)
        t = mt5.symbol_info_tick(sym)
        i = mt5.symbol_info(sym)
        if t is None or i is None:
            print(f"{name:9} {sym:9} STILL None from MT5")
        else:
            print(f"{name:9} {sym:9} broker bid/ask={t.bid}/{t.ask} "
                  f"digits={i.digits} spread={i.spread}")
finally:
    mt5.shutdown()
