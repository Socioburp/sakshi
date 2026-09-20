"""Headless-Chromium compositor.

The background image comes from the image model; everything a human reads --
headline, subhead, CTA, badge, logo -- is laid on top here in the brand's real
fonts. That split is what makes revisions nearly free: "make the headline
shorter" re-runs this function (~1s, no vendor call) instead of the image model.

The browser is launched once and reused. Cold-starting Chromium per creative
adds ~1.5s to every turn, which on WhatsApp is the difference between "fast"
and "did it break?".
"""

from __future__ import annotations

import asyncio
import base64
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from app.config import settings
from app.creative import fonts, legibility
from app.creative.brief import CreativeBrief, Slide
from app.logging import get_logger

log = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "creative"

# Autoescape must cover ".html.j2": select_autoescape(["html"]) matches names
# that END in .html, which none of these do, so for a while every headline
# and brand name reached Chromium as raw markup. Anything that must stay raw
# (the font stack) is marked |safe explicitly.
_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2"), default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


class TextDoesNotFit(RuntimeError):
    """The copy cannot be made to fit even at the minimum font size."""


TEMPLATES = {
    "centered_overlay": "centered_overlay.html.j2",
    "lower_third": "lower_third.html.j2",
    "split_card": "split_card.html.j2",
    "top_band": "top_band.html.j2",
    "poster_stack": "poster_stack.html.j2",
    "frame_card": "frame_card.html.j2",
}
DEFAULT_TEMPLATE = "centered_overlay"

# Instagram's profile grid shows the centre 3:4 of every post (the square grid
# went in 2025). A 1:1 post loses 12.5% on each side there; a 9:16 post loses
# a band top and bottom. Anything a reader must see -- headline, CTA, the
# mark -- stays inside that zone, plus a small margin.
GRID_RATIO = 3 / 4
GRID_MARGIN = 0.03  # of width, inside the safe zone

# The safe zone, in pixels of the 1080-wide canvas. The grid trims ~34px from
# each side of a 4:5 post; 90 clears that with room to spare. No text, logo or
# face may sit outside it -- FIT_JS asserts this on every render, and a breach
# fails the render rather than shipping.
SAFE_PAD = 90

# The one lossy encode in the whole path. The vendor returns PNG, Chromium
# screenshots PNG, and this is where it becomes the JPEG Instagram requires.
EXPORT_JPEG_QUALITY = 93


def grid_insets(width: int, height: int) -> tuple[int, int]:
    """(x, y) pixels trimmed on each side when the grid shows a 3:4 centre crop."""
    if width / height > GRID_RATIO:
        return int(round((width - height * GRID_RATIO) / 2)), 0
    return 0, int(round((height - width / GRID_RATIO) / 2))


def padding_for(width: int, height: int) -> dict[str, int]:
    """Content padding that clears the grid crop as well as the design's own
    margins. Templates read pad_x / pad_top / pad_bottom from the context."""
    x_in, y_in = grid_insets(width, height)
    margin = int(round(width * GRID_MARGIN))
    return {
        "pad_x": max(SAFE_PAD, x_in + margin),
        "pad_top": max(SAFE_PAD, y_in + margin),
        "pad_bottom": max(SAFE_PAD, int(round(width * 0.085)), y_in + margin),
    }


# Type floors, as a fraction of the canvas width. The fit search never goes
# below these: a headline that only fits at 30px on a 1080 canvas is ~10pt on
# a phone, which is "fits" in the way a crop is "fits". Below the floor the
# render FAILS and the copy is shortened upstream -- never truncated, never
# ellipsised, never shipped small.
HEADLINE_MIN = 0.044
SUBHEAD_MIN = 0.026
CTA_MIN = 0.026

# The mark is sized by canvas WIDTH, one figure per kind of mark, so it is the
# same size on every post of a brand whatever the layout. A wordmark is wide
# and reads at a modest height; an emblem is compact and needs the size.
LOGO_WIDTH = {"wordmark": 0.30, "emblem": 0.11}
LOGO_MAX_HEIGHT = 0.13
# Clear space kept free around the mark, as a fraction of its rendered height.
LOGO_CLEAR = 0.25

# Layouts that set type over the photograph; the rest set it on a solid panel,
# where there is nothing for a scrim to do.
TYPE_OVER_PHOTO = frozenset({"centered_overlay", "lower_third", "poster_stack"})


# Supersampling dial, measured rather than assumed.
#
# Rendering at 2x and resampling down does give visibly crisper stems and a
# cleaner logo -- but on this workload it cost 1.1s -> 4.7s per slide, and a
# six-slide carousel composes in parallel, so on a small instance it turned a
# ~1s step into ~28s.
#
# So it ships at 1. This is a measured engineering choice about the TYPE layer,
# not a quality tier: the photograph is generated at 1600x2000 and Lanczos-
# resampled to the canvas before it ever reaches the page (fit_background), so
# the picture gains nothing from a 2x page, and the type quality comes from
# the templates -- leading, tracking, an eased scrim, grain. Re-measure before
# changing it; do not change it to hit a time.
SUPERSAMPLE = 1

# How long the render waits for webfonts after the network goes idle.
FONT_WAIT_S = 6.0

_browser = None
_playwright = None
_loop = None  # the loop the BROWSER was launched on
_lock: asyncio.Lock | None = None
_lock_loop = None  # the loop the LOCK was created on


def _get_lock() -> asyncio.Lock:
    # A Lock is bound to the loop it is first used on. Creating it at import
    # time tied it to whichever loop imported the module -- under a test runner
    # or a worker restart that is not the loop compositing runs on. The lock's
    # loop is tracked separately from the browser's: keying it on the browser
    # loop meant every concurrent first caller (six slides of a carousel) got
    # its own lock and launched its own Chromium.
    global _lock, _lock_loop
    running = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not running:
        _lock, _lock_loop = asyncio.Lock(), running
    return _lock


async def get_browser():
    global _browser, _playwright, _loop
    lock = _get_lock()
    async with lock:
        running = asyncio.get_running_loop()
        if _browser is not None and _loop is not running:
            # The browser belongs to a loop that no longer exists. It cannot be
            # closed from here; drop the handles and start clean rather than
            # hang on a dead transport.
            log.warning("chromium_loop_changed_relaunching")
            _browser, _playwright = None, None
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            if _playwright is not None:
                # A previous launch failed or the browser died: release the old
                # driver before starting another, or they accumulate.
                try:
                    await _playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                _playwright = None
            pw = await async_playwright().start()
            try:
                _browser = await pw.chromium.launch(
                    executable_path=settings.chromium_executable or None,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"],
                )
            except Exception:
                await pw.stop()
                raise
            _playwright, _loop = pw, running
            log.info("chromium_launched")
    return _browser


async def shutdown() -> None:
    global _browser, _playwright, _loop
    lock = _get_lock()
    async with lock:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:  # noqa: BLE001
                pass
            _browser = None
        if _playwright is not None:
            try:
                await _playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            _playwright = None
        _loop = None


_FONT_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{0,48}$")


def _font_name(value: Any, default: str) -> str:
    """A family name safe to place inside quotes in a stylesheet."""
    name = str(value or "").strip()
    return name if _FONT_NAME_OK.match(name) else default


def _brand_context(brand: Any) -> dict[str, Any]:
    palette = dict(getattr(brand, "palette", {}) or {})
    faces = dict(getattr(brand, "fonts", {}) or {})
    analysis = dict(getattr(brand, "logo_analysis", {}) or {})
    # A wordmark and an emblem are not the same shape and must not be sized the
    # same way. Sizing both by height makes a wide wordmark span the canvas and
    # a compact emblem vanish. The vision pass already recorded which this is;
    # until now nothing read it.
    wordmark = bool(analysis.get("has_wordmark"))
    return {
        "logo_wordmark": wordmark,
        "logo_width_frac": LOGO_WIDTH["wordmark" if wordmark else "emblem"],
        "logo_max_height_frac": LOGO_MAX_HEIGHT,
        "name": getattr(brand, "name", ""),
        # Prefer the inlined data URI; fall back to the remote URL, then to text.
        "logo_url": getattr(brand, "logo_src", None) or getattr(brand, "logo_url", None),
        "primary": palette.get("primary", "#111111"),
        "secondary": palette.get("secondary", "#FFFFFF"),
        "accent": palette.get("accent", "#E4572E"),
        "ink": palette.get("ink", "#FFFFFF"),
        "heading_font": _font_name(faces.get("heading"), "Poppins"),
        "body_font": _font_name(faces.get("body"), "Inter"),
        "google_fonts": faces.get("google_fonts_href"),
    }


def _copy_text(brief: CreativeBrief, slide: Slide, brand_ctx: dict[str, Any]) -> str:
    """Every string the template can set in type, for script detection."""
    bits = [
        getattr(slide, "headline", "") or "",
        getattr(slide, "subhead", "") or "",
        brief.cta or "",
        getattr(slide, "badge", "") or "",
        str(brand_ctx.get("name") or ""),
    ]
    return " ".join(b for b in bits if b)


def render_html(
    brief: CreativeBrief,
    slide: Slide,
    brand: Any,
    background_data_uri: str,
    scrim_boost: float = 1.0,
) -> str:
    name = brief.template_for(slide)
    tpl = _env.get_template(TEMPLATES.get(name, TEMPLATES[DEFAULT_TEMPLATE]))
    w, h = brief.pixel_size()
    brand_ctx = _brand_context(brand)
    # The faces the copy needs, not the faces the brand chose: a Kannada
    # headline in a Latin display font is tofu unless a Kannada face is loaded.
    scripts = fonts.script_families(_copy_text(brief, slide, brand_ctx))
    brand_ctx["fonts_href"] = fonts.google_fonts_href(
        brand_ctx["heading_font"], brand_ctx["body_font"]
    )
    brand_ctx["script_fonts_href"] = fonts.script_fonts_href(scripts)
    brand_ctx["font_stack"] = fonts.css_stack(scripts)
    brand_ctx["indic"] = bool(scripts)
    prefs = getattr(brand, "template_prefs", None) or {}
    brand_ctx["signature"] = prefs.get("signature") or "none"
    return tpl.render(
        brief=brief,
        slide=slide,
        brand=brand_ctx,
        background=background_data_uri,
        width=w,
        height=h,
        **padding_for(w, h),
        # The CTA belongs to the post, and on a carousel it earns its place on
        # the last slide only -- repeating it on every slide reads as a template.
        scrim_boost=scrim_boost,
        show_cta=bool(brief.cta)
        and (not brief.is_carousel() or slide.position == len(brief.slides)),
    )


# Runs in the page after fonts settle. Two jobs, in this order:
#
#   FIT     For the headline, then the subhead, then the CTA: a binary search
#           for the LARGEST size between the type floor and the design size at
#           which the layout has no violations. Containers are auto-height, so
#           the text flows and the box follows it; nothing is set in a fixed
#           band and hoped to fit.
#   ASSERT  Every guarantee below is checked on real bounding boxes. Anything
#           left over is returned as a violation, and compose() refuses to
#           render. A collision is a bug, not a warning.
#
#             overflow:<box>      a container's content spills out of it
#             clipped:<el>        text wider than its own block
#             outside:<el>        an element leaves the canvas
#             unsafe:<el>         text or the mark outside the safe zone
#             overlap:<a>+<b>     two elements intersect
#             logo_clearspace:<x> something inside the mark's clear space
#             logo_not_loaded     the mark would render as a hole
#
# Text is measured with a Range, not the block box: an <h1> is as wide as its
# container however short the words are, and that is not what a reader sees.
FIT_JS = """
(cfg) => {
  const stage = document.querySelector('.stage');
  const S = stage.getBoundingClientRect();
  const W = S.width, H = S.height;
  const all = (s) => [...document.querySelectorAll(s)];
  const rel = (r) => ({l: r.left - S.left, t: r.top - S.top,
                       r: r.right - S.left, b: r.bottom - S.top});
  const boxOf = (el) => rel(el.getBoundingClientRect());
  // Width from the words (a Range), height from the block. A Range's height is
  // the font's full ascent+descent -- ~1.5em for Poppins -- which overstates
  // the line box of display type set at line-height .98 by a quarter em top
  // and bottom, and reported every large headline as outside the safe zone.
  const inkOf = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const r = range.getBoundingClientRect();
    const b = boxOf(el);
    if (!(r.width && r.height)) return b;
    const k = rel(r);
    return {l: Math.max(b.l, k.l), t: b.t, r: Math.min(b.r, k.r), b: b.b};
  };
  const area = (b) => Math.max(0, b.r - b.l) * Math.max(0, b.b - b.t);
  const hit = (a, b, tol = 1) =>
    a.l < b.r - tol && b.l < a.r - tol && a.t < b.b - tol && b.t < a.b - tol;
  const grow = (b, d) => ({l: b.l - d, t: b.t - d, r: b.r + d, b: b.b + d});

  // Auto-height panels push the photograph: the picture takes what the words
  // leave, instead of the words being cut to what a fixed panel allows.
  const reflow = () => {
    const panel = document.querySelector('[data-photo-above]');
    if (!panel) return;
    const d = panel.dataset;
    const top = parseFloat(d.photoTop || 0), gap = parseFloat(d.gap || 0);
    const h = Math.max(0, boxOf(panel).t - gap - top + parseFloat(d.overlap || 0));
    all('.bg, .scrim, .grain').forEach(e => { e.style.height = h + 'px'; });
  };

  const TEXT = ['headline', 'subhead', 'brandline'];
  const elements = () => {
    const out = [];
    for (const cls of ['headline', 'subhead', 'cta', 'logo', 'brandline', 'rule']) {
      for (const el of all('.' + cls)) {
        const box = TEXT.includes(cls) ? inkOf(el) : boxOf(el);
        if (area(box) > 0) out.push({cls, el, box});
      }
    }
    return out;
  };

  const violations = () => {
    const v = [];
    for (const b of all('.content, .panel, .band, .card')) {
      const name = b.className.split(' ')[0];
      if (b.scrollHeight > b.clientHeight + 1 || b.scrollWidth > b.clientWidth + 1)
        v.push('overflow:' + name);
      const r = boxOf(b);
      if (r.l < -1 || r.t < -1 || r.r > W + 1 || r.b > H + 1) v.push('outside:' + name);
    }
    const els = elements();
    const safe = {l: cfg.safe.x, t: cfg.safe.top, r: W - cfg.safe.x, b: H - cfg.safe.bottom};
    for (const e of els) {
      if (['headline', 'subhead', 'cta'].includes(e.cls) && e.el.scrollWidth > e.el.clientWidth + 1)
        v.push('clipped:' + e.cls);
      const b = e.box;
      if (b.l < -1 || b.t < -1 || b.r > W + 1 || b.b > H + 1) v.push('outside:' + e.cls);
      else if (e.cls !== 'rule' && (b.l < safe.l - 1 || b.r > safe.r + 1
                                    || b.t < safe.t - 1 || b.b > safe.b + 1))
        v.push('unsafe:' + e.cls);
    }
    for (let i = 0; i < els.length; i++)
      for (let j = i + 1; j < els.length; j++)
        if (hit(els[i].box, els[j].box)) v.push('overlap:' + els[i].cls + '+' + els[j].cls);
    // The corner signature is a triangle; test the triangle, not its square.
    const corner = document.querySelector('.sig-corner');
    if (corner) {
      const c = boxOf(corner);
      for (const e of els)
        if (e.box.r > c.l && e.box.t < c.b && (e.box.r - c.l) > (e.box.t - c.t) + 1)
          v.push('overlap:' + e.cls + '+sig-corner');
    }
    const logo = document.querySelector('img.logo');
    if (logo) {
      if (!logo.complete || logo.naturalWidth === 0) v.push('logo_not_loaded');
      const lb = boxOf(logo);
      const clear = grow(lb, cfg.logoClear * (lb.b - lb.t));
      if (clear.l < 0 || clear.t < 0 || clear.r > W || clear.b > H) v.push('logo_clearspace:edge');
      for (const e of els)
        if (e.el !== logo && hit(clear, e.box, 0)) v.push('logo_clearspace:' + e.cls);
    }
    return [...new Set(v)];
  };

  // The three sized elements, each with a design size (MAX) and a floor (MIN).
  const parts = [['headline', cfg.min.headline], ['subhead', cfg.min.subhead], ['cta', cfg.min.cta]]
    .map(([cls, min]) => {
      const el = document.querySelector('.' + cls);
      if (!el) return null;
      const design = parseFloat(getComputedStyle(el).fontSize);
      return {cls, el, design, min: Math.min(min, design), px: design, steps: 0};
    }).filter(Boolean);
  const set = (p, px) => { p.px = px; p.el.style.fontSize = px + 'px'; reflow(); };
  const ok = () => violations().length === 0;
  const half = (x) => Math.floor(x * 2) / 2;

  reflow();
  if (!ok() && parts.length) {
    // Pass 1: one scale for all three, MIN..MAX together. Binary search for the
    // largest scale that satisfies every guarantee. Shrinking them together
    // keeps the hierarchy; shrinking the headline alone for a collision the
    // CTA caused would throw away optical weight for nothing.
    const at = (s) => parts.forEach(p => set(p, p.min + (p.design - p.min) * s));
    let lo = 0, hi = 1;
    at(0);
    if (ok()) {
      while (hi - lo > 0.01) {
        const mid = (lo + hi) / 2;
        at(mid); parts.forEach(p => p.steps++);
        if (ok()) lo = mid; else hi = mid;
      }
      at(lo);
      parts.forEach(p => set(p, half(p.px)));
      // Pass 2: give back, one element at a time, whatever that element did
      // not need to give up. Each is a binary search between where it is and
      // its design size.
      for (const p of parts) {
        let a = p.px, b = p.design;
        while (b - a > 0.5) {
          const mid = (a + b) / 2;
          set(p, mid); p.steps++;
          if (ok()) a = mid; else b = mid;
        }
        set(p, half(a));
      }
    }
    // else: not even the floors fit. Left at MIN; violations() reports why,
    // and compose() refuses the render.
  }
  const sizes = {};
  for (const p of parts) sizes[p.cls] = {px: p.px, design: p.design, steps: p.steps};

  // The block the scrim has to serve: the union of the words actually set.
  let text = null;
  for (const e of elements()) {
    if (!['headline', 'subhead', 'cta'].includes(e.cls)) continue;
    text = text ? {l: Math.min(text.l, e.box.l), t: Math.min(text.t, e.box.t),
                   r: Math.max(text.r, e.box.r), b: Math.max(text.b, e.box.b)} : {...e.box};
  }
  const boxes = {};
  for (const e of elements()) boxes[e.cls] = e.box;
  return {violations: violations(), sizes, boxes, canvas: [W, H],
          text_box: text && [text.l / W, text.t / H, text.r / W, text.b / H]};
}
"""

# Places the local scrim under the measured text block and sets both scrims'
# strength from what legibility.py measured BEHIND that block.
SCRIM_JS = """
({box, boost, local}) => {
  const stage = document.querySelector('.stage').getBoundingClientRect();
  const W = stage.width, H = stage.height;
  const scrim = document.querySelector('.scrim');
  if (scrim) scrim.style.opacity = boost;
  const el = document.querySelector('.scrim-text');
  if (!el || !box) return;
  const feather = W * 0.07;
  el.style.left = (box[0] * W - feather) + 'px';
  el.style.top = (box[1] * H - feather) + 'px';
  el.style.width = ((box[2] - box[0]) * W + 2 * feather) + 'px';
  el.style.height = ((box[3] - box[1]) * H + 2 * feather) + 'px';
  el.style.opacity = local;
}
"""

# Which of the faces this creative needs actually arrived. `document.fonts.check`
# is no use here: it answers true for a family that does not exist at all.
FONTS_JS = """
(families) => {
  const loaded = new Set([...document.fonts].filter(f => f.status === 'loaded')
                           .map(f => f.family.replace(/["']/g, '')));
  return families.filter(f => !loaded.has(f));
}
"""


def as_data_uri(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"


def _resample(png: bytes, width: int, height: int) -> bytes:
    """Lanczos the supersampled frame down to the delivery size."""
    with Image.open(BytesIO(png)) as im:
        if im.size == (width, height):
            return png
        out = BytesIO()
        im.convert("RGB").resize((width, height), Image.LANCZOS).save(
            out, format="PNG", optimize=True
        )
        return out.getvalue()


def export_jpeg(png: bytes, size: tuple[int, int]) -> bytes:
    """The finished frame as the JPEG that is delivered and published.

    Refuses anything that is not exactly `size`: a creative of the wrong
    dimensions is cropped or letterboxed by Instagram, and inside a carousel
    it drags every other slide with it.
    """
    with Image.open(BytesIO(png)) as im:
        if im.size != tuple(size):
            raise ValueError(f"export is {im.size}, must be exactly {tuple(size)}")
        out = BytesIO()
        im.convert("RGB").save(
            out, format="JPEG", quality=EXPORT_JPEG_QUALITY, subsampling=0, optimize=True
        )
        return out.getvalue()


def image_size(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as im:
        return im.size


class LayoutError(TextDoesNotFit):
    """A deterministic guarantee cannot be met. The render is refused.

    `violations` are FIT_JS's strings ("overlap:headline+logo",
    "overflow:panel", ...). The cure is always upstream: shorter copy.
    """

    def __init__(self, violations: list[str], *, position: int | None = None) -> None:
        self.violations = list(violations)
        self.position = position
        super().__init__(f"layout guarantees not met: {', '.join(self.violations)}")


class BrandFontUnavailable(RuntimeError):
    """A face the creative is set in did not load. Rendering it in a fallback
    face would be an approximation of the brand, so the render is refused."""


def fit_background(image: bytes, width: int, height: int) -> bytes:
    """The generated picture, resampled to the canvas with Lanczos.

    Generation runs above the delivery size (1600x2000 for a 1080x1350 post).
    Handing the browser the full frame and letting `object-fit` scale it uses
    Chromium's bilinear-ish filter and makes every page carry a 5MB data URI;
    resampling here is both sharper and faster. Same ratio in, so nothing is
    cropped; a legacy or owner-supplied picture of another ratio is centre-
    cropped to cover, which is what the template did with it anyway.
    """
    with Image.open(BytesIO(image)) as im:
        im = im.convert("RGB")
        if im.size == (width, height):
            return image
        scale = max(width / im.width, height / im.height)
        if abs(im.width * scale - width) > 1 or abs(im.height * scale - height) > 1:
            cw, ch = width / scale, height / scale
            left, top = (im.width - cw) / 2, (im.height - ch) / 2
            im = im.crop((round(left), round(top), round(left + cw), round(top + ch)))
        out = BytesIO()
        im.resize((width, height), Image.LANCZOS).save(out, format="PNG", compress_level=1)
        return out.getvalue()


def _fit_config(w: int, h: int) -> dict[str, Any]:
    pad = padding_for(w, h)
    return {
        "safe": {"x": pad["pad_x"], "top": pad["pad_top"], "bottom": pad["pad_bottom"]},
        "min": {
            "headline": round(w * HEADLINE_MIN),
            "subhead": round(w * SUBHEAD_MIN),
            "cta": round(w * CTA_MIN),
        },
        "logoClear": LOGO_CLEAR,
    }


def _faces_needed(brief: CreativeBrief, slide: Slide, brand: Any) -> list[str]:
    ctx = _brand_context(brand)
    body_used = bool(slide.subhead or brief.cta or not ctx["logo_url"])
    faces = [ctx["heading_font"]] + ([ctx["body_font"]] if body_used else [])
    faces += fonts.script_families(_copy_text(brief, slide, ctx))
    return list(dict.fromkeys(faces))


async def _layout(page, brief: CreativeBrief, slide: Slide, brand: Any, html: str) -> dict:
    """Load the page, prove the fonts, fit the type, assert the guarantees."""
    template = brief.template_for(slide)
    # The document is set immediately; the wait is for stylesheets and fonts.
    # Bounded twice -- here and on fonts.ready -- so a font host that stalls
    # costs seconds. What happens next is not a fallback face: see below.
    try:
        await page.set_content(html, wait_until="networkidle", timeout=int(FONT_WAIT_S * 1000))
    except Exception:  # noqa: BLE001 - playwright's TimeoutError
        log.warning("page_not_idle", template=template)
    try:
        await asyncio.wait_for(page.evaluate("document.fonts.ready"), FONT_WAIT_S)
    except Exception:  # noqa: BLE001
        log.warning("fonts_not_settled", template=template)

    if settings.compose_require_fonts:
        missing = await page.evaluate(FONTS_JS, _faces_needed(brief, slide, brand))
        if missing:
            raise BrandFontUnavailable(f"brand faces did not load: {', '.join(missing)}")

    w, h = brief.pixel_size()
    report = await page.evaluate(FIT_JS, _fit_config(w, h))
    shrunk = {k: v for k, v in (report.get("sizes") or {}).items() if v and v.get("steps")}
    if shrunk:
        log.info("text_autofit", template=template, sizes=shrunk)
    if report["violations"]:
        log.error(
            "layout_refused",
            template=template,
            position=slide.position,
            violations=report["violations"],
            headline=slide.headline,
        )
        raise LayoutError(report["violations"], position=slide.position)
    return report


# A 1x1 stand-in: layout does not depend on the picture, so the guarantees can
# be proven before a picture is paid for.
_BLANK_BG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "2mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


async def _check_layout_once(brief: CreativeBrief, slide: Slide, brand: Any) -> dict:
    """Prove this slide's copy can be set -- before any money is spent.

    Raises LayoutError with the violations when it cannot. Runs the very same
    page, fit and assertions as compose(), over a blank background.
    """
    w, h = brief.pixel_size()
    browser = await get_browser()
    page = await browser.new_page(viewport={"width": w, "height": h})
    try:
        return await _layout(page, brief, slide, brand, render_html(brief, slide, brand, _BLANK_BG))
    finally:
        await page.close()


async def _compose_once(
    brief: CreativeBrief,
    slide: Slide,
    brand: Any,
    background: bytes,
    background_mime: str = "image/jpeg",
) -> bytes:
    """Return the finished PNG for one slide (a single post is slide 1 of 1).

    Raises LayoutError / BrandFontUnavailable instead of returning a frame that
    breaks a guarantee. There is no degraded output.
    """
    w, h = brief.pixel_size()
    template = brief.template_for(slide)
    fitted = await asyncio.to_thread(fit_background, background, w, h)
    mime = "image/png" if fitted is not background else background_mime
    html = render_html(brief, slide, brand, as_data_uri(fitted, mime))
    browser = await get_browser()
    page = await browser.new_page(
        viewport={"width": w, "height": h}, device_scale_factor=SUPERSAMPLE
    )
    try:
        report = await _layout(page, brief, slide, brand, html)

        # The scrim is sized to the words as they were actually set, and its
        # strength comes from the pixels behind THEM -- not from a fixed band
        # the template was assumed to set its type in.
        if template in TYPE_OVER_PHOTO and report.get("text_box"):
            boost, local, why = await asyncio.to_thread(
                legibility.scrim_for_box, fitted, tuple(report["text_box"])
            )
            if boost != 1.0 or local:
                log.info("scrim_measured", template=template, **why)
            await page.evaluate(
                SCRIM_JS, {"box": report["text_box"], "boost": boost, "local": local}
            )

        shot = await page.screenshot(type="png", clip={"x": 0, "y": 0, "width": w, "height": h})
        return _resample(shot, w, h)
    finally:
        await page.close()


def _is_browser_death(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        s in text
        for s in ("target closed", "browser has been closed", "disconnected", "connection closed")
    )


async def _surviving_a_crash(fn, *args):
    """Run a render; if Chromium died under it, relaunch and run it once more.

    One shared browser serves every job in flight, so one crash used to fail
    them all. get_browser() already relaunches a disconnected browser; this is
    what gives the render that was caught mid-crash its second go. Refusals
    (LayoutError, BrandFontUnavailable) are verdicts, not crashes, and pass
    straight through.
    """
    try:
        return await fn(*args)
    except (TextDoesNotFit, BrandFontUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001
        if not _is_browser_death(exc):
            raise
        log.warning("chromium_died_retrying", error=repr(exc)[:160])
        return await fn(*args)


async def check_layout(brief: CreativeBrief, slide: Slide, brand: Any) -> dict:
    """Prove this slide's copy can be set -- before any money is spent."""
    return await _surviving_a_crash(_check_layout_once, brief, slide, brand)


async def compose(
    brief: CreativeBrief,
    slide: Slide,
    brand: Any,
    background: bytes,
    background_mime: str = "image/jpeg",
) -> bytes:
    """The finished PNG for one slide, or a refusal. Never a degraded frame."""
    return await _surviving_a_crash(_compose_once, brief, slide, brand, background, background_mime)


async def compose_to_file(
    brief: CreativeBrief, slide: Slide, brand: Any, background: bytes, out: Path
) -> Path:
    out.write_bytes(await compose(brief, slide, brand, background))
    return out
