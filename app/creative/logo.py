"""Logo intake and analysis.

When the client sends their logo, two different things happen to it, and the
split matters:

* **The palette is measured, not guessed.** Colours come out of the actual
  pixels with Pillow. Asking a vision model "what colour is this logo" returns
  a plausible-sounding hex that is subtly wrong, and every creative afterwards
  is subtly off-brand. Counting pixels is deterministic, free, and correct.
* **The character is read by vision.** What the mark *is* -- a wordmark, an
  emblem, hand-drawn or geometric, premium or friendly -- is genuinely a
  judgement call, and that is what the model is for.

The result lands in `brands` (structured, loaded into every prompt) and in
`brand_memory` (prose, retrievable later).
"""

from __future__ import annotations

import base64
import colorsys
import io
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

SAMPLE_SIZE = 220
QUANTIZE_COLORS = 12
MIN_SHARE = 0.02  # ignore colours occupying under 2% of the mark

# --- the mark as the compositor will set it ---------------------------------
# A logo photographed or exported on white (the ordinary WhatsApp case: a
# JPEG) shipped as a white rectangle with a drop shadow on every photograph.
# At ingest the ground is removed and the mark trimmed to its ink, so what is
# stored is the mark and only the mark, with its true shape.
#
# The ground is "uniform" when the four corners agree within CORNER_TOLERANCE
# and is removed by flood-filling from each corner within FILL_TOLERANCE of
# that corner's colour -- a JPEG's ringing around the letters is inside it,
# a pale brand colour next to white is not.
CORNER_TOLERANCE = 24
FILL_TOLERANCE = 40
# Alpha below this is "not ink" when the mark is trimmed to its box.
INK_ALPHA = 8
# The least of the picture that must survive the ground fill for the result to
# be a mark at all.
MIN_INK_SHARE = 0.005
# Room left around the ink, as a share of the trimmed mark's longer side, so
# a thin outline is not cut by its own box.
TRIM_MARGIN = 0.02
# A mark this much wider than tall is a wordmark by shape, whatever the
# vision pass said or did not say: it is sized as a wide thing.
WORDMARK_ASPECT = 2.2


def _uniform_ground(rgb) -> tuple[int, int, int] | None:
    """The colour of the ground if the four corners agree, else None."""
    w, h = rgb.size
    corners = [rgb.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    lo = [min(c[i] for c in corners) for i in range(3)]
    hi = [max(c[i] for c in corners) for i in range(3)]
    if max(b - a for a, b in zip(lo, hi, strict=True)) > CORNER_TOLERANCE:
        return None
    return tuple(int(round(sum(c[i] for c in corners) / 4)) for i in range(3))


def remove_ground(rgba):
    """`rgba` with a uniform ground made transparent, or None when the ground
    is not uniform (a mark on a photograph, a gradient) and nothing is done.
    The fill runs from every corner, so a ground enclosed by the mark's own
    strokes (the counter of an O) stays -- that is part of the mark.

    The fill runs over a MASK, never over the picture. It used to be painted
    into the colour channels as the ground plus 128 and every pixel of that
    colour then deleted, which deleted whatever of the MARK happened to be
    that colour: a flat 50% grey rule under a wordmark on white lost the rule,
    and the stored aspect came from the truncated box, so the compositor sized
    the mark for the wrong shape. In the limit -- a black mark on a mid-grey
    ground, sentinel (0,0,0) -- the whole mark went, and a PNG that loads
    fine and paints nothing is exactly what FIT_JS's logo_not_loaded cannot
    see.
    """
    from PIL import ImageDraw

    rgb = rgba.convert("RGB")
    if _uniform_ground(rgb) is None:
        return None
    w, h = rgb.size
    arr = np.asarray(rgb, dtype=np.int16)
    reached = np.zeros((h, w), dtype=bool)
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        # Pillow's own measure of "within thresh of the seed": the sum of the
        # per-channel differences, so a JPEG's ringing around the letters is
        # inside it and a pale brand colour next to white is not.
        seed = np.array(rgb.getpixel(corner), dtype=np.int16)
        near = np.abs(arr - seed).sum(axis=2) <= FILL_TOLERANCE
        # .copy(): fromarray hands back an image sharing the array's buffer
        # read-only, and floodfill's write goes to a copy-on-write nothing
        # here can read back -- the fill silently does nothing.
        mask = Image.fromarray(np.where(near, 255, 0).astype(np.uint8), "L").copy()
        # Two values go in and a third is painted, so what the fill REACHED is
        # told apart from what merely looks like the ground.
        ImageDraw.floodfill(mask, corner, 1)
        reached |= np.asarray(mask) == 1
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8).copy()
    alpha[reached] = 0
    if float((alpha > INK_ALPHA).mean()) < MIN_INK_SHARE:
        # A fill that leaves no ink has not found a ground, it has eaten the
        # mark -- a pale mark on white, touching the edge. Keep the original.
        log.warning("logo_ground_fill_left_no_ink", size=f"{w}x{h}")
        return None
    out = rgba.copy()
    out.putalpha(Image.fromarray(alpha, "L"))
    return out


def prepare(image_bytes: bytes, mime: str | None) -> tuple[bytes, dict[str, Any]]:
    """The mark as it will be set, and what was measured of it.

    Returns (png_bytes, info): a transparent PNG trimmed to the ink, plus
    width, height, aspect (w/h), ink_luminance (alpha-weighted mean relative
    luminance of the ink, 0..1), has_transparency, ground_removed and
    trimmed. Bytes that are not an image come back unchanged with no info.
    """
    try:
        im = Image.open(io.BytesIO(image_bytes))
        im.load()
    except Exception:  # noqa: BLE001
        return image_bytes, {}
    from PIL import ImageOps

    rgba = ImageOps.exif_transpose(im).convert("RGBA")
    had_alpha = rgba.getchannel("A").getextrema()[0] < 250
    removed = False
    if not had_alpha:
        cleared = remove_ground(rgba)
        if cleared is not None:
            rgba, removed = cleared, True
    box = rgba.getchannel("A").point(lambda v: 255 if v > INK_ALPHA else 0).getbbox()
    trimmed = False
    if box and box != (0, 0, rgba.width, rgba.height):
        margin = int(round(TRIM_MARGIN * max(box[2] - box[0], box[3] - box[1])))
        rgba = rgba.crop(
            (
                max(0, box[0] - margin),
                max(0, box[1] - margin),
                min(rgba.width, box[2] + margin),
                min(rgba.height, box[3] + margin),
            )
        )
        trimmed = True
    # The ink's tone: the alpha-weighted mean relative luminance of what the
    # mark paints, so a light mark and a dark mark can be told apart before
    # anything is laid on a panel.
    px = np.asarray(rgba, dtype=np.float32) / 255.0
    weight = px[:, :, 3]
    lin = np.where(
        px[:, :, :3] <= 0.04045, px[:, :, :3] / 12.92, ((px[:, :, :3] + 0.055) / 1.055) ** 2.4
    )
    lum = lin @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    total = float(weight.sum())
    ink_luminance = round(float((lum * weight).sum() / total), 3) if total else 0.0
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    info = {
        "width": rgba.width,
        "height": rgba.height,
        "aspect": round(rgba.width / max(1, rgba.height), 3),
        "ink_luminance": ink_luminance,
        "has_transparency": had_alpha or removed,
        "ground_removed": removed,
        "trimmed": trimmed,
    }
    return buf.getvalue(), info


@dataclass
class LogoAnalysis:
    palette: dict[str, str] = field(default_factory=dict)
    swatches: list[dict[str, Any]] = field(default_factory=list)
    has_transparency: bool = False
    width: int = 0
    height: int = 0
    style: str | None = None
    tone: str | None = None
    has_wordmark: bool | None = None
    summary: str | None = None

    def as_prose(self, brand_name: str) -> str:
        bits = [f"{brand_name} logo:"]
        if self.style:
            bits.append(self.style)
        if self.tone:
            bits.append(f"tone reads as {self.tone}")
        if self.palette:
            bits.append("colours " + ", ".join(f"{k} {v}" for k, v in self.palette.items()))
        return " ".join(bits)


# --------------------------------------------------------------------------- #
# colour, measured
# --------------------------------------------------------------------------- #
def _luminance(rgb: tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _saturation(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return colorsys.rgb_to_hsv(r, g, b)[1]


def _hue(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return colorsys.rgb_to_hsv(r, g, b)[0]


def extract_palette(image_bytes: bytes) -> LogoAnalysis:
    """Count pixels. No model involved, so this cannot hallucinate a colour."""
    img = Image.open(io.BytesIO(image_bytes))
    result = LogoAnalysis(width=img.width, height=img.height)
    img = img.convert("RGBA")
    alpha = img.getchannel("A")
    result.has_transparency = alpha.getextrema()[0] < 250

    # Composite onto white so transparent logos do not contribute a black halo.
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=alpha)
    flat.thumbnail((SAMPLE_SIZE, SAMPLE_SIZE))

    quant = flat.quantize(colors=QUANTIZE_COLORS, method=Image.Quantize.FASTOCTREE)
    palette = quant.getpalette() or []
    total = sum(n for n, _ in quant.getcolors(1 << 16) or [])

    swatches: list[dict[str, Any]] = []
    for count, index in sorted(quant.getcolors(1 << 16) or [], reverse=True):
        rgb = tuple(palette[index * 3 : index * 3 + 3])
        if len(rgb) < 3:
            continue
        share = count / max(total, 1)
        if share < MIN_SHARE:
            continue
        swatches.append(
            {
                "hex": _hex(rgb),
                "rgb": list(rgb),
                "share": round(share, 4),
                "saturation": round(_saturation(rgb), 3),
                "luminance": round(_luminance(rgb), 3),
            }
        )
    result.swatches = swatches

    # Brand colour = the most-used colour that is not paper-white or ink-black.
    def is_neutral(sw: dict) -> bool:
        return sw["saturation"] < 0.12 and (sw["luminance"] > 0.88 or sw["luminance"] < 0.06)

    branded = [sw for sw in swatches if not is_neutral(sw)]
    branded.sort(key=lambda sw: sw["share"] * (0.4 + sw["saturation"]), reverse=True)

    if not branded:
        # A pure black-and-white mark is a legitimate answer, not a failure.
        result.palette = {
            "primary": "#111111",
            "secondary": "#FFFFFF",
            "accent": "#111111",
            "ink": "#FFFFFF",
        }
        return result

    primary = branded[0]
    accent = next(
        (
            sw
            for sw in branded[1:]
            if abs(_hue(tuple(sw["rgb"])) - _hue(tuple(primary["rgb"]))) > 0.08
        ),
        branded[1] if len(branded) > 1 else primary,
    )
    result.palette = {
        "primary": primary["hex"],
        "secondary": "#FFFFFF",
        "accent": accent["hex"],
        # Text laid over the primary colour: pick whichever actually reads.
        "ink": "#FFFFFF" if primary["luminance"] < 0.45 else "#111111",
    }
    return result


# --------------------------------------------------------------------------- #
# character, read
# --------------------------------------------------------------------------- #
VISION_PROMPT = """Look at this logo and answer as JSON only, no prose:

{"style": "<8 words max: geometric/hand-drawn/wordmark/emblem, modern/traditional>",
 "tone": "<3 words max, e.g. premium and calm / friendly and loud>",
 "has_wordmark": <true if the brand name is part of the mark>,
 "summary": "<one sentence a designer would write in a brand sheet>"}

Do not name the colours -- those are measured separately. Do not guess the
company's industry unless the mark makes it unambiguous."""


async def describe_logo(image_bytes: bytes, mime: str) -> dict[str, Any]:
    if not settings.anthropic_api_key or not settings.anthropic_model:
        log.warning("logo_vision_skipped", reason="no model configured")
        return {}
    from anthropic import AsyncAnthropic

    allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    media_type = mime if mime in allowed else "image/png"
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode(),
                        },
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }
        ],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _loads(text)


def _loads(text: str) -> dict[str, Any]:
    """Models wrap JSON in prose or fences more often than they should."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        log.warning("logo_vision_unparseable", head=text[:120])
        return {}


PHOTO_PROMPT = """This is a photo a small-business owner sent to their marketing assistant \
on WhatsApp. Answer with JSON only:
{"kind": "<product|shop|team|other>",
 "label": "<what it shows, <=8 words, the way the owner would say it>",
 "cut_out_ok": <true if a single product object could be cleanly cut out of the \
background and shown on its own; false for people, shopfronts, scenes, plates of food, \
multiple items, transparent glass>}
kind: "product" only for a single sellable item (a bottle, a box, a garment, a jar); \
"shop" for a storefront or interior; "team" for people; "other" for anything else."""


async def describe_photo(image_bytes: bytes, mime: str) -> dict[str, Any]:
    """What a non-logo photo is, so the product lane only ever cuts out a product.

    Without a model configured this returns {} and the caller falls back to
    "product" if the owner captioned it, "other" if they did not.
    """
    if not settings.anthropic_api_key or not settings.anthropic_model:
        return {}
    from anthropic import AsyncAnthropic

    allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    media_type = mime if mime in allowed else "image/jpeg"
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode(),
                        },
                    },
                    {"type": "text", "text": PHOTO_PROMPT},
                ],
            }
        ],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    out = _loads(text)
    if out.get("kind") not in ("product", "shop", "team", "other"):
        out.pop("kind", None)
    return out


async def analyse(image_bytes: bytes, mime: str) -> LogoAnalysis:
    result = extract_palette(image_bytes)
    try:
        described = await describe_logo(image_bytes, mime)
    except Exception as exc:  # noqa: BLE001 - the palette alone is still useful
        log.error("logo_vision_failed", error=str(exc)[:200])
        described = {}
    result.style = described.get("style")
    result.tone = described.get("tone")
    result.has_wordmark = described.get("has_wordmark")
    result.summary = described.get("summary")
    return result
