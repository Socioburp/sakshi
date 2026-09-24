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
# At most one edge of the photo may be touched, and only the bottom (see
# MIN_BASE_SHARE): a mask on two edges is a cropped product or kept backdrop.
MAX_BORDERS_TOUCHED = 1
# A product may stand on the bottom edge of the photo (it sits on something,
# and the contact shadow hides the flat base) -- if that contact is a BASE:
# the mask's bottom row spans at least this share of the product's width. A
# product cut by the left, right or top edge, or a bottom contact that is one
# thin point, is a sliced product; placed mid-canvas it reads as a crop.
MIN_BASE_SHARE = 0.40
# Erosion, in full-resolution pixels. One pixel kills the halo the model
# leaves; more than two takes chains, hooks and spoon handles with it -- a
# 4000px photo used to lose 4px from every edge.
MAX_ERODE_PX = 2
# Share of the raw mask's solid area the refinement may remove. Past this the
# erosion ate a thin feature, and the cut is refused rather than shipped
# missing a piece.
MAX_REFINE_LOSS = 0.03
# The cut-out is never enlarged past this: a product small in the frame was
# blown up ~3x to fill a zone, soft and ringing. Above the cap it is shown
# smaller in its space instead.
MAX_PLACE_UPSCALE = 1.25
# Room kept between the product and the edge of the space it stands in, as a
# share of the window's width.
PLACE_MARGIN = 0.04
# The least of the photo window the product may cover: its longer side over
# the window's shorter one. There was no floor at all -- place() took whatever
# the free rectangle left -- so a long headline and subhead on poster_stack or
# lower_third stood a 900x1600 bottle at 0.19 scale, 174x310 on a 1080x1350
# card: 3.7% of the frame, a postage stamp on an empty paper sweep, with FIT_JS
# reporting no violation because the subject was inside the window, clear of
# the words and inside the safe zone. The product is what the owner judges the
# card by, so the layout moves to one whose window has room before the frame
# is made, and the job is refused by name when none has. Measured: ordinary
# and long copy clear this on every panel layout (0.64 to 0.89); only the
# full-bleed layouts at the copy's limit fall under it.
MIN_PLACE_SHARE = 0.38

# Where the product goes when NO measured layout is at hand (a caller without
# the layout report; tests): (top, bottom, max_width, anchor) as fractions of
# the picture handed in, in the band that picture will show. The pipeline
# never uses these: it lays out first and places second, into the free
# rectangle FIT_JS measured (see `studio`).
PLACEMENT = {
    "lower_third": (0.17, 0.57, 0.68, 0.5),
    "split_card": (0.10, 0.88, 0.62, 0.5),
    "centered_overlay": (0.17, 0.40, 0.50, 0.5),
    "top_band": (0.08, 0.80, 0.70, 0.5),
    # Words top-left: the product goes right of centre, lower half.
    "poster_stack": (0.42, 0.86, 0.56, 0.62),
    "frame_card": (0.08, 0.90, 0.62, 0.5),
}
# Where the product stands, side to side, per layout: the centre of its
# space, or right of it where the words own the left.
ANCHOR = {"poster_stack": 0.62}
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
    edges: tuple[str, ...] = ()
    refine_loss: float = 0.0

    def metrics(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "coverage": round(self.coverage, 3),
            "soft_share": round(self.soft_share, 3),
            "borders_touched": self.borders_touched,
            "edges": list(self.edges),
            "refine_loss": round(self.refine_loss, 4),
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
    edge reads as photographed rather than cut with scissors. One pixel at
    full resolution, two when the mask was inferred well below it -- never
    more: `cutout` measures what the refinement removed and refuses a cut
    that lost a thin part to it.
    """
    erode = 1 if scale <= 1.0 else MAX_ERODE_PX
    m = mask.filter(ImageFilter.MinFilter(2 * erode + 1))
    return m.filter(ImageFilter.GaussianBlur(0.8 * max(1.0, min(scale, 2.0))))


def _measure(mask: Image.Image) -> tuple[float, float, tuple[str, ...], tuple[int, int, int, int]]:
    """(coverage, soft share, edges the product touches, bbox). An edge is
    'touched' when the mask reaches within 1% of it; the bottom counts as a
    BASE (not a cut) when the contact row is wide enough to stand on."""
    a = np.asarray(mask, dtype=np.uint8)
    solid = a > 128
    present = a > 20
    coverage = float(solid.mean())
    soft = float(((a > 20) & (a < 235)).sum() / max(1, present.sum()))
    ys, xs = np.where(solid)
    if len(xs) == 0:
        return coverage, soft, (), (0, 0, 0, 0)
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    h, w = a.shape
    edge = max(2, int(0.01 * min(w, h)))
    edges: list[str] = []
    if x0 <= edge:
        edges.append("left")
    if y0 <= edge:
        edges.append("top")
    if x1 >= w - 1 - edge:
        edges.append("right")
    if y1 >= h - 1 - edge:
        contact = int(solid[h - 1 - edge :, :].any(axis=0).sum())
        edges.append("bottom" if contact >= MIN_BASE_SHARE * (x1 - x0 + 1) else "bottom_point")
    return coverage, soft, tuple(edges), (x0, y0, x1 + 1, y1 + 1)


def cutout(photo: bytes, session=None) -> Cutout:
    """Cut the product out of the owner's photo, or say why it cannot be trusted."""
    try:
        rgb = _load(photo)
    except Exception as exc:  # noqa: BLE001
        return Cutout(ok=False, reason=f"unreadable image: {exc}"[:120])
    scale = max(rgb.size) / INFER_EDGE
    try:
        with _infer:
            raw = _raw_mask(rgb, session)
            mask = _refine(raw, scale)
    except Exception as exc:  # noqa: BLE001
        log.exception("cutout_model_failed")
        return Cutout(ok=False, reason=f"model failed: {exc}"[:120])

    coverage, soft, edges, bbox = _measure(mask)
    raw_area = int((np.asarray(raw, dtype=np.uint8) > 128).sum())
    loss = 1 - coverage * mask.width * mask.height / raw_area if raw_area else 0.0
    touched = len(edges)
    reason = ""
    if coverage < MIN_COVERAGE:
        reason = "product too small in frame or not found"
    elif coverage > MAX_COVERAGE:
        reason = "background not separated from product"
    elif soft > MAX_SOFT_SHARE:
        reason = "edges too uncertain (glass, hair or motion blur)"
    elif touched > MAX_BORDERS_TOUCHED or (edges and edges != ("bottom",)):
        reason = "product runs off the edge of the photo"
    elif loss > MAX_REFINE_LOSS:
        reason = "a thin part of the product would be lost at the edge"
    if reason:
        log.info(
            "cutout_refused",
            reason=reason,
            coverage=round(coverage, 3),
            soft=round(soft, 3),
            edges=list(edges),
            refine_loss=round(loss, 4),
        )
        return Cutout(False, reason, coverage, soft, touched, bbox, None, edges, loss)

    rgba = rgb.convert("RGBA")
    rgba.putalpha(mask)
    x0, y0, x1, y1 = bbox
    pad = int(0.02 * max(rgba.size))
    crop = rgba.crop(
        (max(0, x0 - pad), max(0, y0 - pad), min(rgba.width, x1 + pad), min(rgba.height, y1 + pad))
    )
    return Cutout(True, "", coverage, soft, touched, bbox, crop, edges, loss)


def cutout_png(cut: Cutout, source_asset_id: str | None = None) -> bytes:
    """The RGBA cut-out as a PNG that carries where it came from, so a later
    revision can stand the same product in a new window for nothing."""
    from PIL import PngImagePlugin

    assert cut.rgba is not None
    info = PngImagePlugin.PngInfo()
    if source_asset_id:
        info.add_text("source_asset_id", str(source_asset_id))
    # Not "bbox": a tEXt chunk of that name makes Pillow refuse to load the file.
    info.add_text("product_box", ",".join(str(v) for v in cut.bbox))
    buf = io.BytesIO()
    cut.rgba.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


def cutout_from_png(data: bytes) -> tuple[Image.Image, str | None]:
    """(rgba, source_asset_id) back from `cutout_png`."""
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGBA"), (im.info or {}).get("source_asset_id")


def subject_box(photo: bytes, session=None) -> tuple[tuple[int, int, int, int] | None, bool]:
    """Where the subject is in the owner's photo, in its UPRIGHT pixels, and
    whether the mask behind that box is sane enough to crop around.

    The same salient-object mask the cut-out lane uses; a cut the gate refuses
    (glass, a product on two edges) still says where the subject is, and that
    is what keeps a whole-photo crop off it. `trusted` is False when the mask
    grabbed a shadow or kept the backdrop (coverage outside the gate's range)
    or is mostly guess (soft): then the crop is centred as it always was.
    """
    if not settings.cutout_enabled:
        return None, False
    cut = cutout(photo, session=session)
    if cut.bbox == (0, 0, 0, 0):
        return None, False
    trusted = MIN_COVERAGE <= cut.coverage <= MAX_COVERAGE and cut.soft_share <= MAX_SOFT_SHARE
    return cut.bbox, trusted


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


def placed_size(
    cut: tuple[int, int], window: tuple[int, int], free: tuple[int, int, int, int]
) -> tuple[int, int]:
    """The size `place` will composite the cut product at: the largest it fits
    inside `free` with PLACE_MARGIN kept on every side, never enlarged past
    MAX_PLACE_UPSCALE. The pipeline measures the product with this before the
    layout is charged for, so the gate and the compositor share one formula
    instead of two that drift apart."""
    margin = int(round(window[0] * PLACE_MARGIN))
    zone_w = max(1, free[2] - free[0] - 2 * margin)
    zone_h = max(1, free[3] - free[1] - 2 * margin)
    scale = min(zone_w / cut[0], zone_h / cut[1], MAX_PLACE_UPSCALE)
    return max(1, int(cut[0] * scale)), max(1, int(cut[1] * scale))


def place_share(
    cut: tuple[int, int], window: tuple[int, int], free: tuple[int, int, int, int]
) -> float:
    """How much of the photo window the placed product covers, as its longer
    side over the window's shorter one. Held to MIN_PLACE_SHARE."""
    pw, ph = placed_size(cut, window, free)
    return max(pw, ph) / max(1, min(window))


def place(
    cut: Image.Image,
    canvas: Image.Image,
    free: tuple[int, int, int, int],
    anchor: float = 0.5,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Stand the cut product inside `free` (l, t, r, b), a rectangle of the
    canvas that the layout leaves clear of type and mark, with PLACE_MARGIN
    kept on every side and its base on the rectangle's floor. `anchor` is
    where its centre sits across the rectangle (0.5 the middle). Returns the
    composite and the product's box on it."""
    W, H = canvas.size
    margin = int(round(W * PLACE_MARGIN))
    fl, ft, fr, fb = free
    zone_w = max(1, fr - fl - 2 * margin)
    pw, ph = placed_size(cut.size, canvas.size, free)
    scale = pw / cut.width
    product = cut.resize((pw, ph), Image.LANCZOS)
    x = int(round(fl + margin + zone_w * anchor - pw / 2))
    x = max(fl + margin, min(x, fr - margin - pw))
    y = fb - margin - ph  # stands on the floor of its space, not floating mid-air
    out = canvas.convert("RGBA")
    out = Image.alpha_composite(out, _contact_shadow(product, out, x, y))
    out.alpha_composite(product, (x, y))
    log.info("product_placed", scale=round(scale, 3), box=[x, y, x + pw, y + ph], free=list(free))
    return out.convert("RGB"), (x, y, x + pw, y + ph)


def free_rect_for(template: str, width: int, height: int) -> tuple[int, int, int, int]:
    """The PLACEMENT fallback as a rectangle of a width x height picture."""
    top, bottom, max_w, _ = PLACEMENT.get(template, PLACEMENT[PREFERRED_TEMPLATE])
    half = width * max_w / 2 + width * PLACE_MARGIN
    centre = width * PLACEMENT.get(template, PLACEMENT[PREFERRED_TEMPLATE])[3]
    left = int(max(0, centre - half))
    right = int(min(width, centre + half))
    return left, int(height * top), right, int(height * bottom) + int(width * PLACE_MARGIN)


def studio(
    cut: Image.Image,
    width: int,
    height: int,
    *,
    palette: dict | None,
    free: tuple[int, int, int, int],
    anchor: float = 0.5,
) -> tuple[bytes, tuple[int, int, int, int]]:
    """The product on a studio backdrop of exactly width x height (the photo
    window), standing in `free`. Returns (png_bytes, product_box). The floor
    seam of the paper sweep sits where the product stands."""
    floor = min(0.95, max(0.3, (free[3] - int(round(width * PLACE_MARGIN))) / height))
    canvas = backdrop(width, height, palette, floor=floor)
    composed, box = place(cut, canvas, free, anchor)
    buf = io.BytesIO()
    composed.save(buf, format="PNG", compress_level=1)
    return buf.getvalue(), box


def product_background(
    photo: bytes,
    width: int,
    height: int,
    *,
    palette: dict | None,
    template: str,
    session=None,
    cut: Cutout | None = None,
    free: tuple[int, int, int, int] | None = None,
) -> tuple[bytes, dict[str, Any]] | None:
    """The owner's product on a clean studio backdrop, width x height -- the
    photo WINDOW the layout shows, not the canvas.

    Returns (png_bytes, metrics) or None when the cut cannot be trusted --
    in which case the caller uses the photo as it was. Never raises: any
    failure here means "use the photo whole", not "lose the slide". `cut` is
    a cut-out already made from this photo (the pipeline cuts once, before
    the layout is chosen); without one the model runs here. `free` is the
    rectangle the measured layout leaves clear of type and mark; without one
    the PLACEMENT fallback for the template is used. metrics["subject"] is
    the product's box on the picture.
    """
    if not settings.cutout_enabled:
        return None
    try:
        cut = cut or cutout(photo, session=session)
        if not cut.ok or cut.rgba is None:
            return None
        space = free or free_rect_for(template, width, height)
        anchor = ANCHOR.get(template, 0.5)
        png, box = studio(cut.rgba, width, height, palette=palette, free=space, anchor=anchor)
        return png, {**cut.metrics(), "subject": list(box), "free": list(space)}
    except Exception:  # noqa: BLE001
        log.exception("product_background_failed")
        return None
