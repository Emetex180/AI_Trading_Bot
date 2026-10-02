"""Screenshot a rendered snapshot at an exact viewport width.

Headless Chrome clamps its window to ~500px on Windows, so the page is loaded in
an iframe of the requested width instead — the page then sees that as its
viewport, at any width, including 320.

Run: python _shot.py <page> <width> <out.png> [height]
"""
import subprocess, sys, pathlib

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
D = pathlib.Path(__file__).resolve().parent

page, width, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
height = int(sys.argv[4]) if len(sys.argv) > 4 else 1600

frame = D / "snapshots" / f"_shot_{page}.html"
frame.write_text(
    "<!doctype html><html><head><meta charset='utf-8'><style>"
    "html,body{margin:0;background:#0B0F14}"
    f"iframe{{width:{width}px;height:{height}px;border:0;display:block}}"
    f"</style></head><body><iframe src='{page}.html'></iframe></body></html>",
    encoding="utf-8")
try:
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--allow-file-access-from-files",
         "--virtual-time-budget=5000",
         f"--window-size={max(width, 520)},{height}",
         f"--screenshot={D / out}", frame.as_uri()],
        capture_output=True, timeout=180)
finally:
    frame.unlink(missing_ok=True)
print(f"{out} ({width}px)")
