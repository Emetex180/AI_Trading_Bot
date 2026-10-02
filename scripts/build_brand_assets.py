"""One-off: derive web-ready 3rader brand assets from the supplied logo JPG.

The source is a wide JPG with a near-flat dark plate behind the wordmark. The
plate is #070B13-ish, a few levels off the app's #0B0F14, so serving the JPG
directly would draw a faint rectangle on every page. Here the plate is keyed
out to real transparency, so the mark sits on any dark surface.

Run once; the PNG/ICO outputs are committed. Not a runtime dependency.
"""
from PIL import Image

SRC = "app/static/image/3rader_Logo.jpg"
OUT = "app/static/image"

# Sampled from the plate. Uniform to within a level or two across the frame.
BG = (7, 11, 19)

# Below this the pixel is JPEG noise in the plate, not artwork. Rescaling from
# the threshold keeps the antialiased glyph edges smooth instead of biting a
# hard step out of them.
FLOOR = 0.055


def keyed(img):
    """Return RGBA with the dark plate removed, un-premultiplied."""
    img = img.convert("RGB")
    w, h = img.size
    src = img.load()
    out = Image.new("RGBA", (w, h))
    dst = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b = src[x, y]
            # How far each channel has climbed from the plate, as a fraction of
            # the room it had. The brightest channel decides: the blue glyph's
            # blue channel is at full scale even though red is near the plate.
            a = max(
                (r - BG[0]) / (255 - BG[0]),
                (g - BG[1]) / (255 - BG[1]),
                (b - BG[2]) / (255 - BG[2]),
            )
            if a <= FLOOR:
                dst[x, y] = (0, 0, 0, 0)
                continue
            if a < 1.0:
                a = (a - FLOOR) / (1.0 - FLOOR)
                # observed = fg*a + plate*(1-a)  ->  fg = (observed - plate*(1-a)) / a
                r = int(min(255, max(0, (r - BG[0] * (1 - a)) / a)))
                g = int(min(255, max(0, (g - BG[1] * (1 - a)) / a)))
                b = int(min(255, max(0, (b - BG[2] * (1 - a)) / a)))
            dst[x, y] = (r, g, b, int(round(a * 255)))
    return out


def trim(img, pad=2):
    box = img.getbbox()
    if not box:
        return img
    l, t, r, b = box
    l, t = max(0, l - pad), max(0, t - pad)
    r, b = min(img.width, r + pad), min(img.height, b + pad)
    return img.crop((l, t, r, b))


def main():
    logo = keyed(Image.open(SRC))

    # --- Wordmark: the whole lockup, as it appears in a header ---------------
    word = trim(logo, pad=2)
    # Native is 699x159 — already 2x for the ~350px widest slot. Downscale to a
    # sensible ceiling so the file stays small without softening on retina.
    if word.width > 700:
        word = word.resize((700, round(word.height * 700 / word.width)), Image.LANCZOS)
    word.save(f"{OUT}/3rader-logo.png", optimize=True)
    print("3rader-logo.png", word.size)

    # --- Mark: the angular "3" alone, for the favicon and tight spaces -------
    mark = logo.crop((416, 408, 572, 568))  # the blue glyph plus a hair of air
    mark = trim(mark, pad=1)
    side = max(mark.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(mark, ((side - mark.width) // 2, (side - mark.height) // 2))
    square.save(f"{OUT}/3rader-mark.png", optimize=True)
    print("3rader-mark.png", square.size)

    # --- Favicon: pad the mark on a plate so it does not touch the tab edge --
    def icon(size, plate=None):
        pad = round(size * 0.16)
        inner = size - pad * 2
        g = square.resize((inner, inner), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), plate or (0, 0, 0, 0))
        canvas.paste(g, (pad, pad), g)
        return canvas

    icon(48).save(
        f"{OUT}/favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48)],
        append_images=[icon(16), icon(32)],
    )
    print("favicon.ico")

    icon(32).save(f"{OUT}/favicon-32.png", optimize=True)
    # iOS ignores alpha on the touch icon and composites on black; give it the
    # brand plate instead so it reads as the app rather than a black tile.
    icon(180, plate=(11, 15, 20, 255)).save(f"{OUT}/apple-touch-icon.png", optimize=True)
    print("favicon-32.png, apple-touch-icon.png")


if __name__ == "__main__":
    main()
