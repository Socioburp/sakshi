"""Render every template over the same copy, for eyeballing.

Design QA needs the layouts side by side over a background with real texture --
a flat gradient hides exactly the problems (thin type on busy areas, a logo
lost in the picture) that a photograph exposes. Nothing here touches the DB,
the queue or any vendor: it is the compositor and the templates only.

    python scripts/creative_preview.py            # writes out/preview_*.png
    python scripts/creative_preview.py --bg photo.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.creative.brief import EXAMPLE, CreativeBrief
from app.creative.compose import TEMPLATES, as_data_uri, compose

OUT = Path("out")


class Brand:
    """Stand-in for a brand row, with the fields the compositor reads."""

    name = "Kadamba Naturals"
    logo_url = None
    logo_src = None
    logo_analysis = {"has_wordmark": False}
    palette = {"primary": "#123B2E", "secondary": "#FFFFFF", "accent": "#E4572E",
               "ink": "#FFFFFF"}
    fonts = {"heading": "Poppins", "body": "Inter"}


def stand_in_logo(kind: str) -> bytes:
    """An emblem or a wordmark, so the lockup can be checked without a real brand.

    The two shapes are sized by different rules in the template, and that rule is
    exactly what this exists to exercise.
    """
    if kind == "wordmark":
        im = Image.new("RGBA", (900, 200), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 60, 900, 140], radius=12, fill=(255, 255, 255, 255))
        d.rectangle([40, 78, 300, 122], fill=(18, 59, 46, 255))
    else:
        im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse([10, 10, 390, 390], fill=(255, 255, 255, 255))
        d.ellipse([90, 90, 310, 310], fill=(18, 59, 46, 255))
        d.rectangle([180, 150, 220, 250], fill=(228, 87, 46, 255))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def busy_background(w: int, h: int, seed: int = 7) -> bytes:
    """A stand-in with photographic texture: soft colour masses plus grain.

    Not a photograph, and not pretending to be one. Its job is to have busy
    mid-tones and edges, so a headline that only survives over a flat gradient
    fails here instead of in a client's feed.
    """
    rnd = random.Random(seed)
    im = Image.new("RGB", (w, h), (86, 74, 58))
    d = ImageDraw.Draw(im)
    for i in range(26):
        r = rnd.randint(w // 10, w // 3)
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        tone = (rnd.randint(40, 210), rnd.randint(45, 190), rnd.randint(35, 150))
        d.ellipse([x - r, y - r, x + r, y + r], fill=tone)
        if i % 5 == 0:
            d.line([(0, y), (w, y + rnd.randint(-h // 6, h // 6))],
                   fill=tone, width=max(2, w // 90))
    im = im.filter(ImageFilter.GaussianBlur(radius=w / 55))
    px = im.load()
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            n = int(18 * math.sin(x * 0.7) * math.cos(y * 0.5)) + rnd.randint(-14, 14)
            r, g, b = px[x, y]
            px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)),
                        max(0, min(255, b + n)))
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", type=Path, help="a real photograph to composite over")
    ap.add_argument("--headline", default=None)
    ap.add_argument("--logo", choices=("none", "emblem", "wordmark"), default="emblem")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    payload = dict(EXAMPLE)
    if args.headline:
        payload["headline"] = args.headline

    brand = Brand()
    if args.logo != "none":
        brand.logo_src = as_data_uri(stand_in_logo(args.logo), "image/png")
        brand.logo_analysis = {"has_wordmark": args.logo == "wordmark"}

    for key in TEMPLATES:
        brief = CreativeBrief.model_validate({**payload, "template_id": key})
        slide = brief.units()[0]
        w, h = brief.pixel_size()
        bg = args.bg.read_bytes() if args.bg else busy_background(w, h)
        png = await compose(brief, slide, brand, bg)
        path = OUT / f"preview_{key}_{args.logo}.png"
        path.write_bytes(png)
        print(f"  {key:18s} -> {path} ({len(png) // 1024} KB, {w}x{h})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
