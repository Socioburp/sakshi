"""Script-aware fonts: the copy decides which faces the render loads."""

from __future__ import annotations

import json
import types

import pytest

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


@pytest.fixture
def nothing_vendored(monkeypatch):
    """The Google Fonts path: what a brand face outside templates/fonts/ gets."""
    monkeypatch.setattr(fonts, "vendored", lambda: frozenset())


def test_script_faces_get_their_own_stylesheet_request(nothing_vendored):
    """One unknown brand face 400s the whole CSS2 request; the Kannada face
    must not go down with it."""
    assert fonts.script_fonts_href([]) is None
    href = fonts.script_fonts_href(["Noto Sans Kannada", "Noto Sans Tamil"])
    assert href.startswith("https://fonts.googleapis.com/css2?family=Noto+Sans+Kannada")
    assert "Poppins" not in href and href.endswith("&display=block")


def test_rendered_html_loads_the_faces_the_headline_needs(nothing_vendored):
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
    assert "line-height: 1.26" in html and "letter-spacing: 0;" in html


def test_leading_follows_the_script_of_the_element_it_sets():
    """One figure for every Indic script (1.14) left a two-line Hindi headline
    with -20px between the ink of its lines. Devanagari and its relatives hang
    matras below and stack marks above; the southern scripts mostly do not;
    Latin display type stays tight."""
    assert fonts.display_leading([]) == ".98"
    assert fonts.display_leading(["Noto Sans Tamil"]) == "1.26"
    assert fonts.display_leading(["Noto Sans Kannada", "Noto Sans Devanagari"]) == "1.38"
    for tall in ("Devanagari", "Bengali", "Gujarati", "Gurmukhi", "Arabic"):
        assert float(fonts.display_leading([f"Noto Sans {tall}"])) > float(
            fonts.display_leading(["Noto Sans Malayalam"])
        )
    assert fonts.body_leading([]) == "1.34" and fonts.body_leading(["Noto Sans Bengali"]) == "1.6"
    # A Latin headline over a Hindi subhead keeps its tight leading.
    payload = json.loads(json.dumps(EXAMPLE))
    payload["subhead"] = "\u0906\u091c \u0939\u0940 \u0911\u0930\u094d\u0921\u0930"
    brief = CreativeBrief.model_validate(payload)
    html = compose.render_html(brief, brief.units()[0], _brand(), "data:image/jpeg;base64,")
    assert "line-height: .98;" in html and "line-height: 1.6;" in html


def test_urdu_copy_reads_from_the_right_and_the_layout_mirrors():
    assert fonts.text_direction("\u0622\u062c \u06c1\u06cc \u0622\u0631\u0688\u0631") == "rtl"
    assert fonts.text_direction("Weekend Sale") == "ltr"
    assert fonts.text_direction("50% \u0622\u0641") == "rtl", "digits are not a direction"
    assert fonts.text_direction("") == "ltr"
    payload = json.loads(json.dumps(EXAMPLE))
    payload.update(
        headline="\u0622\u062c \u06c1\u06cc \u0622\u0631\u0688\u0631", template_id="lower_third"
    )
    brief = CreativeBrief.model_validate(payload)
    html = compose.render_html(brief, brief.units()[0], _brand(), "data:image/jpeg;base64,")
    assert 'dir="rtl"' in html and '<h1 class="headline" dir="auto">' in html
    assert "text-align: left" not in html


# --------------------------------------------------------------------------- #
# vendored faces: nothing leaves the box
# --------------------------------------------------------------------------- #
def test_every_face_the_product_can_set_is_on_disk():
    from app.creative import brandkit

    have = fonts.vendored()
    for look in brandkit.LOOKS.values():
        assert look.heading in have and look.body in have, look.key
    for family, _, _ in fonts.SCRIPT_BLOCKS:
        assert family in have, family
    assert "Noto Sans" in have
    css = (fonts.LOCAL_DIR / "fonts.css").read_text(encoding="utf-8")
    assert "fonts.gstatic.com" not in css and "googleapis" not in css
    import re

    for name in set(re.findall(r"/([a-z0-9-]+\.woff2)\)", css)):
        assert (fonts.LOCAL_DIR / name).is_file(), name
    assert (fonts.LOCAL_DIR / "OFL-NOTICE.txt").is_file()


def test_a_vendored_brand_asks_google_for_nothing():
    payload = json.loads(json.dumps(EXAMPLE))
    payload["headline"] = "ಇಂದು ಆರ್ಡರ್ ಮಾಡಿ"
    brief = CreativeBrief.model_validate(payload)
    html = compose.render_html(brief, brief.units()[0], _brand(), "data:image/jpeg;base64,")
    assert html.count('<link rel="stylesheet"') == 1
    assert f'href="{fonts.LOCAL_HOST}/fonts.css"' in html
    assert "googleapis" not in html and "gstatic" not in html
    assert fonts.LOCAL_HOST.endswith(".invalid"), "can never resolve if the route is missing"


def test_only_an_unvendored_brand_face_goes_to_google():
    brand = _brand()
    brand.fonts = {"heading": "Bricolage Grotesque", "body": "Inter"}
    brief = CreativeBrief.model_validate(EXAMPLE)
    html = compose.render_html(brief, brief.units()[0], brand, "data:image/jpeg;base64,")
    assert "family=Bricolage+Grotesque" in html and "family=Inter" not in html
    assert fonts.remote_brand_href("Poppins", "Inter") is None


async def test_renders_with_google_fonts_unreachable_and_the_font_guard_on(monkeypatch):
    """The whole point: block the network's font hosts, keep the guard that
    refuses a fallback face, and set Latin, Devanagari and Kannada anyway."""
    try:
        browser = await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium: {exc}")
    monkeypatch.setattr(compose.settings, "compose_require_fonts", True)
    real_new_page = browser.new_page
    blocked = []

    async def new_page(**kw):
        page = await real_new_page(**kw)

        async def block(route):
            blocked.append(route.request.url)
            await route.abort()

        await page.route("**://fonts.googleapis.com/**", block)
        await page.route("**://fonts.gstatic.com/**", block)
        return page

    monkeypatch.setattr(browser, "new_page", new_page)
    try:
        for headline in (
            "Weekend Sale",
            "\u0906\u091c \u0939\u0940 \u0911\u0930\u094d\u0921\u0930 \u0915\u0930\u0947\u0902",
            "\u0c87\u0c82\u0ca6\u0cc1 \u0c86\u0cb0\u0ccd\u0ca1\u0cb0\u0ccd "
            + "\u0cae\u0cbe\u0ca1\u0cbf",
        ):
            payload = json.loads(json.dumps(EXAMPLE))
            payload["headline"] = headline
            brief = CreativeBrief.model_validate(payload)
            report = await compose.check_layout(brief, brief.units()[0], _brand())
            assert report["violations"] == []
        assert blocked == [], "a vendored brand never even asks"
    finally:
        await compose.shutdown()


async def test_the_font_route_serves_only_font_files_from_its_own_directory():
    served = {}

    class Route:
        def __init__(self, url):
            self.request = types.SimpleNamespace(url=url)

        async def fulfill(self, **kw):
            served[self.request.url] = kw

    ok = f"{fonts.LOCAL_HOST}/fonts.css"
    host = fonts.LOCAL_HOST
    bad = (f"{host}/families.txt", f"{host}/..%2f..%2fpyproject.toml", f"{host}/nope.woff2")
    for url in (ok, *bad):
        await compose._serve_fonts(Route(url))
    assert served[ok]["status"] == 200 and served[ok]["content_type"].startswith("text/css")
    assert [v["status"] for k, v in served.items() if k != ok] == [404, 404, 404]


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


def test_latin_headline_loads_only_the_brand_faces(nothing_vendored):
    brief = CreativeBrief.model_validate(EXAMPLE)
    html = compose.render_html(brief, brief.units()[0], _brand(), "data:image/jpeg;base64,")
    assert "Noto+Sans+" not in html
    assert "family=Poppins" in html and "family=Inter" in html
    assert "line-height: .98" in html and "letter-spacing: -0.028em" in html
