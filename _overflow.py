"""Probe rendered snapshots for horizontal overflow at an exact viewport width.

Headless Chrome clamps its own window to ~500px on Windows, so --window-size
cannot produce a real 390px viewport. This loads each snapshot in an iframe of
exactly the requested width instead, which the page sees as its viewport, and
reports document scrollWidth against it plus the elements that cross the edge.

An element inside a container that scrolls on purpose (.sidebar-nav, .table-wrap)
is not page overflow, so its ancestors are consulted: if any ancestor clips or
scrolls horizontally, the element is not counted.

Run: python _overflow.py <width> [page ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
D = Path(__file__).resolve().parent
SNAP = D / "snapshots"

WRAPPER = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0}iframe{width:%(w)dpx;height:900px;border:0}</style>
</head><body>
<iframe id="f" src="%(src)s"></iframe>
<pre id="OUT">PENDING</pre>
<script>
var f = document.getElementById('f');
f.addEventListener('load', function () {
  try {
    var d = f.contentDocument.documentElement;
    var vw = f.contentDocument.documentElement.clientWidth;
    var bad = [];
    f.contentDocument.querySelectorAll('body *').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return;
      if (r.right <= vw + 1 && r.left >= -1) return;
      // Ignore anything living inside a container that scrolls/clips on purpose.
      var p = el.parentElement;
      while (p && p !== f.contentDocument.body) {
        var ov = getComputedStyle(p).overflowX;
        if (ov === 'auto' || ov === 'scroll' || ov === 'hidden') return;
        p = p.parentElement;
      }
      var cls = (typeof el.className === 'string' ? el.className : '')
                  .trim().split(/\\s+/).slice(0, 3).join('.');
      bad.push(el.tagName.toLowerCase() + (cls ? '.' + cls : '')
               + '@' + Math.round(r.left) + '..' + Math.round(r.right));
    });
    document.getElementById('OUT').textContent = JSON.stringify({
      scrollWidth: d.scrollWidth, clientWidth: vw,
      overflow: d.scrollWidth - vw, culprits: bad.slice(0, 12),
      total: bad.length
    });
  } catch (e) {
    document.getElementById('OUT').textContent = 'ERROR ' + e.message;
  }
});
</script></body></html>
"""


def probe(name: str, width: int) -> dict | str:
    wrapper = SNAP / f"_probe_{name}.html"
    wrapper.write_text(WRAPPER % {"w": width, "src": f"{name}.html"},
                       encoding="utf-8")
    try:
        out = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--allow-file-access-from-files", "--virtual-time-budget=6000",
             "--dump-dom", wrapper.as_uri()],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        raw = out.stdout or ""
        i = raw.find('<pre id="OUT">')
        if i < 0:
            return "(no probe output)"
        payload = raw[i + len('<pre id="OUT">'):raw.find("</pre>", i)]
        if payload.startswith("PENDING") or payload.startswith("ERROR"):
            return payload
        return json.loads(payload.replace("&quot;", '"').replace("&amp;", "&")
                          .replace("&lt;", "<").replace("&gt;", ">"))
    finally:
        wrapper.unlink(missing_ok=True)


def main() -> int:
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 390
    names = sys.argv[2:] or sorted(
        p.stem for p in SNAP.glob("*.html") if not p.name.startswith("_probe"))
    worst, worst_page = 0, ""
    for n in names:
        if not (SNAP / f"{n}.html").exists():
            print(f"?    {n:20} (missing)")
            continue
        r = probe(n, width)
        if not isinstance(r, dict):
            print(f"?    {n:20} {r}")
            continue
        over = r["overflow"]
        if over > worst:
            worst, worst_page = over, n
        flag = "OK  " if over <= 1 else "OVER"
        detail = " ".join(r["culprits"][:6])
        print(f"{flag} {n:20} vw={r['clientWidth']} over={over:+5d} "
              f"({r['total']}) {detail}")
    print(f"\nworst: {worst}px on {worst_page or '-'} at {width}px")
    return 0 if worst <= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
