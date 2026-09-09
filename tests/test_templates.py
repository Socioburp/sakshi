from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief
from app.creative.compose import TEMPLATES, render_html


class Brand:
    name = "Kadamba Naturals"
    logo_url = None
    palette = {"primary": "#123456", "accent": "#E4572E", "ink": "#FFFFFF"}
    fonts = {"heading": "Poppins", "body": "Inter"}


BG = "data:image/jpeg;base64,AA=="


def test_every_template_renders():
    for key in TEMPLATES:
        brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": key})
        slide = brief.units()[0]
        html = render_html(brief, slide, Brand(), BG)
        assert brief.headline in html
        assert brief.cta in html
        assert "1080px" in html


def test_copy_is_dom_text_not_baked_into_background():
    brief = CreativeBrief.model_validate(EXAMPLE)
    html = render_html(brief, brief.units()[0], Brand(), BG)
    assert html.index('class="bg"') < html.index(brief.headline)
    assert brief.headline not in brief.visual_direction.prompt


def test_carousel_shows_pips_and_holds_cta_to_last_slide():
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    first, last = brief.units()[0], brief.units()[-1]
    html_first = render_html(brief, first, Brand(), BG)
    html_last = render_html(brief, last, Brand(), BG)
    assert html_first.count('<span class="pip') == 3
    assert brief.cta not in html_first
    assert brief.cta in html_last
