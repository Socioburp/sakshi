"""Legibility is a guarantee, not a hope.

Found by LOOKING at renders, not by a test: a brand with a light palette has a
dark ink. The layouts that set type over the photograph darken it with a black
scrim -- so that brand's headline came out near-black on a darkened picture.
Inside every box, overlapping nothing, in the safe zone, and unreadable. Every
geometric guarantee passed. This pins the one that was missing.
"""

from __future__ import annotations

import json
import re
import types

import pytest

from app.creative import compose
from app.creative.brief import EXAMPLE, CreativeBrief

PALETTES = {
    "light_brand_dark_ink": {"primary": "#F4EFE6", "accent": "#E4572E", "ink": "#1B1B1B",
                             "secondary": "#FFFFFF"},
    "dark_brand_light_ink": {"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF",
                             "secondary": "#FFFFFF"},
    "ink_same_as_primary": {"primary": "#0B2A5B", "accent": "#FFD400", "ink": "#0B2A5B",
                            "secondary": "#FFD400"},
    "pastel_everything": {"primary": "#FCE4EC", "accent": "#F8BBD0", "ink": "#F48FB1",
                          "secondary": "#FFFFFF"},
    "no_palette_at_all": {},
}  # fmt: skip


def _brand(palette):
    return types.SimpleNamespace(
        name="Paper Trail", palette=palette, fonts={"heading": "Poppins", "body": "Inter"},
        logo_analysis={}, logo_url=None, logo_src=None, template_prefs={"signature": "none"},
    )  # fmt: skip


def _colours(html: str) -> dict[str, str]:
    def grab(selector: str) -> str:
        rule = html.split(f"  {selector} {{", 1)[1].split("}", 1)[0]
        return re.search(r"(?<![-\w])color:\s*(#[0-9A-Fa-f]{6})", rule).group(1)

    return {"headline": grab(".headline"), "cta": grab(".cta")}


def test_contrast_maths_is_the_wcag_formula():
    assert round(compose.contrast("#FFFFFF", "#000000"), 1) == 21.0
    assert compose.contrast("#777777", "#777777") == 1.0
    assert compose.readable_on("#123B2E", "#FFFFFF") == "#FFFFFF", "a readable brand ink is kept"
    assert compose.readable_on("#F4EFE6", "#FFFFFF") == compose._DARK
    assert compose.readable_on("#123B2E", "not-a-colour") == compose._LIGHT


@pytest.mark.parametrize("palette", PALETTES, ids=list(PALETTES))
@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
def test_type_can_always_be_read_against_what_it_sits_on(template, palette):
    brief = CreativeBrief.model_validate(
        {**json.loads(json.dumps(EXAMPLE)), "template_id": template}
    )
    html = compose.render_html(brief, brief.units()[0], _brand(PALETTES[palette]), "data:,")
    got = _colours(html)
    ctx = compose._brand_context(_brand(PALETTES[palette]))
    if template in compose.TYPE_OVER_PHOTO:
        # over a BLACK scrim: the type must be light, whatever the brand's ink is
        assert compose.contrast(got["headline"], "#000000") >= 7.0, (template, palette, got)
    else:
        assert compose.contrast(got["headline"], ctx["primary"]) >= compose.MIN_CONTRAST, got
    assert compose.contrast(got["cta"], ctx["accent"]) >= compose.MIN_CONTRAST, (palette, got)


def test_the_brands_own_ink_is_kept_wherever_it_can_be_read():
    palette = {"primary": "#123B2E", "accent": "#F2B233", "ink": "#FFF4D6", "secondary": "#2A0D0D"}
    for template in compose.TEMPLATES:
        brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": template})
        got = _colours(compose.render_html(brief, brief.units()[0], _brand(palette), "data:,"))
        assert got == {"headline": "#FFF4D6", "cta": "#2A0D0D"}, template
