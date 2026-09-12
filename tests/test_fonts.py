"""Script-aware fonts: the copy decides which faces the render loads."""

from __future__ import annotations

import json
import types

from app.creative import compose, fonts
from app.creative.brief import EXAMPLE, CreativeBrief


def test_latin_copy_needs_no_script_faces():
    assert fonts.script_families("Weekend Sale — order on WhatsApp!") == []
    assert fonts.script_families("") == []


def test_indic_scripts_are_detected_in_block_order():
    text = "இன்று ஆர்டர் · आज ही ऑर्डर करें · ಇಂದು"
    assert fonts.script_families(text) == [
        "Noto Sans Devanagari",
        "Noto Sans Tamil",
        "Noto Sans Kannada",
    ]
    assert fonts.script_families("আজই অর্ডার") == ["Noto Sans Bengali"]
    assert fonts.script_families("આજે જ") == ["Noto Sans Gujarati"]
    assert fonts.script_families("ఈరోజే") == ["Noto Sans Telugu"]
    assert fonts.script_families("ഇന്ന്") == ["Noto Sans Malayalam"]
    assert fonts.script_families("ਅੱਜ") == ["Noto Sans Gurmukhi"]
    assert fonts.script_families("آج") == ["Noto Sans Arabic"]


def test_href_carries_brand_faces_and_script_faces():
    href = fonts.google_fonts_href("Poppins", "Inter", ["Noto Sans Tamil"])
    assert href.startswith("https://fonts.googleapis.com/css2?")
    assert "family=Poppins:wght@600;700;800" in href
    assert "family=Inter:wght@400;600;700" in href
    assert "family=Noto+Sans+Tamil:wght@" in href
    assert href.endswith("&display=block")
    # Same heading and body face: listed once.
    assert fonts.google_fonts_href("Poppins", "Poppins", []).count("Poppins") == 1


def test_css_stack_lists_script_faces_before_the_system_face():
    assert fonts.css_stack(["Noto Sans Kannada"]) == (
        "'Noto Sans Kannada', 'Noto Sans', system-ui, sans-serif"
    )
    assert fonts.css_stack([]) == "'Noto Sans', system-ui, sans-serif"


def _brand():
    return types.SimpleNamespace(
        name="Test",
        palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
        fonts={"heading": "Poppins", "body": "Inter"},
        logo_analysis={},
        logo_url=None,
        logo_src=None,
    )


def test_script_faces_get_their_own_stylesheet_request():
    """One unknown brand face 400s the whole CSS2 request; the Kannada face
    must not go down with it."""
    assert fonts.script_fonts_href([]) is None
    href = fonts.script_fonts_href(["Noto Sans Kannada", "Noto Sans Tamil"])
    assert href.startswith("https://fonts.googleapis.com/css2?family=Noto+Sans+Kannada")
    assert "Poppins" not in href and href.endswith("&display=block")


def test_rendered_html_loads_the_faces_the_headline_needs():
    payload = json.loads(json.dumps(EXAMPLE))
    payload["headline"] = "ಇಂದು ಆರ್ಡರ್ ಮಾಡಿ"
    brief = CreativeBrief.model_validate(payload)
    html = compose.render_html(brief, brief.units()[0], _brand(), "data:image/jpeg;base64,")
    assert html.count('<link rel="stylesheet"') == 2
    assert "family=Poppins" in html and "family=Noto+Sans+Kannada" in html
    assert "'Poppins', 'Noto Sans Kannada', 'Noto Sans', system-ui, sans-serif" in html
    # The quotes in the stack are real quotes inside <style>, not entities.
    assert "&#39;Noto" not in html
    assert "display=block" in html
    # Marks above and below the line need room; conjuncts need no tracking.
    assert "line-height: 1.14" in html and "letter-spacing: 0;" in html


def test_copy_is_escaped_but_the_font_stack_is_not():
    """Autoescape must cover .html.j2: a headline is text, never markup."""
    payload = json.loads(json.dumps(EXAMPLE))
    payload["headline"] = "Buy 1 <b>get 1</b> & more"
    brief = CreativeBrief.model_validate(payload)
    brand = _brand()
    brand.name = "T & C's <shop>"
    brand.fonts = {"heading": "Poppins'; } </style><script>", "body": "Inter"}
    html = compose.render_html(brief, brief.units()[0], brand, "data:image/jpeg;base64,")
    assert "<b>get 1</b>" not in html and "&lt;b&gt;get 1&lt;/b&gt; &amp; more" in html
    assert "<shop>" not in html and "&lt;shop&gt;" in html
    # A font name that is not a font name falls back to the default face.
    assert "<script>" not in html and "font-family: 'Poppins'," in html
    assert ", 'Noto Sans', system-ui, sans-serif" in html


def test_latin_headline_loads_only_the_brand_faces():
    brief = CreativeBrief.model_validate(EXAMPLE)
    html = compose.render_html(brief, brief.units()[0], _brand(), "data:image/jpeg;base64,")
    assert "Noto+Sans+" not in html
    assert "family=Poppins" in html and "family=Inter" in html
    assert "line-height: .98" in html and "letter-spacing: -0.028em" in html
