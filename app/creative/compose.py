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
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.creative.brief import CreativeBrief, Slide
from app.logging import get_logger

log = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "creative"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

class TextDoesNotFit(RuntimeError):
    """The copy cannot be made to fit even at the minimum font size."""


TEMPLATES = {
    "centered_overlay": "centered_overlay.html.j2",
    "lower_third": "lower_third.html.j2",
    "split_card": "split_card.html.j2",
}
DEFAULT_TEMPLATE = "centered_overlay"

_browser = None
_playwright = None
_lock = asyncio.Lock()


async def get_browser():
    global _browser, _playwright
    async with _lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright

            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"]
            )
            log.info("chromium_launched")
    return _browser


async def shutdown() -> None:
    global _browser, _playwright
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None


def _brand_context(brand: Any) -> dict[str, Any]:
    palette = dict(getattr(brand, "palette", {}) or {})
    fonts = dict(getattr(brand, "fonts", {}) or {})
    return {
        "name": getattr(brand, "name", ""),
        # Prefer the inlined data URI; fall back to the remote URL, then to text.
        "logo_url": getattr(brand, "logo_src", None) or getattr(brand, "logo_url", None),
        "primary": palette.get("primary", "#111111"),
        "secondary": palette.get("secondary", "#FFFFFF"),
        "accent": palette.get("accent", "#E4572E"),
        "ink": palette.get("ink", "#FFFFFF"),
        "heading_font": fonts.get("heading", "Poppins"),
        "body_font": fonts.get("body", "Inter"),
        "google_fonts": fonts.get("google_fonts_href"),
    }


def render_html(
    brief: CreativeBrief, slide: Slide, brand: Any, background_data_uri: str
) -> str:
    name = brief.template_for(slide)
    tpl = _env.get_template(TEMPLATES.get(name, TEMPLATES[DEFAULT_TEMPLATE]))
    w, h = brief.pixel_size()
    return tpl.render(
        brief=brief,
        slide=slide,
        brand=_brand_context(brand),
        background=background_data_uri,
        width=w,
        height=h,
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
() => {
  const stage = document.querySelector('.stage');
  const boxes = [...document.querySelectorAll('.content, .panel')];
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
      el.style.fontSize = (size * 0.94) + 'px';
      n++;
    }
    return n;
  };
  const head = shrink('.headline', 28, 30);
  const sub = boxes.some(b => !fits(b)) ? shrink('.subhead', 18, 20) : 0;
  return {headline_steps: head, subhead_steps: sub,
          fits: boxes.every(b => fits(b))};
}
"""


def as_data_uri(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"


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
    page = await browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
    try:
        await page.set_content(html, wait_until="networkidle")
        # Webfonts settle after networkidle on slow links; a short wait beats
        # shipping a creative in a fallback face.
        try:
            await page.evaluate("document.fonts.ready")
        except Exception:  # noqa: BLE001
            pass

        try:
            fit = await page.evaluate(FIT_JS)
        except Exception:  # noqa: BLE001
            fit = {"fits": None}
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

        return await page.screenshot(type="png", clip={"x": 0, "y": 0, "width": w, "height": h})
    finally:
        await page.close()


async def compose_to_file(
    brief: CreativeBrief, slide: Slide, brand: Any, background: bytes, out: Path
) -> Path:
    out.write_bytes(await compose(brief, slide, brand, background))
    return out
