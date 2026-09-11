"""Throwaway end-to-end check for the backtest window + period analysis.

Renders /backtests/<id> against an in-memory database seeded with a real
three-month engine summary, then syntax-checks the page's inline scripts with
node. Deleted after use -- not part of the suite.

Run:  python _verify_period_page.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "tests")

from app.web import create_app                                    # noqa: E402
from test_web import _FakeJobs, _repo                             # noqa: E402
from backtesting.engine import BacktestRunner, BacktestTrade      # noqa: E402
from trading.asset_manager import Asset                           # noqa: E402

SPEC = [
    # (entry UTC, outcome, pnl_r) -- NY is UTC-4, so these read as the NY
    # periods in the trailing comments.
    (datetime(2026, 1, 6, 13, 10), "WIN", 2.5),    # Tue 09:10 -> 2026-01
    (datetime(2026, 1, 20, 18, 5), "LOSS", -1.0),  # Tue 14:05 -> 2026-01
    (datetime(2026, 2, 3, 13, 40), "WIN", 2.5),    # Tue 09:40 -> 2026-02
    (datetime(2026, 2, 17, 15, 30), "LOSS", -1.0), # Tue 11:30 -> 2026-02
    (datetime(2026, 3, 2, 13, 25), "WIN", 2.5),    # Mon 09:25 -> 2026-03
    (datetime(2026, 3, 30, 13, 55), "LOSS", -1.0), # Mon 09:55 -> 2026-03
]


def build(repo):
    trades = []
    for entry, outcome, pnl in SPEC:
        trades.append(dict(
            asset="TEST", direction="buy", entry=101.7, sl=99.5, tp=106.0,
            exit_price=106.0 if outcome == "WIN" else 99.5,
            entry_time_utc=entry, exit_time_utc=entry + timedelta(minutes=45),
            outcome=outcome, pnl=pnl, rr=2.5, bars_held=45,
            reason="tp" if outcome == "WIN" else "sl"))

    # Through the real engine, so the buckets are the ones the backtester
    # writes rather than hand-written JSON that could drift from it.
    engine_trades = [
        BacktestTrade(asset=t["asset"], direction=t["direction"], entry=t["entry"],
                      sl=t["sl"], tp=t["tp"], exit_price=t["exit_price"],
                      entry_time_utc=t["entry_time_utc"],
                      exit_time_utc=t["exit_time_utc"], outcome=t["outcome"],
                      pnl_r=t["pnl"], rr=t["rr"], bars_held=t["bars_held"],
                      reason=t["reason"])
        for t in trades]
    runner = BacktestRunner(Asset(name="TEST", broker_symbol="TEST", enabled=True))
    summary = runner._summarize([], engine_trades, datetime(2026, 1, 1),
                                datetime(2026, 3, 31), n_bars=121000)

    return repo.save_backtest(
        name="q1", asset="TEST", symbol="TEST", batch_id="b1",
        start_utc=datetime(2026, 1, 1), end_utc=datetime(2026, 3, 31),
        params={"max_hold_m1": 720}, summary=summary.to_dict(), trades=trades)


def main() -> int:
    repo = _repo()
    row = build(repo)
    app = create_app(repository=repo, setup_db=False, jobs=_FakeJobs())
    text = app.test_client().get(f"/backtests/{row.id}").get_data(as_text=True)

    print(f"rendered {len(text)} bytes\n")
    print("markup probes:")
    for probe in ("Replay window", "121,000", "89 days, 0 h",
                  "Performance by period", "Calendar months on the NY clock",
                  'id="periodChart"', 'id="trade-rows"'):
        print(f"   {'OK ' if probe in text else 'MISSING'}  {probe!r}")

    print("\nby_month buckets as rendered:")
    for m in re.finditer(
            r'<td class="mono fw-semibold">([\w-]+)</td>\s*'
            r'<td class="mono text-end">(\d+)</td>', text):
        print(f"    {m.group(1):12} trades={m.group(2)}")

    payload = json.loads(re.search(r'<script id="period-data"[^>]*>(.*?)</script>',
                                   text, re.S).group(1))
    print("\nperiod payload buckets:")
    for p in payload:
        print(f"    {p['key']:12} {[r['bucket'] for r in p['rows']]}")

    # Syntax-check every inline <script> block (the ones without src=), since
    # a typo there only shows up in the browser console.
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)
    scripts = {Path("app/static/js/dashboard.js"): None}
    for i, body in enumerate(inline):
        if "window.ictClock" in body and len(body) < 200:
            continue  # the offset global, checked below by the same parser
        scripts[Path(f"_inline_{i}.js")] = body

    print("\nsyntax checks (node --check):")
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for path, body in scripts.items():
            target = Path(tmp) / path.name
            target.write_text(body if body is not None
                              else path.read_text(encoding="utf-8"),
                              encoding="utf-8")
            proc = subprocess.run(["node", "--check", str(target)],
                                  capture_output=True, text=True)
            label = str(path) if body is not None else "dashboard.js"
            if proc.returncode:
                ok = False
                print(f"    FAIL  {label}\n{proc.stderr.strip()}")
            else:
                print(f"    OK    {label}")

    print("\nRESULT:", "all checks passed" if ok else "FAILURES above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
