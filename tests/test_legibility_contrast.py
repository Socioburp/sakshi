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

    pill = html.split("  .cta {", 1)[1].split("}", 1)[0]
    ground = re.search(r"background:\s*(#[0-9A-Fa-f]{6})", pill).group(1)
    return {"headline": grab(".headline"), "cta": grab(".cta"), "cta_ground": ground}


def test_contrast_maths_is_the_wcag_formula():
    assert round(compose.contrast("#FFFFFF", "#000000"), 1) == 21.0
    assert compose.contrast("#777777", "#777777") == 1.0
    assert compose.readable_on("#123B2E", "#FFFFFF") == "#FFFFFF", "a readable brand ink is kept"
    assert compose.readable_on("#F4EFE6", "#FFFFFF") == compose._DARK
    assert compose.readable_on("#123B2E", "not-a-colour") == compose._LIGHT


def test_a_palette_colour_is_read_the_way_the_stylesheet_renders_it():
    """CSS renders '#123', 'rgb(18,59,46)', 'darkgreen' and ' #123B2E ' as the
    colours they are; the contrast maths saw "not #RRGGBB" and used mid grey,
    so the brand's white ink became black on its own green panel and the
    legibility pass laid white plates over the panel to rescue it. Every
    colour is brought to one form at the boundary; nothing falls back to
    grey in silence."""
    for form in (
        "#123B2E",
        "#123b2e",
        " #123B2E ",
        "#123B2EFF",
        "rgb(18, 59, 46)",
        "rgb(18,59,46)",
    ):
        assert compose.normalise_colour(form) == "#123B2E", form
    assert compose.normalise_colour("#123") == "#112233"
    assert compose.normalise_colour("darkgreen") == "#006400"
    assert compose.normalise_colour("white") == "#FFFFFF"
    for junk in ("brand orange", "", None, "#12", "#GGGGGG", 12):
        assert compose.normalise_colour(junk) is None, junk
    with pytest.raises(ValueError):
        compose._luminance("darkgreen")
    for palette in (
        {"primary": "rgb(18,59,46)", "accent": "rgb(228,87,46)", "ink": "rgb(255,255,255)"},
        {"primary": "darkgreen", "accent": "orangered", "ink": "white"},
        {"primary": " #123B2E ", "accent": "#E4572E", "ink": "#FFFFFF"},
    ):
        ctx = compose._brand_context(_brand(palette))
        assert (
            ctx["ink"] == "#FFFFFF" and ctx["primary"].startswith("#") and len(ctx["primary"]) == 7
        )
        for template in sorted(compose.CTA_ON_PANEL):
            brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": template})
            got = _colours(compose.render_html(brief, brief.units()[0], _brand(palette), "data:,"))
            assert got["headline"] == "#FFFFFF", (palette, template, got)
    # An unreadable colour gets the default for its role, never mid grey.
    ctx = compose._brand_context(_brand({"primary": "brand green", "ink": "#FFFFFF"}))
    assert ctx["primary"] == compose.DEFAULT_PALETTE["primary"]


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
    # The label is read against the pill it actually sits on. That is the
    # accent -- except on a panel layout whose accent cannot be told from the
    # panel, where the pill takes the panel's ink (pinned further down).
    assert compose.contrast(got["cta"], got["cta_ground"]) >= compose.MIN_CONTRAST, (palette, got)
    if template in compose.CTA_ON_PANEL:
        assert compose.contrast(got["cta_ground"], ctx["primary"]) >= compose.MIN_SHAPE_CONTRAST
    else:
        assert got["cta_ground"].upper() == ctx["accent"].upper()


def test_the_brands_own_ink_is_kept_wherever_it_can_be_read():
    palette = {"primary": "#123B2E", "accent": "#F2B233", "ink": "#FFF4D6", "secondary": "#2A0D0D"}
    for template in compose.TEMPLATES:
        brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": template})
        got = _colours(compose.render_html(brief, brief.units()[0], _brand(palette), "data:,"))
        assert got == {"headline": "#FFF4D6", "cta": "#2A0D0D", "cta_ground": "#F2B233"}, template


def test_the_button_does_not_dissolve_into_the_panel():
    """A palette pulled from a one-colour logo: accent ~= primary. On the panel
    layouts the pill vanished into the panel and the CTA read as loose bold
    text. It becomes an inverse button in the panel's readable ink, and
    frame_card's border goes with it -- decided from the numbers, every time."""
    palette = {"primary": "#0B2A5B", "accent": "#10326B", "ink": "#FFFFFF", "secondary": "#FFFFFF"}
    assert compose.contrast(palette["accent"], palette["primary"]) < compose.MIN_SHAPE_CONTRAST
    for template in sorted(compose.CTA_ON_PANEL):
        brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": template})
        html = compose.render_html(brief, brief.units()[0], _brand(palette), "data:,")
        got = _colours(html)
        assert got["cta_ground"] == "#FFFFFF" and got["cta"] == "#0B2A5B", (template, got)
        assert compose.contrast(got["cta_ground"], palette["primary"]) >= compose.MIN_CONTRAST
    frame = compose.render_html(
        CreativeBrief.model_validate({**EXAMPLE, "template_id": "frame_card"}),
        CreativeBrief.model_validate({**EXAMPLE, "template_id": "frame_card"}).units()[0],
        _brand(palette),
        "data:,",
    )
    assert "solid #FFFFFF" in frame and "solid #10326B" not in frame
    # Over the photograph the accent pill is left alone: its ground is the picture.
    brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": "lower_third"})
    html = compose.render_html(brief, brief.units()[0], _brand(palette), "data:,")
    assert _colours(html)["cta_ground"] == "#10326B"


def test_a_mid_tone_ground_still_gets_an_ink_that_clears_the_bar():
    """At luminance ~0.19 white and near-black both land at ~4.3:1, under the
    bar the label is later MEASURED against. Pure black always clears it."""
    for ground in ("#7A7A7A", "#767676", "#808080", "#6E7B8B", "#8A7F72"):
        ink = compose.readable_on(ground, "not-a-colour")
        assert compose.contrast(ink, ground) >= compose.MIN_CONTRAST, (ground, ink)
