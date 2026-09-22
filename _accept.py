"""Acceptance tests 1, 3, 5, 7 — run against the live engine during asian_range."""
import re, sys, time
import requests
from database.repository import Repository
from trading import time_utils as tu

BASE = "http://127.0.0.1:5000"
s = requests.Session()
r = s.get(BASE + "/login", timeout=10)
tok = re.search(r'name="_csrf" value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"username": "_verify",
                              "password": "_verify-PoolCheck-9f3a2b1c",
                              "_csrf": tok}, timeout=10)

def market():
    return s.get(BASE + "/api/client/market", timeout=15).json()

# ---------------------------------------------------------------- test 1
print("========== (1) ENGINE ALIVE / SESSION STATE ==========")
row = Repository().load_engine_state()
m = market(); st = m["status"]
print(f"  engine_state: state={row.state!r} active={row.active!r} "
      f"activity={row.activity!r} next_open_utc={row.next_open_utc!r}")
print(f"  heartbeat age: {(tu.now_utc()-row.heartbeat_utc).total_seconds():.1f}s "
      f"(lease {row.lease_seconds}s)")
print(f"  API: scanner_running={st['scanner_running']!r} "
      f"scanner_active={st['scanner_active']!r} "
      f"engine_alive={st.get('engine_alive')!r} "
      f"engine_source={st.get('engine_source')!r} engine_pid={st.get('engine_pid')}")

# ---------------------------------------------------------------- test 3 leg A
print("\n========== (3a) engine_state row  ->  API JSON ==========")
api = {x["name"]: x for x in m["rows"]}
q = row.quotes or {}
mism = []
for name, quote in q.items():
    a = api.get(name)
    if a is None:
        mism.append(f"{name}: missing from API"); continue
    for k in ("bid", "ask", "spread", "spread_points"):
        if a[k] != quote[k]:
            mism.append(f"{name}.{k}: api={a[k]!r} db={quote[k]!r}")
print(f"  compared {len(q)} assets x 4 fields against the published row")
print(f"  mismatches: {mism or 'NONE — the API reproduces the DB row exactly'}")

# bid_label formatting
lab = []
for name, a in api.items():
    d = a["digits"]
    want = "" if a["bid"] is None else f"{a['bid']:.{d}f}"
    if a["bid_label"] != want:
        lab.append(f"{name}: label={a['bid_label']!r} expected={want!r}")
print(f"  bid_label formatted correctly for all rows: "
      f"{'YES' if not lab else lab}")

# ---------------------------------------------------------------- test 3 leg B
print("\n========== (3b) mt5.symbol_info_tick (3rd process) -> engine_state ==========")
import MetaTrader5 as mt5
if not mt5.initialize():
    print("  mt5.initialize FAILED:", mt5.last_error())
else:
    try:
        print(f"  {'name':9} {'db bid/ask':>21} {'mt5 bid/ask':>21} {'quote age':>10}  verdict")
        live = drift = stale = absent = 0
        for name, quote in q.items():
            sym = api[name]["symbol"]
            mt5.symbol_select(sym, True)
            t = mt5.symbol_info_tick(sym)
            if t is None or not t.bid:
                print(f"  {name:9} {quote['bid']!s:>21} {'<no tick>':>21} "
                      f"{'-':>10}  ABSENT at broker")
                absent += 1; continue
            age = (tu.now_utc() - tu.utc_epoch_to_naive(t.time)).total_seconds()
            tol = 2 * 10 ** -(api[name]["digits"] or 5)
            ok = abs(quote["bid"] - t.bid) <= tol and abs(quote["ask"] - t.ask) <= tol
            if age > 300:
                verdict, tag = "STALE FEED", stale
            elif ok:
                verdict, tag = "AGREES", live
            else:
                verdict, tag = f"DRIFT {quote['bid']-t.bid:+.5f}", drift
            if tag == stale: stale += 1
            elif tag == live: live += 1
            else: drift += 1
            print(f"  {name:9} {quote['bid']!s:>10}/{quote['ask']!s:<10} "
                  f"{t.bid!s:>10}/{t.ask!s:<10} {age/60:>8.1f}m  {verdict}")
        print(f"\n  agrees={live} drift={drift} stale={stale} absent={absent}")
    finally:
        mt5.shutdown()

# ---------------------------------------------------------------- test 7
print("\n========== (7) POLLING: two samples 15s apart ==========")
a = {x["name"]: x for x in market()["rows"]}
time.sleep(15)
b = {x["name"]: x for x in market()["rows"]}
changed = [(n, a[n]["bid"], b[n]["bid"]) for n in a
           if a[n]["bid"] != b[n]["bid"]]
print(f"  {len(changed)} of {len(a)} assets changed bid in 15s")
for n, x, y in changed[:6]:
    print(f"    {n:9} {x} -> {y}")
if not changed:
    print("  (no movement — thin Asian session; quote timestamps still advancing is"
          " the poll-liveness signal)")
adv = [(n, a[n]["quote_time_ny"], b[n]["quote_time_ny"]) for n in a
       if a[n]["quote_time_ny"] != b[n]["quote_time_ny"]]
print(f"  {len(adv)} assets advanced their quote timestamp")

# ---------------------------------------------------------------- test 5
print("\n========== (5) SETUPS ==========")
sp = s.get(BASE + "/api/client/setups", timeout=15)
print(f"  /api/client/setups -> HTTP {sp.status_code}")
sj = sp.json()
rows = sj.get("rows") or sj.get("setups") or []
print(f"  payload keys: {sorted(sj)[:8]}")
print(f"  rows: {len(rows)}")
for r_ in rows[:5]:
    print(f"    {r_}")
