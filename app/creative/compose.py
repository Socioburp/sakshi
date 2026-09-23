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

# Share of a full-screen 9:16 frame the app's own chrome covers, top and bottom.
STORY_TOP = 0.14
STORY_BOTTOM = 0.18

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
    if height / width > 1.7:
        # Full-screen 9:16 (a story, a reel's card). The app draws over it: the
        # account row and progress bars across the top ~14%, the reply bar and
        # send/like across the bottom ~18%. Instagram's own guidance is to keep
        # text and logos out of roughly the top 250px and bottom 340px of 1920.
        return {
            "pad_x": max(SAFE_PAD, x_in + margin),
            "pad_top": max(SAFE_PAD, y_in + margin, int(round(height * STORY_TOP))),
            "pad_bottom": max(SAFE_PAD, y_in + margin, int(round(height * STORY_BOTTOM))),
        }
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
#
# The subhead and CTA floors were 0.026 (28px, ~10pt on a phone) while the type
# was rasterised at 1x. Instagram and WhatsApp re-encode whatever they are sent,
# and 28px body type is where that re-encode starts to eat the counters. 0.030
# (32px) is the smallest size that survives it; the brand name, set in
# tracked capitals, holds at 0.026 -- at FULL opacity, never the .88 it had.
HEADLINE_MIN = 0.044
SUBHEAD_MIN = 0.030
CTA_MIN = 0.030
BRANDLINE_MIN = 0.026

# Past these a post stops being a post. A headline that needs five lines or a
# subhead that needs four is a paragraph set in display type, and the agent is
# told to shorten it before anything is charged. The search shrinks the type
# first -- smaller type takes fewer lines -- so these only refuse copy that
# cannot be set in four (three, two) lines even at the floor.
MAX_LINES = {"headline": 4, "subhead": 3, "brandline": 2}
# The headline is at least this many times the subhead once both are fitted.
# At the floors (48px over 32px) it is exactly 1.5, so the floors alone never
# break it; what it stops is the second pass regrowing a subhead to 41px under
# a headline that had to stay at 48.
HIERARCHY = 1.5
# The least clear space between the ink of two lines of one element, in em.
LINE_GAP = 0.03
# How far FIT_JS may open an element's leading past the template's to get it.
MAX_EXTRA_LEADING = 0.5

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
# Layouts whose CTA pill sits on the brand's PRIMARY panel rather than on the
# photograph, so the pill's own colour has to separate from that panel.
CTA_ON_PANEL = frozenset({"split_card", "frame_card"})
# A shape (the pill, the frame's border) needs this much contrast against the
# panel to read as a shape at all. Palettes pulled from a one-colour logo come
# back with accent ~= primary, and the button dissolved into the panel: its
# label stayed readable, as loose bold text with no button around it.
MIN_SHAPE_CONTRAST = 2.0


# Supersampling: the DELIVERED frame is rasterised at 2x and Lanczos-resampled
# to the export size, which gives visibly crisper stems, cleaner counters in
# 32px body type and a cleaner logo edge. It used to ship at 1 because 2x was
# measured at 1.1s -> 4.7s per slide -- but most of that was not the render: it
# was the resampled PNG being written with optimize=True (1.3s on its own) for
# a PNG that only ever goes to export_jpeg. Now the pre-charge layout proof
# (check_layout) runs at 1x, where geometry is identical at any device scale;
# compose() re-runs the same fit on the 2x page it screenshots, because the
# page it measures must be the page it ships, and the intermediate PNG is
# written fast.
#
# Measured on the same machine, same lower_third slide with a logo, together
# with the legibility pass (two more 1x screenshots of the frame):
#     one slide           1.02s -> 1.86s   (layout .61, legibility .40,
#                                           2x screenshot .63, resample .14)
#     six in parallel     1.97s -> 5.9s    (Chromium serialises the six 2x
#                                           screenshots; they are the floor)
# That is the price of type that survives Instagram's re-encode and of a frame
# whose contrast was measured, and it is paid once per DELIVERED slide, never
# per fit attempt. Quality is not traded for speed in this product; re-measure
# before changing it, and do not change it to hit a time.
SUPERSAMPLE = 2

# How many times the legibility pass may strengthen a plate and re-measure the
# rendered frame before it gives up and refuses. The plate is computed from
# the contrast target, so the first answer is normally the last.
LEGIBILITY_ROUNDS = 3

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


# WCAG 2.x contrast. 4.5:1 is the AA line for body text; the subhead is body
# text, so everything set in type is held to it.
MIN_CONTRAST = 4.5
_LIGHT, _DARK, _BLACK = "#FFFFFF", "#141414", "#000000"
_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
# What a brand gets for a colour it never stated (or stated in a form nothing
# can read): a near-black panel, white ink, one warm accent.
DEFAULT_PALETTE = {
    "primary": "#111111",
    "secondary": "#FFFFFF",
    "accent": "#E4572E",
    "ink": "#FFFFFF",
}


def normalise_colour(value: Any) -> str | None:
    """`value` as '#RRGGBB', or None when it is not a colour.

    Every colour is brought to this one form at the boundary, because the
    stylesheet and the contrast maths used to read the palette differently:
    CSS renders 'rgb(18,59,46)', 'darkgreen', '#123' and ' #123B2E' faithfully,
    while the maths saw "not #RRGGBB" and quietly used mid grey. That put pure
    black type on the brand's own dark green panel, then laid WHITE plates
    over the panel to rescue it, and shipped the smear as 4.7:1. An alpha
    channel is dropped: a brand colour is a colour, not a tint.
    """
    from PIL import ImageColor

    text = str(value or "").strip()
    if not text:
        return None
    try:
        rgb = ImageColor.getrgb(text)
    except ValueError:
        return None
    return "#{:02X}{:02X}{:02X}".format(*rgb[:3])


def _luminance(colour: str) -> float:
    if not _HEX6.match(colour or ""):
        raise ValueError(f"not a normalised colour: {colour!r}")
    out = []
    for i in (1, 3, 5):
        v = int(colour[i : i + 2], 16) / 255
        out.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_on(ground: str, wanted: str) -> str:
    """`wanted` if it can be read on `ground`; otherwise white or near-black,
    whichever reads better. The brand's colour is used whenever it CAN be."""
    if _HEX6.match(wanted or "") and contrast(wanted, ground) >= MIN_CONTRAST:
        return wanted
    best = max((_LIGHT, _DARK), key=lambda c: contrast(c, ground))
    # On a mid-tone ground (luminance ~0.19) white and near-black BOTH land at
    # ~4.3:1. Pure black is the one ink that always clears the bar there.
    return best if contrast(best, ground) >= MIN_CONTRAST else _BLACK


def _palette(brand: Any) -> dict[str, str]:
    """The brand's colours, every one of them '#RRGGBB'. One that cannot be
    read is replaced by the default for its role and logged -- never used as
    written by the stylesheet and as mid grey by the maths."""
    stated = dict(getattr(brand, "palette", {}) or {})
    palette = {}
    for role, default in DEFAULT_PALETTE.items():
        colour = normalise_colour(stated.get(role))
        if colour is None and stated.get(role):
            log.warning("palette_colour_unreadable", role=role, value=str(stated[role])[:40])
        palette[role] = colour or default
    return palette


def _brand_context(brand: Any) -> dict[str, Any]:
    palette = _palette(brand)
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
        **palette,
        # Set per layout in render_html: the brand's ink where it can be read,
        # a readable ink where it cannot. See `readable_on`.
        "ink_photo": _LIGHT,
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
    # Vendored faces come off disk; Google Fonts is asked only for a face that
    # is not vendored (a brand outside the four looks).
    brand_ctx["local_fonts_href"] = fonts.local_href()
    brand_ctx["fonts_href"] = fonts.remote_brand_href(
        brand_ctx["heading_font"], brand_ctx["body_font"]
    )
    brand_ctx["script_fonts_href"] = fonts.script_fonts_href(scripts)
    brand_ctx["font_stack"] = fonts.css_stack(scripts)
    brand_ctx["indic"] = bool(scripts)
    # Tracked capitals are a Latin device; conjuncts and joined Arabic letters
    # come apart under it. Decided by the NAME's script, not the copy's.
    brand_ctx["name_tracking"] = "0" if fonts.script_families(brand_ctx["name"]) else ".14em"
    # Leading follows the script of the element it sets, not of the whole
    # creative: a Latin headline over a Hindi brand name stays tight.
    brand_ctx["display_leading"] = fonts.display_leading(fonts.script_families(slide.headline))
    brand_ctx["body_leading"] = fonts.body_leading(fonts.script_families(slide.subhead or ""))
    prefs = getattr(brand, "template_prefs", None) or {}
    brand_ctx["signature"] = prefs.get("signature") or "none"
    # LEGIBILITY IS A GUARANTEE TOO. The layouts that set type over the
    # photograph put a BLACK scrim behind it, so the type must be light; a brand
    # whose ink is dark (a light-palette brand) came out as near-black words on
    # a darkened photograph -- inside every box, overlapping nothing, and
    # unreadable. On a solid panel the ink must read against the brand's own
    # primary, and the CTA's label against its accent. The brand's colour is
    # kept wherever it passes 4.5:1 and replaced only where it cannot.
    ink = brand_ctx["ink"]
    brand_ctx["ink_photo"] = ink if contrast(ink, "#000000") >= 7.0 else _LIGHT
    if name in TYPE_OVER_PHOTO:
        brand_ctx["ink"] = brand_ctx["ink_photo"]
    else:
        brand_ctx["ink"] = readable_on(brand_ctx["primary"], ink)
    # THE BUTTON MUST READ AS A BUTTON. On a panel layout the pill sits on the
    # primary; when the accent cannot be told from it, the pill (and
    # frame_card's border) take the panel's readable ink and the label takes
    # the panel's colour -- an inverse button, decided here, never at random.
    brand_ctx["cta_ground"] = brand_ctx["frame"] = brand_ctx["accent"]
    label = brand_ctx["secondary"]
    dissolves = contrast(brand_ctx["accent"], brand_ctx["primary"]) < MIN_SHAPE_CONTRAST
    if name in CTA_ON_PANEL and dissolves:
        brand_ctx["cta_ground"] = brand_ctx["frame"] = brand_ctx["ink"]
        label = brand_ctx["primary"]
    brand_ctx["cta_ink"] = readable_on(brand_ctx["cta_ground"], label)
    return tpl.render(
        brief=brief,
        slide=slide,
        brand=brand_ctx,
        background=background_data_uri,
        text_dir=fonts.text_direction(slide.headline),
        width=w,
        height=h,
        **padding_for(w, h),
        # The plate's edge, from the same figures _make_legible sizes it with.
        plate_feather=round(w * PLATE_FEATHER),
        plate_blur=round(w * PLATE_BLUR),
        # The CTA belongs to the post, and on a carousel it earns its place on
        # the last slide only -- repeating it on every slide reads as a template.
        scrim_boost=scrim_boost,
        show_cta=bool(brief.cta)
        and (not brief.is_carousel() or slide.position == len(brief.slides)),
    )


# Runs in the page after fonts settle. Two jobs, in this order:
#
#   FIT     For the headline, the subhead, the CTA and the brand name: a binary
#           search for the LARGEST size between the type floor and the design
#           size at which the layout has no violations. Containers are
#           auto-height, so the text flows and the box follows it; nothing is
#           set in a fixed band and hoped to fit.
#   ASSERT  Every guarantee below is checked on real bounding boxes. Anything
#           left over is returned as a violation, and compose() refuses to
#           render. A collision is a bug, not a warning.
#
#             overflow:<box>       a container's content spills out of it
#             clipped:<el>         text wider than its own block
#             wordbreak:<el>       a word set across two lines ("Anniversar|y")
#             linegap:<el>         the ink of two lines of one element touches
#             too_many_lines:<el>  a headline past 4 lines, a subhead past 3,
#                                  a brand name past 2: a paragraph, not a post
#             hierarchy            the headline under 1.5x the subhead
#             tofu:<el>:<U+....>   a character no loaded face of ours can set
#             outside:<el>         an element leaves the canvas
#             unsafe:<el>          text or the mark outside the safe zone
#             overlap:<a>+<b>      two elements intersect
#             logo_clearspace:<x>  something inside the mark's clear space
#             logo_not_loaded      the mark would render as a hole
#
# Text is measured from its REAL INK, grapheme by grapheme. Horizontally that is
# a Range per grapheme -- an <h1> is as wide as its container however short
# the words are, and that is not what a reader sees -- widened by what the
# canvas's actualBoundingBox says the glyph's ink overhangs its advance box:
# the shirorekha of Devanagari, Bengali and Gurmukhi type starts a pixel or two
# left of the first letter's box, and measured from the Range alone it sat in
# the strip the safe zone exists to keep clear. Vertically it is the same
# actualBoundingBox for each grapheme hung from the line's baseline -- not the
# block box, which at line-height .98 is SHORTER than the letters (a "y" hung
# 15px below the safe zone with nothing to notice) and not the Range's height,
# which is the font's whole ascent+descent and called every large headline
# unsafe. Checked against rendered pixels for every face we ship, Latin and
# Indic: the two agree to within a pixel.
#
# Because the block box is not the ink, FIT also SETTLES each text element
# every time its size changes: it opens the leading when the ink of two lines
# would touch (shrinking cannot cure that -- the ink shrinks with the gap), and
# it pads the block on every side by exactly what the ink overshoots, so the
# layout positions the letters and not an abstraction of them.
FIT_JS = r"""
(cfg) => {
  const stage = document.querySelector('.stage');
  const S = stage.getBoundingClientRect();
  const W = S.width, H = S.height;
  const all = (s) => [...document.querySelectorAll(s)];
  const rel = (r) => ({l: r.left - S.left, t: r.top - S.top,
                       r: r.right - S.left, b: r.bottom - S.top});
  const boxOf = (el) => rel(el.getBoundingClientRect());
  const area = (b) => Math.max(0, b.r - b.l) * Math.max(0, b.b - b.t);
  const hit = (a, b, tol = 1) =>
    a.l < b.r - tol && b.l < a.r - tol && a.t < b.b - tol && b.t < a.b - tol;
  const grow = (b, d) => ({l: b.l - d, t: b.t - d, r: b.r + d, b: b.b + d});

  // ---- real ink ---------------------------------------------------------
  const TEXT = ['headline', 'subhead', 'brandline'];
  const graphemes = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
  const pen = document.createElement('canvas').getContext('2d');
  const REF = 200;  // glyph ink is measured once at this size and scaled
  const inkCache = new Map();
  const glyphInk = (face, g) => {
    const key = face + '\n' + g;
    let m = inkCache.get(key);
    if (!m) {
      pen.font = face;
      const t = pen.measureText(g);
      // a, d: ink above and below the baseline. l, r: ink beyond the advance
      // box on either side (never negative: ink inside the box is the box).
      m = {a: t.actualBoundingBoxAscent / REF, d: t.actualBoundingBoxDescent / REF,
           l: Math.max(0, t.actualBoundingBoxLeft) / REF,
           r: Math.max(0, t.actualBoundingBoxRight - t.width) / REF};
      inkCache.set(key, m);
    }
    return m;
  };
  // A line may end after a space, a hyphen or a dash. Anywhere else is the
  // middle of a word.
  const BREAK_OK = /[\s\-‐-—­​]$/;

  let version = 0;  // bumped on every style write; measurements are cached per version
  const seen = new Map();
  const measure = (el) => {
    const had = seen.get(el);
    if (had && had.version === version) return had.m;
    const cs = getComputedStyle(el);
    const px = parseFloat(cs.fontSize);
    const pitch = parseFloat(cs.lineHeight) || px;
    const family = `${cs.fontStyle} ${cs.fontWeight}`;
    pen.font = `${family} ${px}px ${cs.fontFamily}`;
    // A text fragment's rect starts one (rounded) font ascent above its baseline.
    const rise = Math.round(pen.measureText('x').fontBoundingBoxAscent);
    const face = `${family} ${REF}px ${cs.fontFamily}`;
    const upper = cs.textTransform === 'uppercase';
    const lines = [], breaks = [];
    let line = null, prev = '';
    const range = document.createRange();
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      for (const {segment: g, index} of graphemes.segment(node.data)) {
        if (g.trim()) {
          range.setStart(node, index);
          range.setEnd(node, index + g.length);
          const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0);
          if (rects.length) {
            const top = rects[0].top - S.top;
            if (!line || Math.abs(top - line.top) > pitch / 2) {
              if (line && prev && !BREAK_OK.test(prev)) breaks.push(prev + '|' + g);
              line = {top, base: top + rise, l: Infinity, r: -Infinity, a: 0, d: 0,
                      glyphs: [], text: ''};
              lines.push(line);
            }
            const ink = glyphInk(face, upper ? g.toUpperCase() : g);
            const glyph = {l: Math.min(...rects.map(r => r.left)) - S.left - ink.l * px,
                           r: Math.max(...rects.map(r => r.right)) - S.left + ink.r * px,
                           a: ink.a * px, d: ink.d * px};
            line.glyphs.push(glyph);
            line.l = Math.min(line.l, glyph.l); line.r = Math.max(line.r, glyph.r);
            line.a = Math.max(line.a, glyph.a); line.d = Math.max(line.d, glyph.d);
          }
        }
        if (line) line.text += g;
        prev = g;
      }
    }
    const block = boxOf(el);
    const box = lines.length
      ? {l: Math.min(...lines.map(n => n.l)), t: Math.min(...lines.map(n => n.base - n.a)),
         r: Math.max(...lines.map(n => n.r)), b: Math.max(...lines.map(n => n.base + n.d))}
      : block;
    const m = {px, pitch, lines, breaks, block, box};
    seen.set(el, {version, m});
    return m;
  };

  // The baseline distance two adjacent lines need: the deepest descender of the
  // upper line that sits OVER an ascender or mark of the lower one. Ink that is
  // not above other ink cannot touch it, however tight the leading.
  const pairNeed = (A, B, px) => {
    const near = 0.04 * px;
    let need = 0;
    for (const p of A.glyphs)
      for (const q of B.glyphs)
        if (p.l < q.r + near && q.l < p.r + near) need = Math.max(need, p.d + q.a);
    return need;
  };

  const lead0 = new Map();  // the template's own leading, unitless
  const settle = (el) => {
    let m = measure(el);
    if (!lead0.has(el)) lead0.set(el, m.pitch / m.px);
    const base = lead0.get(el);
    let need = 0;
    for (let i = 0; i + 1 < m.lines.length; i++)
      need = Math.max(need, pairNeed(m.lines[i], m.lines[i + 1], m.px) / m.px + cfg.lineGap);
    const want = need > base + 0.004
      ? Math.min(Math.ceil(need * 100 - 0.01) / 100, base + cfg.maxExtraLeading) : base;
    if (Math.abs(want - m.pitch / m.px) > 0.004) {
      el.style.lineHeight = want === base ? '' : want;
      version++;
      m = measure(el);
    }
    const cs = getComputedStyle(el);
    const pad = {top: parseFloat(cs.paddingTop) || 0, bottom: parseFloat(cs.paddingBottom) || 0,
                 left: parseFloat(cs.paddingLeft) || 0, right: parseFloat(cs.paddingRight) || 0};
    const over = {top: Math.max(0, Math.ceil(m.block.t + pad.top - m.box.t)),
                  bottom: Math.max(0, Math.ceil(m.box.b - (m.block.b - pad.bottom))),
                  left: Math.max(0, Math.ceil(m.block.l + pad.left - m.box.l)),
                  right: Math.max(0, Math.ceil(m.box.r - (m.block.r - pad.right)))};
    if (Object.keys(over).some(side => over[side] !== pad[side])) {
      for (const side of Object.keys(over))
        el.style['padding-' + side] = over[side] + 'px';
      version++;
    }
  };
  const settleAll = () => all(TEXT.map(c => '.' + c).join(',')).forEach(settle);

  // Every character must be set by a face WE loaded. One that falls through to
  // a system font is measured in nothing we ship and rendered differently on
  // every host; one that falls through to nothing is a box.
  const clean = (s) => s.trim().replace(/^["']|["']$/g, '');
  const rangesOf = (face) => (face.unicodeRange || 'U+0-10FFFF').split(',').map(part => {
    const m = part.trim().match(/^U\+([0-9A-F?]+)(?:-([0-9A-F]+))?$/i);
    if (!m) return null;
    return [parseInt(m[1].replace(/\?/g, '0'), 16),
            m[2] ? parseInt(m[2], 16) : parseInt(m[1].replace(/\?/g, 'F'), 16)];
  }).filter(Boolean);
  const loaded = [...document.fonts].filter(f => f.status === 'loaded')
    .map(f => ({family: clean(f.family), ranges: rangesOf(f)}));
  const invisible = (cp) => cp <= 0x20 || cp === 0xA0 || cp === 0xAD || cp === 0x2060
    || cp === 0xFEFF || (cp >= 0x2000 && cp <= 0x200F) || (cp >= 0x2028 && cp <= 0x202F);
  const tofu = [];
  if (cfg.tofu) {
    for (const cls of [...TEXT, 'cta']) {
      for (const el of all('.' + cls)) {
        const cs = getComputedStyle(el);
        const stack = cs.fontFamily.split(',').map(clean);
        const faces = loaded.filter(f => stack.includes(f.family));
        const text = cs.textTransform === 'uppercase'
          ? el.textContent.toUpperCase() : el.textContent;
        for (const ch of text) {
          const cp = ch.codePointAt(0);
          if (invisible(cp)) continue;
          if (!faces.some(f => f.ranges.some(([lo, hi]) => cp >= lo && cp <= hi)))
            tofu.push(`tofu:${cls}:U+${cp.toString(16).toUpperCase().padStart(4, '0')}`);
        }
      }
    }
  }

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

  const elements = () => {
    const out = [];
    for (const cls of ['headline', 'subhead', 'cta', 'logo', 'brandline', 'rule']) {
      for (const el of all('.' + cls)) {
        const box = TEXT.includes(cls) ? measure(el).box : boxOf(el);
        if (area(box) > 0) out.push({cls, el, box});
      }
    }
    return out;
  };

  const violations = () => {
    const v = [...tofu];
    for (const b of all('.content, .panel, .band, .card')) {
      const name = b.className.split(' ')[0];
      if (b.scrollHeight > b.clientHeight + 1 || b.scrollWidth > b.clientWidth + 1)
        v.push('overflow:' + name);
      const r = boxOf(b);
      if (r.l < -1 || r.t < -1 || r.r > W + 1 || r.b > H + 1) v.push('outside:' + name);
    }
    const els = elements();
    const safe = {l: cfg.safe.x, t: cfg.safe.top, r: W - cfg.safe.x, b: H - cfg.safe.bottom};
    const px = {};
    for (const e of els) {
      if (e.cls === 'cta' && e.el.scrollWidth > e.el.clientWidth + 1) v.push('clipped:cta');
      if (TEXT.includes(e.cls)) {
        const m = measure(e.el);
        px[e.cls] = m.px;
        // A word wider than its block now OVERFLOWS it (nothing breaks words any
        // more), and that overflow is what makes the search shrink the type.
        if (m.lines.some(n => n.l < m.block.l - 1 || n.r > m.block.r + 1)
            || e.el.scrollWidth > e.el.clientWidth + 1) v.push('clipped:' + e.cls);
        if (m.breaks.length) v.push('wordbreak:' + e.cls);
        if (m.lines.length > cfg.maxLines[e.cls]) v.push('too_many_lines:' + e.cls);
        for (let i = 0; i + 1 < m.lines.length; i++) {
          const A = m.lines[i], B = m.lines[i + 1];
          if (B.base - A.base - pairNeed(A, B, m.px) < cfg.lineGap * m.px - 0.75)
            v.push('linegap:' + e.cls);
        }
      }
      const b = e.box;
      if (b.l < -1 || b.t < -1 || b.r > W + 1 || b.b > H + 1) v.push('outside:' + e.cls);
      else if (e.cls !== 'rule' && (b.l < safe.l - 1 || b.r > safe.r + 1
                                    || b.t < safe.t - 1 || b.b > safe.b + 1))
        v.push('unsafe:' + e.cls);
    }
    // The headline must still read as the headline once both have been fitted.
    if (px.headline && px.subhead && px.headline < cfg.hierarchy * px.subhead - 0.01)
      v.push('hierarchy');
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

  // The sized elements, each with a design size (MAX) and a floor (MIN). The
  // brand name is one of them: it used to be fixed, so a long shop name was a
  // violation nothing could search its way out of.
  const parts = [['headline', cfg.min.headline], ['subhead', cfg.min.subhead],
                 ['cta', cfg.min.cta], ['brandline', cfg.min.brandline]]
    .map(([cls, min]) => {
      const el = document.querySelector('.' + cls);
      if (!el) return null;
      const design = parseFloat(getComputedStyle(el).fontSize);
      return {cls, el, design, min: Math.min(min, design), px: design, steps: 0};
    }).filter(Boolean);
  const set = (p, px) => {
    p.px = px; p.el.style.fontSize = px + 'px'; version++;
    settleAll(); reflow();
  };
  const ok = () => violations().length === 0;
  const half = (x) => Math.floor(x * 2) / 2;

  settleAll(); reflow();
  if (!ok() && parts.length) {
    // Pass 1: one scale for all of them, MIN..MAX together. Binary search for the
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
      // its design size. The headline goes first, so the subhead can only
      // regrow as far as the hierarchy allows.
      for (const p of parts) {
        let a = p.px, b = p.design;
        // The design size itself first: an element that never needed to give
        // anything up gets ALL of it back, not the half-pixel short of it a
        // bisection converges to.
        set(p, b); p.steps++;
        if (ok()) continue;
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

  // What the legibility pass needs to measure the rendered frame: where the
  // ink of every piece of type is, line by line, what colour it really is
  // once every opacity above it has been multiplied in, and whether it sits
  // on something with a solid background of its own (a panel, a band, the
  // pill) rather than on the photograph.
  const solidBehind = (el) => {
    for (let n = el; n && n !== stage; n = n.parentElement) {
      const bg = getComputedStyle(n).backgroundColor.match(/[\d.]+/g);
      if (bg && (bg.length < 4 || parseFloat(bg[3]) >= 0.999)) return true;
    }
    return false;
  };
  const inks = [];
  for (const cls of [...TEXT, 'cta']) {
    for (const el of all('.' + cls)) {
      const m = measure(el);
      if (!m.lines.length) continue;
      const cs = getComputedStyle(el);
      let opacity = 1;
      for (let n = el; n && n !== stage; n = n.parentElement)
        opacity *= parseFloat(getComputedStyle(n).opacity);
      inks.push({cls, box: m.box, color: cs.color, opacity, px: m.px,
                 leading: Math.round(m.pitch / m.px * 100) / 100,
                 lines: m.lines.map(n => n.text.trim()),
                 rows: m.lines.map(n => [n.l, n.base - n.a, n.r, n.base + n.d]),
                 solid: solidBehind(el)});
    }
  }
  const boxes = {};
  for (const e of elements()) boxes[e.cls] = e.box;
  const mark = document.querySelector('img.logo');
  return {violations: violations(), sizes, boxes, inks, canvas: [W, H],
          logo_solid: mark ? solidBehind(mark) : false};
}
"""

# Sets the gradient's measured strength and lays the plates the legibility
# pass asked for. A plate lives INSIDE the positioned container that holds the
# words it serves (z-index -1 in an isolated stacking context): above that
# container's ground, below its type -- which is what puts a plate over
# top_band's photograph and under its foot, and a logo's plate over a brand
# panel instead of behind it.
SCRIM_JS = r"""
({k, plates, marks}) => {
  const stage = document.querySelector('.stage');
  const S = stage.getBoundingClientRect();
  const scrim = document.querySelector('.scrim');
  if (scrim && k !== null) scrim.style.setProperty('--k', k);
  document.querySelectorAll('.scrim-text, .mark-plate').forEach(e => e.remove());
  const lay = (kind, spec) => {
    const anchor = document.querySelector('.' + spec.anchor);
    if (!anchor) return null;
    const host = anchor.offsetParent && stage.contains(anchor.offsetParent)
      ? anchor.offsetParent : stage;
    host.style.isolation = 'isolate';
    const hb = host.getBoundingClientRect();
    const el = document.createElement('div');
    el.className = kind;
    el.style.left = (S.left + spec.box[0] - hb.left - host.clientLeft) + 'px';
    el.style.top = (S.top + spec.box[1] - hb.top - host.clientTop) + 'px';
    el.style.width = (spec.box[2] - spec.box[0]) + 'px';
    el.style.height = (spec.box[3] - spec.box[1]) + 'px';
    el.style.background = spec.colour;
    el.style.opacity = spec.alpha;
    host.appendChild(el);
    return el;
  };
  for (const p of plates) lay('scrim-text', p);
  for (const m of marks) {
    const el = lay('mark-plate', m);
    if (!el) continue;
    el.style.borderRadius = m.radius + 'px';
    // On a plate the mark needs no shadow to separate it, and an opaque logo's
    // shadow is a grey rectangle drawn on its own card.
    document.querySelector('.' + m.anchor).style.filter = 'none';
  }
}
"""

# The frame with its ink hidden (and, with `mark` false, the logo too), so the
# ground behind each element can be read off the rendered page.
HIDE_JS = """
(classes) => {
  const stage = document.querySelector('.stage');
  stage.classList.remove('no-type', 'no-mark');
  if (classes.length) stage.classList.add(...classes);
}
"""

# Which faces the creative is ACTUALLY set in, read off the DOM, and whether
# they arrived. `document.fonts.check` is no use here: it answers true for a
# family that does not exist at all.
#
# This used to be worked out in Python from the brief ("there is a CTA, so the
# body face is needed"), and it was wrong: a carousel's CTA renders on the last
# slide only, so slide 1 of the prompt's own example -- a logo brand, no
# subhead -- used Inter nowhere, Chromium never fetched it, and the render was
# refused for a face it did not need. The page knows what it rendered.
#
# For every non-empty text element, the FIRST family in its computed stack is
# the brand's face. It must exist at the weight the element is set in, and
# every character it declares it can set must have arrived. A character it
# does NOT declare (Kannada in Poppins) is the script face's job; whether some
# loaded face of ours covers it is FIT_JS's `tofu` check. Poppins carries
# Devanagari, so a Hindi headline never touches Noto Sans Devanagari --
# demanding that it load refused perfectly good renders.
FONTS_JS = r"""
() => {
  const clean = (s) => s.trim().replace(/^["']|["']$/g, '');
  const covers = (face, cp) => (face.unicodeRange || 'U+0-10FFFF').split(',').some(part => {
    const m = part.trim().match(/^U\+([0-9A-F?]+)(?:-([0-9A-F]+))?$/i);
    if (!m) return false;
    const lo = parseInt(m[1].replace(/\?/g, '0'), 16);
    const hi = m[2] ? parseInt(m[2], 16) : parseInt(m[1].replace(/\?/g, 'F'), 16);
    return cp >= lo && cp <= hi;
  });
  const weights = (face) => {
    const w = String(face.weight).split(/\s+/)
      .map(x => x === 'normal' ? 400 : x === 'bold' ? 700 : parseFloat(x));
    return [w[0], w[w.length - 1]];
  };
  const faces = [...document.fonts];
  const missing = [];
  for (const el of document.querySelectorAll('.headline, .subhead, .cta, .brandline')) {
    const cs = getComputedStyle(el);
    const raw = el.textContent.trim();
    if (!raw) continue;
    const text = cs.textTransform === 'uppercase' ? raw.toUpperCase() : raw;
    const family = clean(cs.fontFamily.split(',')[0]);
    const weight = parseFloat(cs.fontWeight);
    const mine = faces.filter(f => clean(f.family) === family);
    if (!mine.length) { missing.push(family); continue; }
    const cut = mine.filter(f => weights(f)[0] <= weight && weight <= weights(f)[1]);
    if (!cut.length) { missing.push(`${family} ${weight}`); continue; }
    for (const ch of new Set(text)) {
      const cp = ch.codePointAt(0);
      if (cp <= 0x20) continue;
      const declared = cut.filter(f => covers(f, cp));
      if (declared.length && !declared.some(f => f.status === 'loaded')) {
        missing.push(`${family} ${weight}`);
        break;
      }
    }
  }
  return [...new Set(missing)];
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
        # Written fast, not small: this PNG never leaves the process -- it goes
        # straight to export_jpeg -- and optimize=True spent 1.3s per slide
        # shrinking a file nobody stores.
        im.convert("RGB").resize((width, height), Image.LANCZOS).save(
            out, format="PNG", compress_level=1
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
    "overflow:panel", ...). The cure is always upstream -- usually shorter copy,
    sometimes the brand's name or mark (see pipeline.layout_gate, which tells
    the two apart).
    """

    def __init__(self, violations: list[str], *, position: int | None = None) -> None:
        self.violations = list(violations)
        self.position = position
        super().__init__(f"layout guarantees not met: {', '.join(self.violations)}")


class LegibilityError(LayoutError):
    """Type (or the mark) cannot be made to separate from what is behind it.

    Raised from the RENDERED frame, after the plates have been strengthened as
    far as they go. `measured` is {element: contrast ratio} for what fell
    short; text is held to 4.5:1 and the logo to 3:1.
    """

    def __init__(self, measured: dict[str, float], *, position: int | None = None) -> None:
        self.measured = {k: round(float(v), 2) for k, v in measured.items()}
        super().__init__([f"contrast:{cls}" for cls in self.measured], position=position)
        found = ", ".join(f"{cls} {ratio}:1" for cls, ratio in self.measured.items())
        self.args = (f"legibility guarantee not met on the rendered frame: {found}",)


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
            "brandline": round(w * BRANDLINE_MIN),
        },
        "maxLines": MAX_LINES,
        "hierarchy": HIERARCHY,
        "lineGap": LINE_GAP,
        "maxExtraLeading": MAX_EXTRA_LEADING,
        # Coverage can only be judged against faces that were required to load.
        "tofu": bool(settings.compose_require_fonts),
        "logoClear": LOGO_CLEAR,
    }


_FONT_TYPES = {".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}


async def _serve_fonts(route) -> None:
    """Answer the compositor's private font host from templates/fonts/."""
    name = route.request.url.rsplit("/", 1)[-1].split("?", 1)[0]
    path = fonts.LOCAL_DIR / name
    kind = _FONT_TYPES.get(path.suffix)
    # A bare filename of a known type, inside the directory: nothing else is served.
    if not kind or "/" in name or "\\" in name or ".." in name or not path.is_file():
        await route.fulfill(status=404, body="")
        return
    await route.fulfill(
        status=200,
        body=path.read_bytes(),
        content_type=kind,
        headers={"access-control-allow-origin": "*", "cache-control": "max-age=31536000"},
    )


async def _layout(page, brief: CreativeBrief, slide: Slide, brand: Any, html: str) -> dict:
    """Load the page, prove the fonts, fit the type, assert the guarantees."""
    template = brief.template_for(slide)
    await page.route(f"{fonts.LOCAL_HOST}/**", _serve_fonts)
    # The document is set immediately; the wait is for stylesheets and fonts.
    # Bounded twice -- here and on fonts.ready -- so a font host that stalls
    # costs seconds. What happens next is not a fallback face: see below.
    try:
        await page.set_content(html, wait_until="networkidle", timeout=int(FONT_WAIT_S * 1000))
    except Exception:  # noqa: BLE001 - playwright's TimeoutError
        log.warning("page_not_idle", template=template)
    # Every vendored face is `font-display: block`: a face still loading paints
    # INVISIBLE text, and FIT_JS would measure a face that swaps in later. So
    # fonts that have not settled are a failure, not a warning -- after one more
    # wait, because a cold font route under six parallel slides can be slow once.
    for attempt in (1, 2):
        try:
            settled = page.evaluate("document.fonts.ready.then(() => true)")
            await asyncio.wait_for(settled, FONT_WAIT_S)
            break
        except TimeoutError:
            log.warning("fonts_not_settled", template=template, attempt=attempt)
            if attempt == 2 and settings.compose_require_fonts:
                raise BrandFontUnavailable(
                    f"fonts did not settle within {2 * FONT_WAIT_S:.0f}s"
                ) from None

    if settings.compose_require_fonts:
        missing = await page.evaluate(FONTS_JS)
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


_CSS_RGB = re.compile(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?")

# Words closer than this (of the canvas width) share a plate: a headline and
# the subhead under it are one block of type. Anything further apart gets its
# own -- poster_stack sets its headline at the top and its CTA and brand name
# at the foot, and one plate for "the text" was a dark slab over the middle of
# the photograph, exactly where the product is and no words are.
CLUSTER_GAP = 0.07
# How far a plate reaches past the ink it serves; twice .scrim-text's blur, so
# the plate is still at ~97% under the last letter. Wide and soft rather than
# tight and hard: at .06 with a .03 blur the plate on a white photograph was
# a grey box with the words in it -- legible, and the first thing the owner
# would ask to have removed. At .10 with a .05 blur the same strength reads
# as a shadow the words sit in, and the picture shows through its edge.
PLATE_FEATHER = 0.10
PLATE_BLUR = 0.05
# The logo's plate stays inside its clear space (LOGO_CLEAR), which FIT_JS has
# already proven empty and on the canvas.
MARK_PLATE_PAD = 0.8 * LOGO_CLEAR
MARK_PLATE_ALPHA = 0.94


def _ink_of(item: dict) -> tuple[float, float]:
    """(relative luminance, effective opacity) of one measured piece of type."""
    m = _CSS_RGB.match(item.get("color") or "")
    if not m:
        return 1.0, float(item.get("opacity", 1.0))
    rgb = tuple(float(m.group(i)) for i in (1, 2, 3))
    alpha = float(m.group(4)) if m.group(4) else 1.0
    return legibility.relative_luminance(rgb), alpha * float(item.get("opacity", 1.0))


def _edges(box: dict) -> tuple[float, float, float, float]:
    return box["l"], box["t"], box["r"], box["b"]


def _union(items: list[dict]) -> tuple[float, float, float, float]:
    boxes = [_edges(i["box"]) for i in items]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _clusters(words: list[dict], width: int) -> list[list[dict]]:
    """Group the measured type into blocks that read as one."""
    groups: list[list[dict]] = []
    for item in sorted(words, key=lambda i: i["box"]["t"]):
        left, top, right, _ = _edges(item["box"])
        for group in groups:
            g = _union(group)
            if top - g[3] <= width * CLUSTER_GAP and min(g[2], right) > max(g[0], left):
                group.append(item)
                break
        else:
            groups.append([item])
    return groups


async def _frame(page, clip: tuple[float, float, float, float], *hidden: str) -> Image.Image:
    """The page as it will ship, minus the ink (and the mark, if asked), at one
    pixel per CSS pixel whatever the page's device scale."""
    await page.evaluate(HIDE_JS, list(hidden))
    try:
        area = {"x": clip[0], "y": clip[1], "width": clip[2] - clip[0], "height": clip[3] - clip[1]}
        shot = await page.screenshot(type="png", scale="css", clip=area)
    finally:
        await page.evaluate(HIDE_JS, [])
    with Image.open(BytesIO(shot)) as im:
        return im.convert("RGB")


async def _make_legible(
    page, report: dict, template: str, fitted: bytes, size: tuple[int, int], position: int
) -> dict:
    """Measure the ground behind every word ON THE RENDERED FRAME and hold it to
    4.5:1 (the logo to 3:1), strengthening a plate under whatever falls short.

    Returns the contrast found behind each element and the plates it took to
    get there. Raises LegibilityError when a plate at full strength still
    cannot deliver it. White type over a white photograph
    used to ship at 2.78:1 because nothing ever looked at the frame.
    """
    w, h = size
    words = [i for i in report["inks"] if i["cls"] != "cta"]
    groups = _clusters(words, w)
    k = None
    if template in TYPE_OVER_PHOTO and groups:
        # The prediction, from the photograph: how hard the gradient works.
        guesses = [
            await asyncio.to_thread(
                legibility.scrim_for_box,
                fitted,
                tuple(v / d for v, d in zip(_union(g), (w, h, w, h), strict=True)),
            )
            for g in groups
        ]
        k, why = max(guesses, key=lambda guess: guess[0])
        if k != 1.0:
            log.info("scrim_measured", template=template, **why)

    alphas = [0.0] * len(groups)
    logo = report["boxes"].get("logo")
    logo_solid = bool(report.get("logo_solid"))
    looks: dict | None = None
    mark: dict | None = None
    feather = w * PLATE_FEATHER
    short: dict[str, float] = {}
    for attempt in range(LEGIBILITY_ROUNDS + 1):
        plates = []
        for group, alpha in zip(groups, alphas, strict=True):
            if alpha:
                g = _union(group)
                ink, _ = _ink_of(group[0])
                plates.append(
                    {
                        "anchor": group[0]["cls"],
                        "box": [g[0] - feather, g[1] - feather, g[2] + feather, g[3] + feather],
                        "colour": legibility.plate_colour(ink),
                        "alpha": alpha,
                    }
                )
        await page.evaluate(SCRIM_JS, {"k": k, "plates": plates, "marks": [mark] if mark else []})
        ground = await _frame(page, (0, 0, w, h), "no-type", "no-mark")

        found: dict[str, float] = {}
        short, stats_for = {}, {}
        for item in report["inks"]:
            ink, opacity = _ink_of(item)
            stats = legibility.ground(ground, item["rows"], item["px"])
            found[item["cls"]] = legibility.contrast_on(ink, opacity, stats)
            stats_for[item["cls"]] = stats
            # A designed ground (panel, pill) gets the measurement slack; a
            # photograph or gradient is held to the bar and plated.
            slack = legibility.MEASURE_SLACK if legibility.designed(item["solid"], stats) else 0.0
            if found[item["cls"]] < legibility.TEXT_CONTRAST - slack:
                short[item["cls"]] = found[item["cls"]]

        changed = False
        if logo:
            box = _edges(logo)
            if looks is None:
                with_mark = await _frame(page, box, "no-type")
                looks = legibility.mark_stats(
                    with_mark, ground.crop(tuple(round(v) for v in box)), (0, 0, *with_mark.size)
                )
                if looks["opaque"]:
                    mark = _mark_plate(logo, looks["edge"], 1.0)
                    changed = True
            if not looks["opaque"]:
                stats = legibility.ground(ground, [box], box[3] - box[1])
                found["logo"] = legibility.contrast_on(looks["luminance"], 1.0, stats)
                slack = legibility.MEASURE_SLACK if legibility.designed(logo_solid, stats) else 0
                if found["logo"] < legibility.MARK_CONTRAST - slack:
                    short["logo"] = found["logo"]

        if not short and not changed:
            if any(alphas) or mark:
                log.info("legibility_plated", template=template, plates=alphas, mark=bool(mark))
            return {
                "contrast": {cls: round(ratio, 2) for cls, ratio in found.items()},
                "plates": plates,
                "mark_plate": mark,
            }
        if attempt == LEGIBILITY_ROUNDS or (short and set(short) <= {"cta"}):
            # The CTA's ground is its own pill: no plate can change it. The
            # other plates are still laid first, so a refusal reports what a
            # plate could NOT fix, not a headline number one round would have.
            break
        for n, group in enumerate(groups):
            # Every ground in this group was measured under the plate as it is
            # NOW; the group gets the strongest answer any of its words needs.
            under = alphas[n]
            for item in group:
                if item["cls"] in short:
                    ink, _ = _ink_of(item)
                    need = legibility.plate_alpha(
                        ink, stats_for[item["cls"]], under, legibility.TEXT_CONTRAST
                    )
                    alphas[n] = max(alphas[n], need)
        if "logo" in short:
            on_white = legibility.contrast_ratio(looks["luminance"], 1.0)
            on_dark = legibility.contrast_ratio(looks["luminance"], _luminance(_DARK))
            colour = _LIGHT if on_white >= on_dark else _DARK
            mark = _mark_plate(logo, colour, 1.0 if mark else MARK_PLATE_ALPHA)
    if all(v >= legibility.bar_for(cls) - legibility.MEASURE_SLACK for cls, v in short.items()):
        # Every plate is as strong as it goes and what is left is rounding:
        # 4.48 read off 8-bit pixels is not a frame the owner can tell from 4.5.
        log.info("legibility_within_slack", template=template, measured=short)
        return {
            "contrast": {cls: round(ratio, 2) for cls, ratio in found.items()},
            "plates": plates,
            "mark_plate": mark,
        }
    log.error("legibility_refused", template=template, position=position, measured=short)
    raise LegibilityError(short, position=position)


def _mark_plate(logo: dict, colour: str, alpha: float) -> dict:
    pad = MARK_PLATE_PAD * (logo["b"] - logo["t"])
    return {
        "anchor": "logo",
        "box": [logo["l"] - pad, logo["t"] - pad, logo["r"] + pad, logo["b"] + pad],
        "colour": colour,
        "alpha": alpha,
        "radius": round(pad * 1.2, 1),
    }


async def _compose_once(
    brief: CreativeBrief,
    slide: Slide,
    brand: Any,
    background: bytes,
    background_mime: str = "image/jpeg",
) -> tuple[bytes, dict]:
    """The finished PNG for one slide (a single post is slide 1 of 1), and the
    report of what was fitted and measured to make it.

    Raises LayoutError / LegibilityError / BrandFontUnavailable instead of
    returning a frame that breaks a guarantee. There is no degraded output.
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
        report["legibility"] = await _make_legible(
            page, report, template, fitted, (w, h), slide.position
        )
        shot = await page.screenshot(type="png", clip={"x": 0, "y": 0, "width": w, "height": h})
    finally:
        await page.close()
    return await asyncio.to_thread(_resample, shot, w, h), report


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
    png, _ = await compose_with_report(brief, slide, brand, background, background_mime)
    return png


async def compose_with_report(
    brief: CreativeBrief,
    slide: Slide,
    brand: Any,
    background: bytes,
    background_mime: str = "image/jpeg",
) -> tuple[bytes, dict]:
    """compose(), plus what was measured on the way: FIT_JS's report with the
    contrast found behind every element of the rendered frame (`legibility`)."""
    return await _surviving_a_crash(_compose_once, brief, slide, brand, background, background_mime)


async def compose_to_file(
    brief: CreativeBrief, slide: Slide, brand: Any, background: bytes, out: Path
) -> Path:
    out.write_bytes(await compose(brief, slide, brand, background))
    return out
