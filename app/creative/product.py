"""The product lane: the owner's own photo, product kept, background replaced.

A shop owner's product photo is the most real picture the creative can carry
and the one they judge hardest: they know exactly what the jar looks like. The
failure they will not forgive is the product itself coming out wrong -- a
chewed edge, a missing cap, a label that went soft. So this lane is built
around one rule: **the product pixels are never generated, only cut out and
placed.** If the cut cannot be trusted, the lane refuses and the photo is used
whole, as it was.

Three stages, each measurable:

1. **Cut.** A salient-object model (rembg, `settings.cutout_model`) produces
   the alpha. Inference runs on a <=1024px copy for memory and time; the mask
   is upscaled to the photo and refined at full resolution.
2. **Gate.** Coverage, edge softness and border contact decide whether the
   mask is a product or a guess. A mask that fails is not "improved", it is
   refused: a wrong cut ships a broken product, a refused cut ships the photo.
3. **Place.** A procedural studio backdrop (seamless-paper sweep, floor, grain,
   in the brand's neutrals), a contact shadow, and the product scaled into the
   zone the template leaves free of type. No image model is involved, so this
   lane costs nothing and cannot hallucinate a second bottle.
"""

from __future__ import annotations

import io
import math
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

# The model sees this many pixels on the long edge. Above it, memory and time
# grow quadratically and the mask does not get better; the refinement below
# happens at full resolution anyway.
INFER_EDGE = 1024

# --- the gate --------------------------------------------------------------
# A product occupies a real share of a phone photo. Below the floor the model
# grabbed a shadow or a prop; above the ceiling it kept the background.
MIN_COVERAGE = 0.03
MAX_COVERAGE = 0.80
# Share of the mask that is semi-transparent. Clean cuts are decisive; a
# mask that is a third fuzz is guessing (glass, hair, motion blur).
MAX_SOFT_SHARE = 0.30
# A product may sit on the bottom edge (it stands on something) but a mask
# that runs off two or more edges is a cropped product or retained backdrop.
MAX_BORDERS_TOUCHED = 1

# Where the product goes, per template: (top, bottom, max_width) as fractions
# of the canvas. Chosen to stay clear of the type AND the logo each template
# sets, and -- for split_card -- inside the band of the background that the
# template actually shows (it centre-crops the picture into the top 63% of
# the card, so the product must sit in the middle of the source, not its top).
PLACEMENT = {
    "lower_third": (0.17, 0.57, 0.68),
    "split_card": (0.24, 0.72, 0.62),
    "centered_overlay": (0.17, 0.40, 0.50),
    # The band covers the top 34%; the product stands in the photo below it,
    # above the footer row.
    "top_band": (0.40, 0.84, 0.70),
    # Words top-left: the product goes right of centre, lower half.
    "poster_stack": (0.42, 0.86, 0.56),
    # The card shows the top 56% of the picture, framed; the product sits in
    # the middle of that band.
    "frame_card": (0.10, 0.50, 0.62),
}
# Type in the middle of the canvas and a product in the middle of the canvas
# cannot both win. A product creative asked for as centered_overlay is set as
# lower_third instead; the pipeline records the switch in its result.
PREFERRED_TEMPLATE = "lower_third"

_session: Any = None
_load_lock = threading.Lock()
# One cutout at a time. The model is ~1.2GB resident per inference and
# CPU-bound: six slides in parallel took 3.5GB and were no faster than serial.
_infer = threading.Semaphore(1)


def _get_session():
    global _session
    with _load_lock:
        if _session is None:
            from rembg import new_session

            _session = new_session(settings.cutout_model)
            log.info("cutout_model_loaded", model=settings.cutout_model)
        return _session


def warm_up() -> None:
    """Load the model (downloading weights on first use). Run at build time."""
    _get_session()


@dataclass
class Cutout:
    ok: bool
    reason: str = ""
    coverage: float = 0.0
    soft_share: float = 0.0
    borders_touched: int = 0
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    rgba: Image.Image | None = field(default=None, repr=False)

    def metrics(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "coverage": round(self.coverage, 3),
            "soft_share": round(self.soft_share, 3),
            "borders_touched": self.borders_touched,
        }


def _load(photo: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(photo))
    im = ImageOps.exif_transpose(im)  # phones store rotation in EXIF
    return im.convert("RGB")


def _raw_mask(rgb: Image.Image, session=None) -> Image.Image:
    from rembg import remove

    work = rgb.copy()
    work.thumbnail((INFER_EDGE, INFER_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    work.save(buf, format="PNG")
    mask = remove(buf.getvalue(), session=session or _get_session(), only_mask=True)
    m = Image.open(io.BytesIO(mask)).convert("L")
    if m.size != rgb.size:
        m = m.resize(rgb.size, Image.BICUBIC)
    return m


def _refine(mask: Image.Image, scale: float = 1.0) -> Image.Image:
    """Kill the halo the model leaves, then feather the edge slightly.

    Erode so the background's colour does not ride along the outline (the
    classic "white fringe on a dark backdrop"), then a sub-pixel blur so the
    edge reads as photographed rather than cut with scissors. The mask was
    inferred at <=1024px and upscaled by `scale`; one pixel of erosion at
    1024 is `scale` pixels here.
    """
    k = 2 * max(1, math.ceil(scale)) + 1
    m = mask.filter(ImageFilter.MinFilter(k))
    return m.filter(ImageFilter.GaussianBlur(0.8 * max(1.0, scale)))


def _measure(mask: Image.Image) -> tuple[float, float, int, tuple[int, int, int, int]]:
    a = np.asarray(mask, dtype=np.uint8)
    solid = a > 128
    present = a > 20
    coverage = float(solid.mean())
    soft = float(((a > 20) & (a < 235)).sum() / max(1, present.sum()))
    ys, xs = np.where(solid)
    if len(xs) == 0:
        return coverage, soft, 0, (0, 0, 0, 0)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    h, w = a.shape
    edge = max(2, int(0.01 * min(w, h)))
    touched = sum(
        1 for hit in (x0 <= edge, y0 <= edge, x1 >= w - 1 - edge, y1 >= h - 1 - edge) if hit
    )
    return coverage, soft, touched, (x0, y0, x1 + 1, y1 + 1)


def cutout(photo: bytes, session=None) -> Cutout:
    """Cut the product out of the owner's photo, or say why it cannot be trusted."""
    try:
        rgb = _load(photo)
    except Exception as exc:  # noqa: BLE001
        return Cutout(ok=False, reason=f"unreadable image: {exc}"[:120])
    scale = max(rgb.size) / INFER_EDGE
    try:
        with _infer:
            mask = _refine(_raw_mask(rgb, session), scale)
    except Exception as exc:  # noqa: BLE001
        log.exception("cutout_model_failed")
        return Cutout(ok=False, reason=f"model failed: {exc}"[:120])

    coverage, soft, touched, bbox = _measure(mask)
    reason = ""
    if coverage < MIN_COVERAGE:
        reason = "product too small in frame or not found"
    elif coverage > MAX_COVERAGE:
        reason = "background not separated from product"
    elif soft > MAX_SOFT_SHARE:
        reason = "edges too uncertain (glass, hair or motion blur)"
    elif touched > MAX_BORDERS_TOUCHED:
        reason = "product runs off the edge of the photo"
    if reason:
        log.info("cutout_refused", reason=reason, coverage=round(coverage, 3), soft=round(soft, 3))
        return Cutout(False, reason, coverage, soft, touched, bbox)

    rgba = rgb.convert("RGBA")
    rgba.putalpha(mask)
    x0, y0, x1, y1 = bbox
    pad = int(0.02 * max(rgba.size))
    crop = rgba.crop(
        (max(0, x0 - pad), max(0, y0 - pad), min(rgba.width, x1 + pad), min(rgba.height, y1 + pad))
    )
    return Cutout(True, "", coverage, soft, touched, bbox, crop)


# --------------------------------------------------------------------------- #
# the studio
# --------------------------------------------------------------------------- #
def _hex(c: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        c = (c or "").lstrip("#")
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except (ValueError, IndexError):
        return default


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))  # type: ignore[return-value]


def backdrop(width: int, height: int, palette: dict | None, floor: float = 0.62) -> Image.Image:
    """A seamless-paper sweep in the brand's neutrals: lit from above, floor
    slightly darker, a hint of vignette and grain. What a product photographer
    builds with a roll of paper and two softboxes."""
    palette = palette or {}
    secondary = _hex(palette.get("secondary"), (246, 243, 238))
    primary = _hex(palette.get("primary"), (40, 40, 40))
    # Paper: mostly the light neutral with a whisper of the brand primary, so
    # the backdrop belongs to the brand without competing with the product.
    paper = _mix(secondary, primary, 0.06)
    top = _mix(paper, (255, 255, 255), 0.35)
    wall_low = _mix(paper, (0, 0, 0), 0.04)
    floor_col = _mix(paper, (0, 0, 0), 0.11)

    fy = int(height * floor)
    rows = np.empty((height, 3), dtype=np.float32)
    top_a, wall_a, floor_a = (np.array(c, dtype=np.float32) for c in (top, wall_low, floor_col))
    ys = np.arange(height, dtype=np.float32)
    wall_t = np.clip(ys[:fy] / max(1, fy), 0, 1) ** 1.4
    rows[:fy] = top_a + (wall_a - top_a) * wall_t[:, None]
    floor_t = np.clip((ys[fy:] - fy) / max(1, height - fy) * 1.6, 0, 1)
    rows[fy:] = wall_a + (floor_a - wall_a) * floor_t[:, None]
    im = Image.fromarray(np.repeat(rows[:, None, :], width, axis=1).astype(np.uint8), "RGB")
    # Soft floor line: the wall meets the paper curve, blurred.
    line = Image.new("L", (width, height), 0)
    ImageDraw.Draw(line).rectangle([0, fy - 2, width, fy + 2], fill=40)
    line = line.filter(ImageFilter.GaussianBlur(width * 0.02))
    im = Image.composite(Image.new("RGB", (width, height), floor_col), im, line)
    # Vignette, faint.
    vig = Image.new("L", (width, height), 0)
    ImageDraw.Draw(vig).ellipse(
        [-int(width * 0.2), -int(height * 0.25), int(width * 1.2), int(height * 1.15)], fill=255
    )
    vig = vig.filter(ImageFilter.GaussianBlur(width * 0.12))
    im = Image.composite(im, Image.new("RGB", (width, height), _mix(paper, (0, 0, 0), 0.16)), vig)
    # Grain, so it reads as paper rather than a gradient tool.
    rnd = np.random.default_rng(7)
    noise = rnd.normal(0, 3.2, (height, width, 1)).astype(np.float32)
    arr = np.clip(np.asarray(im, dtype=np.float32) + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def _contact_shadow(product: Image.Image, canvas: Image.Image, x: int, y: int) -> Image.Image:
    """Grounds the product. Two layers: a tight dark ellipse where it meets the
    floor and a wider, fainter one for ambient occlusion."""
    w, h = product.size
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    base_y = y + h - int(h * 0.03)
    d.ellipse(
        [x + int(w * 0.08), base_y - int(h * 0.05), x + int(w * 0.92), base_y + int(h * 0.05)],
        fill=(0, 0, 0, 120),
    )
    tight = layer.filter(ImageFilter.GaussianBlur(max(2, w * 0.03)))
    wide = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(wide).ellipse(
        [x - int(w * 0.15), base_y - int(h * 0.10), x + int(w * 1.15), base_y + int(h * 0.12)],
        fill=(0, 0, 0, 55),
    )
    wide = wide.filter(ImageFilter.GaussianBlur(max(4, w * 0.09)))
    return Image.alpha_composite(wide, tight)


def place(cut: Image.Image, canvas: Image.Image, template: str) -> Image.Image:
    """Scale the cut product into the zone the template leaves clear of type."""
    top, bottom, max_w = PLACEMENT.get(template, PLACEMENT[PREFERRED_TEMPLATE])
    W, H = canvas.size
    zone_h = int(H * (bottom - top))
    zone_w = int(W * max_w)
    scale = min(zone_w / cut.width, zone_h / cut.height)
    pw, ph = max(1, int(cut.width * scale)), max(1, int(cut.height * scale))
    product = cut.resize((pw, ph), Image.LANCZOS)
    x = (W - pw) // 2
    y = int(H * bottom) - ph  # stands on the zone floor, not floating mid-air
    out = canvas.convert("RGBA")
    out = Image.alpha_composite(out, _contact_shadow(product, out, x, y))
    out.alpha_composite(product, (x, y))
    return out.convert("RGB")


def product_background(
    photo: bytes,
    width: int,
    height: int,
    *,
    palette: dict | None,
    template: str,
    session=None,
) -> tuple[bytes, dict[str, Any]] | None:
    """The owner's product on a clean studio backdrop, sized for the canvas.

    Returns (jpeg_bytes, metrics) or None when the cut cannot be trusted --
    in which case the caller uses the photo as it was. Never raises: any
    failure here means "use the photo whole", not "lose the slide".
    """
    if not settings.cutout_enabled:
        return None
    try:
        cut = cutout(photo, session=session)
        if not cut.ok or cut.rgba is None:
            return None
        _, zone_bottom, _ = PLACEMENT.get(template, PLACEMENT[PREFERRED_TEMPLATE])
        # The floor seam sits exactly where the product stands.
        canvas = backdrop(width, height, palette, floor=zone_bottom)
        composed = place(cut.rgba, canvas, template)
        buf = io.BytesIO()
        composed.save(buf, format="JPEG", quality=92, subsampling=0)
        return buf.getvalue(), cut.metrics()
    except Exception:  # noqa: BLE001
        log.exception("product_background_failed")
        return None
