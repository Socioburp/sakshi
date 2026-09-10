"""Six layouts, three aspects, one brand kit: everything fits and is grid-safe."""

from __future__ import annotations

import json
import types

import pytest

from app.creative import brandkit, compose
from app.creative.brief import EXAMPLE, CreativeBrief
from app.creative.imagegen.base import ImageRequest
from app.creative.imagegen.providers import MockImageProvider

LONG_HEADLINE = "Cold-pressed groundnut oil, back in stock this weekend"
LONG_SUBHEAD = "Small batches, pressed on Tuesdays and Fridays in Indiranagar"


def test_grid_insets_follow_the_3_4_profile_grid():
    assert compose.grid_insets(1080, 1350) == (34, 0)  # 4:5 loses a thin strip each side
    assert compose.grid_insets(1080, 1080) == (135, 0)  # 1:1 loses 12.5% each side
    assert compose.grid_insets(1080, 1920) == (0, 240)  # 9:16 loses a band top and bottom
    pad = compose.padding_for(1080, 1080)
    assert pad["pad_x"] >= 135 + 32 and pad["pad_top"] == 81
    tall = compose.padding_for(1080, 1920)
    assert tall["pad_top"] >= 240 + 32 and tall["pad_bottom"] >= 240 + 32


def test_brand_kit_is_picked_from_the_category():
    assert brandkit.pick("premium skincare").key == "editorial"
    assert brandkit.pick("family bakery").key == "warm"
    assert brandkit.pick("mobile repair shop").key == "bold"
    assert brandkit.pick("stationery shop").key == "clean"
    assert brandkit.pick(None).key == "clean"
    brand = types.SimpleNamespace(category="jewellery", fonts={}, template_prefs={})
    look = brandkit.apply(brand)
    assert look.key == "editorial"
    assert brand.fonts["heading"] == "Playfair Display"
    assert brand.template_prefs["look"] == "editorial"
    assert brand.template_prefs["signature"] == "none"
    brandkit.apply(brand, "bold")
    assert brand.fonts["heading"] == "Manrope" and brand.template_prefs["signature"] == "bar"
    for look in brandkit.LOOKS.values():
        assert all(t in compose.TEMPLATES for t in look.family)


def _brand(signature: str):
    return types.SimpleNamespace(
        name="Kadamba Naturals",
        palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
        fonts={"heading": "Poppins", "body": "Inter"},
        logo_analysis={},
        logo_url=None,
        logo_src=None,
        template_prefs={"signature": signature},
    )


def test_signatures_render_once_and_only_when_chosen():
    brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": "lower_third"})
    slide = brief.units()[0]
    html = compose.render_html(brief, slide, _brand("corner"), "data:image/jpeg;base64,")
    assert html.count('class="sig-corner"') == 1 and "sig-bar" not in html.split("<body>")[1]
    html = compose.render_html(brief, slide, _brand("bar"), "data:image/jpeg;base64,")
    assert html.count('class="sig-bar"') == 1
    html = compose.render_html(brief, slide, _brand("none"), "data:image/jpeg;base64,")
    body = html.split("<body>")[1]
    assert "sig-corner" not in body and "sig-bar" not in body


@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_every_layout_fits_and_is_grid_safe_at_every_aspect(template):
    """Long copy, every aspect: the compositor never has to give up, and
    nothing a reader must see sits in the strip the profile grid trims."""
    try:
        browser = await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium: {exc}")
    try:
        for aspect in ("1:1", "4:5", "9:16"):
            payload = json.loads(json.dumps(EXAMPLE))
            payload["template_id"] = template
            payload["format"]["aspect_ratio"] = aspect
            payload["headline"] = LONG_HEADLINE
            payload["subhead"] = LONG_SUBHEAD
            brief = CreativeBrief.model_validate(payload)
            slide = brief.units()[0]
            w, h = brief.pixel_size()
            bg = await MockImageProvider().generate(ImageRequest(prompt="x", width=w, height=h))
            html = compose.render_html(brief, slide, _brand("corner"), compose.as_data_uri(bg.data))
            page = await browser.new_page(viewport={"width": w, "height": h})
            try:
                try:
                    await page.set_content(html, wait_until="load", timeout=6000)
                except Exception:  # noqa: BLE001 - fonts host unreachable: fallback faces
                    pass
                fit = await page.evaluate(compose.FIT_JS, list(compose.grid_insets(w, h)))
            finally:
                await page.close()
            assert fit["fits"] is True, (template, aspect, fit)
            assert fit["grid_safe"] is True, (template, aspect, fit["unsafe"])
            # A layout that needs more than a dozen shrink steps is mis-sized.
            assert fit["headline_steps"] <= 12, (template, aspect, fit)
    finally:
        await compose.shutdown()
