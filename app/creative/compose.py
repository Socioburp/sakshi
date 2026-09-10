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

from app.creative import fonts
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
        "pad_x": max(int(round(width * 0.078)), x_in + margin),
        "pad_top": max(int(round(width * 0.075)), y_in + margin),
        "pad_bottom": max(int(round(width * 0.085)), y_in + margin),
    }


# Supersampling dial, measured rather than assumed.
#
# Rendering at 2x and resampling down does give visibly crisper stems and a
# cleaner logo -- but on this workload it cost 1.1s -> 4.7s per slide, and a
# six-slide carousel composes in parallel, so on a small instance it turned a
# ~1s step into ~28s. That breaks the promise the product is built on: a
# creative back inside 30-60 seconds. v1 already died of minute-long carousels.
#
# So it ships at 1. The type quality in this file comes from the templates --
# leading, tracking, an eased scrim, grain -- not from brute pixels. Raise this
# to 2 for a print-resolution one-off, never for the WhatsApp path.
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
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"]
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


def render_html(brief: CreativeBrief, slide: Slide, brand: Any, background_data_uri: str) -> str:
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
        show_cta=bool(brief.cta)
        and (not brief.is_carousel() or slide.position == len(brief.slides)),
        slide_count=len(brief.slides) if brief.is_carousel() else 1,
    )


# Runs in the page after fonts settle. Shrinks the headline (then the subhead)
# until nothing spills out of its box. A cropped headline is the single most
# visible way a creative looks broken, and it is entirely preventable: the
# browser already knows the rendered size, so measure it instead of hoping the
# copy limit was tight enough.
FIT_JS = """
([xi, yi]) => {
  const stage = document.querySelector('.stage');
  const boxes = [...document.querySelectorAll('.content, .panel, .band, .card')];
  const fits = (el) => {
    const s = stage.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const inside = r.top >= s.top - 1 && r.bottom <= s.bottom + 1
                && r.left >= s.left - 1 && r.right <= s.right + 1;
    return inside && el.scrollHeight <= el.clientHeight + 1
                  && el.scrollWidth <= el.clientWidth + 1;
  };
  const shrink = (sel, floorPx, steps) => {
    const el = document.querySelector(sel);
    if (!el) return 0;
    let n = 0;
    while (n < steps && boxes.some(b => !fits(b))) {
      const size = parseFloat(getComputedStyle(el).fontSize);
      if (size <= floorPx) break;
      el.style.fontSize = (size * 0.97) + 'px';
      n++;
    }
    return n;
  };
  const head = shrink('.headline', 30, 60);
  const sub = boxes.some(b => !fits(b)) ? shrink('.subhead', 20, 40) : 0;
  // Grid safety: everything a reader must see sits inside the 3:4 centre crop.
  const s = stage.getBoundingClientRect();
  const must = [...document.querySelectorAll('.headline, .subhead, .cta, .logo, .brandline')];
  const unsafe = must.filter(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    return r.left < s.left + xi - 1 || r.right > s.right - xi + 1
        || r.top < s.top + yi - 1 || r.bottom > s.bottom - yi + 1;
  }).map(el => el.className);
  return {headline_steps: head, subhead_steps: sub,
          fits: boxes.every(b => fits(b)), grid_safe: unsafe.length === 0, unsafe};
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


async def compose(
    brief: CreativeBrief,
    slide: Slide,
    brand: Any,
    background: bytes,
    background_mime: str = "image/jpeg",
) -> bytes:
    """Return the finished PNG for one slide (a single post is slide 1 of 1)."""
    html = render_html(brief, slide, brand, as_data_uri(background, background_mime))
    w, h = brief.pixel_size()
    browser = await get_browser()
    page = await browser.new_page(
        viewport={"width": w, "height": h}, device_scale_factor=SUPERSAMPLE
    )
    try:
        # The document is set immediately; the wait is for stylesheets and
        # fonts. Bounded twice -- here and on fonts.ready -- because a font
        # host that stalls must cost seconds, not the creative: the page is
        # rendered in the fallback face instead.
        try:
            await page.set_content(html, wait_until="networkidle", timeout=int(FONT_WAIT_S * 1000))
        except Exception:  # noqa: BLE001 - playwright's TimeoutError
            log.warning("page_not_idle", template=brief.template_for(slide))
        try:
            await asyncio.wait_for(page.evaluate("document.fonts.ready"), FONT_WAIT_S)
        except Exception:  # noqa: BLE001
            log.warning("fonts_not_settled", template=brief.template_for(slide))

        try:
            fit = await page.evaluate(FIT_JS, list(grid_insets(w, h)))
        except Exception:  # noqa: BLE001
            fit = {"fits": None}
        if fit.get("grid_safe") is False:
            # Not fatal -- the post itself is whole -- but a thumbnail with a
            # clipped headline is what makes a grid look amateur.
            log.warning(
                "grid_unsafe", template=brief.template_for(slide), elements=fit.get("unsafe")
            )
        if fit.get("headline_steps") or fit.get("subhead_steps"):
            log.info(
                "text_autofit",
                headline_steps=fit.get("headline_steps"),
                subhead_steps=fit.get("subhead_steps"),
                fits=fit.get("fits"),
            )
        if fit.get("fits") is False:
            # Shipping a knowingly-cropped creative is worse than failing here:
            # the client sees it, and so does their audience.
            raise TextDoesNotFit(
                f"copy still overflows at minimum size: headline={slide.headline!r}"
            )

        shot = await page.screenshot(type="png", clip={"x": 0, "y": 0, "width": w, "height": h})
        return _resample(shot, w, h)
    finally:
        await page.close()


async def compose_to_file(
    brief: CreativeBrief, slide: Slide, brand: Any, background: bytes, out: Path
) -> Path:
    out.write_bytes(await compose(brief, slide, brand, background))
    return out
