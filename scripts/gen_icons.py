"""Generate PWA icons for Class Action Scout."""
from pathlib import Path
from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "static"
STATIC.mkdir(exist_ok=True)


def make_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded indigo background
    radius = size // 5
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=(94, 106, 210, 255))

    W = (255, 255, 255, 255)
    t = max(2, size // 42)   # line thickness scales with icon size
    cx = size // 2

    # Vertical stem
    stem_top = size * 22 // 100
    stem_bot = size * 76 // 100
    d.rectangle([cx - t, stem_top, cx + t, stem_bot], fill=W)

    # Horizontal balance beam at ~38% height
    bx0 = size * 17 // 100
    bx1 = size * 83 // 100
    by  = size * 38 // 100
    d.rectangle([bx0, by - t, bx1, by + t], fill=W)

    # Pivot dot at top of stem
    pr = t * 2
    d.ellipse([cx - pr, stem_top - pr, cx + pr, stem_top + pr], fill=W)

    # Base bar at bottom of stem
    base_w = size * 28 // 100
    d.rectangle([cx - base_w, stem_bot - t, cx + base_w, stem_bot + t], fill=W)

    # Pans: V-shaped chain + horizontal bar
    pan_depth = size * 20 // 100
    pan_hw    = size * 12 // 100

    for bx in (bx0, bx1):
        pan_y = by + pan_depth
        d.line([(bx, by + t), (bx - pan_hw, pan_y)], fill=W, width=t)
        d.line([(bx, by + t), (bx + pan_hw, pan_y)], fill=W, width=t)
        d.rectangle([bx - pan_hw - t, pan_y, bx + pan_hw + t, pan_y + t * 2], fill=W)

    return img


if __name__ == "__main__":
    for sz, name in ((192, "icon-192.png"), (512, "icon-512.png")):
        make_icon(sz).save(STATIC / name, "PNG")
        print(f"Generated {STATIC / name}")
