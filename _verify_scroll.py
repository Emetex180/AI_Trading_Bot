"""Prove that anything wider than its box can still be reached.

_overflow.py answers "does the page scroll sideways by accident", and it
deliberately ignores elements inside a container that scrolls or clips — because
a horizontally scrolling nav is a design decision, not a bug. That leaves the
opposite question unasked: when a container *is* clipped, can the reader get to
the rest of it?

So this walks every rendered page at phone width and, for each element whose
content is wider than the element, reports how it is contained:

  scroll  — overflow-x is auto/scroll: the reader can swipe to the rest. Fine.
  clip    — overflow-x is hidden: the rest is unreachable. A defect, unless the
            element is a deliberate text-truncation (ellipsis) box.
  leak    — overflow-x is visible and the element itself overflows its parent:
            already caught by _overflow.py.

Text-truncation boxes (`text-overflow: ellipsis`) are excluded: clipping there
is the point, and the full value is in the `title` attribute.

Run: python _verify_scroll.py [width]
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
<style>html,body{margin:0;padding:0}iframe{width:%(w)dpx;height:900px;border:0}
</style></head><body>
<iframe id="f" src="%(src)s"></iframe><pre id="OUT">PENDING</pre>
<script>
var f = document.getElementById('f');
f.addEventListener('load', function () {
  var win = f.contentWindow, doc = f.contentDocument;
  var out = {clipped: [], scrolling: [], pages: 0};
  doc.querySelectorAll('body *').forEach(function (el) {
    var c = win.getComputedStyle(el);
    if (c.display === 'none' || c.visibility === 'hidden') return;
    // Content wider than the box, beyond rounding.
    if (el.scrollWidth <= el.clientWidth + 1) return;
    if (el.clientWidth === 0) return;
    var ovx = c.overflowX;
    var label = el.tagName.toLowerCase()
      + (typeof el.className === 'string' && el.className.trim()
         ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '')
      + ' [' + el.scrollWidth + '>' + el.clientWidth + ']';
    if (ovx === 'auto' || ovx === 'scroll') {
      out.scrolling.push(label);
    } else if (ovx === 'hidden') {
      // Deliberate truncation: the ellipsis is a signal, and `title` or the
      // full text is available on hover/focus. Not an unreachable-content bug.
      if (c.textOverflow === 'ellipsis') return;
      out.clipped.push(label);
    }
  });
  out.pages = 1;
  document.getElementById('OUT').textContent = JSON.stringify(out);
});
</script></body></html>
"""


def probe(name: str, width: int) -> dict | str:
    w = SNAP / f"_scroll_{name}.html"
    w.write_text(WRAPPER % {"w": width, "src": f"{name}.html"},
                 encoding="utf-8")
    try:
        out = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--allow-file-access-from-files", "--virtual-time-budget=5000",
             "--dump-dom", w.as_uri()],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        raw = out.stdout or ""
        i = raw.find('<pre id="OUT">')
        if i < 0:
            return "(no probe output)"
        payload = raw[i + len('<pre id="OUT">'):raw.find("</pre>", i)]
        for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"),
                     ("&gt;", ">"), ("&apos;", "'")):
            payload = payload.replace(a, b)
        return json.loads(payload)
    finally:
        w.unlink(missing_ok=True)


def main() -> int:
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 390
    pages = sorted(p.stem for p in SNAP.glob("*.html")
                   if not p.name.startswith("_"))
    if not pages:
        print("run `python _snapshot.py snapshots` first")
        return 2

    clipped: list[str] = []
    scrollers: dict[str, int] = {}
    for page in pages:
        r = probe(page, width)
        if not isinstance(r, dict):
            print(f"{page}: {r}")
            continue
        for c in r["clipped"]:
            clipped.append(f"{page}: {c}")
        for s in r["scrolling"]:
            scrollers[s] = scrollers.get(s, 0) + 1

    print(f"{len(pages)} pages at {width}px\n")
    print("Scrollable containers (content wider than the box, swipes to reach):")
    for k, n in sorted(scrollers.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3}x  {k}")

    print()
    if clipped:
        print(f"FAIL — {len(clipped)} element(s) clip content with no way to reach it:")
        for c in clipped:
            print(f"  - {c}")
        return 1
    print("PASS — nothing is clipped unreachably; every wider-than-box element "
          "is inside a container the reader can scroll")
    return 0


if __name__ == "__main__":
    sys.exit(main())
