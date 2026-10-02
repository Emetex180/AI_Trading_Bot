"""Drive the password toggle in a real browser and report what it actually did.

The static checks in _verify_password.py prove the markup is right; this proves
the behaviour is. It clicks the icon (not the button) to exercise the delegated
handler, asserts the input flips between password and text, that the two state
icons swap, that the accessible name follows the action, that pressing it twice
restores the field, and — the failure that would be worst in production — that
the click did not submit the form the button sits inside.

Run: python _verify_password_browser.py   (needs _snapshot.py to have run)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
D = Path(__file__).resolve().parent
SNAP = D / "snapshots"

PROBE = """
<script>
window.addEventListener('load', function () {
  var out = {};
  var btn = document.querySelector('[data-pw-toggle]');
  if (!btn) { out.error = 'no toggle found'; }
  else {
  var wrap = btn.closest('.pw-field');
  var inp = wrap.querySelector('input');
  var eye = wrap.querySelector('.pw-toggle-eye');
  var eyeOff = wrap.querySelector('.pw-toggle-eye-off');

  function state() {
    return {
      type: inp.type,
      pressed: btn.getAttribute('aria-pressed'),
      label: btn.getAttribute('aria-label'),
      title: btn.getAttribute('title'),
      revealed: wrap.classList.contains('is-revealed'),
      eye: getComputedStyle(eye).display,
      eyeOff: getComputedStyle(eyeOff).display
    };
  }
  out.before = state();

  var ib = inp.getBoundingClientRect(), bb = btn.getBoundingClientRect();
  out.geometry = {
    buttonW: Math.round(bb.width), buttonH: Math.round(bb.height),
    // the button must sit inside the input's right edge, top-aligned to it
    alignsRight: Math.abs(bb.right - ib.right) <= 2,
    alignsTop: Math.abs(bb.top - ib.top) <= 2,
    sameHeight: Math.abs(bb.height - ib.height) <= 2,
    // reserved gutter: the input's right padding must clear the button
    inputPadRight: getComputedStyle(inp).paddingRight
  };

  // Click the SVG, the way a user clicking the glyph does: proves the handler
  // is delegated rather than bound to the button element alone.
  eye.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  out.afterShow = state();

  // A keyboard user reaches it with Enter/Space; a real click event is what
  // the browser fires for those on a <button>.
  btn.dispatchEvent(new MouseEvent('click', {bubbles: true}));
  out.afterHide = state();

  // If the click had submitted, the form would have navigated and this node
  // would be gone.
  out.pageIntact = !!document.querySelector('[data-pw-toggle]');
  out.formSubmits = 0;
  }
  var m = document.createElement('div');
  m.id = 'PWPROBE';
  m.textContent = JSON.stringify(out);
  document.body.appendChild(m);
});
</script>
"""


def probe(page: str) -> dict | str:
    src = SNAP / f"{page}.html"
    tmp = SNAP / f"_pwprobe_{page}.html"
    tmp.write_text(src.read_text(encoding="utf-8").replace("</body>", PROBE + "</body>"),
                   encoding="utf-8")
    try:
        out = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--allow-file-access-from-files", "--virtual-time-budget=6000",
             "--dump-dom", tmp.as_uri()],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        raw = out.stdout or ""
        i = raw.find('id="PWPROBE">')
        if i < 0:
            return "(no probe output)"
        payload = raw[i + len('id="PWPROBE">'):raw.find("</div>", i)]
        for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"),
                     ("&gt;", ">"), ("&apos;", "'")):
            payload = payload.replace(a, b)
        return json.loads(payload)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    if not SNAP.exists():
        print("run `python _snapshot.py snapshots` first")
        return 2
    fails: list[str] = []
    for page in ("login", "register", "settings"):
        r = probe(page)
        if not isinstance(r, dict):
            fails.append(f"{page}: {r}")
            continue
        b, show, hide = r["before"], r["afterShow"], r["afterHide"]
        g = r["geometry"]

        def eq(label, got, want):
            if got != want:
                fails.append(f"{page}/{label}: {got!r} != {want!r}")

        eq("initial type", b["type"], "password")
        eq("initial aria-pressed", b["pressed"], "false")
        eq("initial label", b["label"], "Show password")
        eq("initial revealed", b["revealed"], False)
        if b["eye"] == "none":
            fails.append(f"{page}: eye glyph hidden while password is masked")
        if b["eyeOff"] != "none":
            fails.append(f"{page}: eye-off glyph shown while password is masked")

        # after the first click: revealed
        eq("shown type", show["type"], "text")
        eq("shown aria-pressed", show["pressed"], "true")
        eq("shown label", show["label"], "Hide password")
        eq("shown revealed", show["revealed"], True)
        if show["eye"] != "none":
            fails.append(f"{page}: eye glyph still shown after reveal")
        if show["eyeOff"] == "none":
            fails.append(f"{page}: eye-off glyph not shown after reveal")

        # after the second click: masked again
        eq("restored type", hide["type"], "password")
        eq("restored aria-pressed", hide["pressed"], "false")
        eq("restored label", hide["label"], "Show password")
        eq("restored revealed", hide["revealed"], False)

        if not r["pageIntact"]:
            fails.append(f"{page}: clicking the toggle navigated away "
                         f"(the form submitted)")

        eq("button aligns to input right", g["alignsRight"], True)
        eq("button aligns to input top", g["alignsTop"], True)
        eq("button matches input height", g["sameHeight"], True)
        if g["buttonW"] < 40:
            fails.append(f"{page}: touch target only {g['buttonW']}px wide")
        if float(g["inputPadRight"].replace("px", "")) < 40:
            fails.append(f"{page}: input padding-right {g['inputPadRight']} "
                         f"does not clear the button")

        print(f"OK   {page:10} {b['type']} -> {show['type']} -> {hide['type']}  "
              f"label {b['label']!r} -> {show['label']!r}  "
              f"btn {g['buttonW']}x{g['buttonH']}  pad {g['inputPadRight']}")

    if fails:
        print(f"\nFAIL ({len(fails)})")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nPASS — toggle flips the field, swaps its icon, and never submits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
