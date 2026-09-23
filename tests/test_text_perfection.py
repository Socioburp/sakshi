"""Text is never cropped, never broken mid-word, never unreadable.

Every test here pins a failure that was REPRODUCED with the real compositor
before it was fixed: a word cut in two at full size, white type shipped on a
white photograph at 2.78:1, a brand name nobody could read over a studio
backdrop, a valid carousel refused for a typeface it did not use, a shop whose
long name refused every render it ever asked for. They run the real page in
real Chromium with the vendored faces -- a guarantee is only as good as the
frame it was measured on.
"""

from __future__ import annotations

import io
import json
import types

import pytest
from PIL import Image, ImageDraw

from app.creative import compose, legibility, pipeline
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief

LONG_NAME = "Sri Venkateshwara Traders and General Stores"
HINDI = "त्योहार की खुशियाँ दुगुनी करें पूरे हफ़्ते"
TAMIL_WORD = "நல்வாழ்த்துக்கள்"
URDU = "آج ہی آرڈر کریں"


def _brand(name: str = "Kadamba Naturals", palette: dict | None = None, logo: bytes | None = None):
    brand = types.SimpleNamespace(
        name=name,
        palette=palette or {"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
        fonts={"heading": "Poppins", "body": "Inter"},
        logo_analysis={},
        logo_url=None,
        logo_src=None,
        template_prefs={"signature": "none"},
    )
    if logo:
        brand.logo_src = compose.as_data_uri(logo, "image/png")
    return brand


def _brief(template: str, headline: str, subhead=None, cta=None, kind: str = "single"):
    payload = json.loads(json.dumps(EXAMPLE))
    payload.update(template_id=template, headline=headline, subhead=subhead, cta=cta)
    payload["format"] = {"type": kind}
    return CreativeBrief.model_validate(payload)


def _flat(rgb: tuple[int, int, int], size=(1600, 2000)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, "PNG")
    return buf.getvalue()


def _emblem(fill: tuple[int, int, int, int], *, opaque_ground=None) -> bytes:
    """A round mark. Transparent around the disc, or -- with `opaque_ground` --
    the JPEG-style file with its background baked in."""
    im = Image.new("RGBA", (400, 400), opaque_ground or (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([20, 20, 380, 380], fill=fill)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
async def chromium():
    try:
        await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium: {exc}")
    yield
    await compose.shutdown()


async def _layout_with(brief, brand, css: str = "") -> dict:
    """check_layout(), with a stylesheet override -- how a detector is proven to
    fire: put the page in the broken state the templates no longer allow."""
    slide = brief.units()[0]
    w, h = brief.pixel_size()
    browser = await compose.get_browser()
    page = await browser.new_page(viewport={"width": w, "height": h})
    try:
        html = compose.render_html(brief, slide, brand, compose._BLANK_BG)
        html = html.replace("</style>", css + "</style>", 1)
        return await compose._layout(page, brief, slide, brand, html)
    finally:
        await page.close()


def _ink(report: dict, cls: str) -> dict:
    return next(i for i in report["inks"] if i["cls"] == cls)


# --------------------------------------------------------------------------- #
# 1. no mid-word breaks
# --------------------------------------------------------------------------- #
async def test_a_festival_name_is_never_cut_in_two(chromium):
    """'Anniversar|y', 'Mahashivr|atri', 'Janmashta|mi Sale' and the Tamil
    greeting all shipped split, at full size, with zero violations."""
    for template, headline, lines in (
        ("centered_overlay", "Anniversary", ["Anniversary"]),
        ("poster_stack", "Mahashivratri", ["Mahashivratri"]),
        ("lower_third", "Janmashtami Sale", ["Janmashtami", "Sale"]),
        ("centered_overlay", TAMIL_WORD, [TAMIL_WORD]),
    ):
        brief = _brief(template, headline)
        report = await compose.check_layout(brief, brief.units()[0], _brand())
        assert _ink(report, "headline")["lines"] == lines, (template, headline)
        assert report["violations"] == []


async def test_a_word_set_across_two_lines_is_a_violation(chromium):
    """The detector itself, with word-breaking forced back on: the search
    shrinks the type until the word is whole, and where even the floor cannot
    hold it the render is refused as `wordbreak`."""
    broken = ".headline { overflow-wrap: anywhere !important; }"
    brief = _brief("centered_overlay", "Anniversary")
    report = await _layout_with(brief, _brand(), broken)
    assert _ink(report, "headline")["lines"] == ["Anniversary"]
    assert report["sizes"]["headline"]["px"] < report["sizes"]["headline"]["design"]
    brief = _brief("centered_overlay", "Supercalifragilisticexpialidocious")
    with pytest.raises(compose.LayoutError) as err:
        await _layout_with(brief, _brand(), broken)
    assert "wordbreak:headline" in err.value.violations


async def test_a_hyphenated_word_may_break_at_its_hyphen(chromium):
    brief = _brief("centered_overlay", "Supercalifragilistic-expialidocious")
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    assert _ink(report, "headline")["lines"] == ["Supercalifragilistic-", "expialidocious"]


def test_no_template_lets_the_browser_break_a_word():
    import re

    for path in compose.TEMPLATE_DIR.glob("*.j2"):
        css = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for banned in ("break-word", "break-all", "overflow-wrap: anywhere", "hyphens: auto"):
            assert banned not in css, (path.name, banned)


# --------------------------------------------------------------------------- #
# 2. contrast is measured on the real frame, and the scrim can deliver it
# --------------------------------------------------------------------------- #
def _ground_behind(png: bytes, box: dict) -> float:
    """Independent of the compositor: the median luminance of the pixels inside
    an element's ink box that are NOT ink, read off the delivered frame."""
    import numpy as np

    with Image.open(io.BytesIO(png)) as im:
        crop = im.convert("RGB").crop(tuple(round(box[k]) for k in ("l", "t", "r", "b")))
    rgb = np.asarray(crop, dtype=float) / 255
    linear = np.where(rgb <= 0.03928, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    lum = linear @ np.array([0.2126, 0.7152, 0.0722])
    return float(np.median(lum[lum < 0.6]))


@pytest.mark.parametrize(
    "template", ["centered_overlay", "lower_third", "poster_stack", "top_band"]
)
async def test_white_type_on_a_white_photograph_still_reads(chromium, template):
    """Shipped at 2.78:1 (centered_overlay) and the brand name at 1.30:1
    (top_band), because the boost was set as CSS opacity -- clamped at 1 -- the
    plate was capped at .42 and nobody measured the frame."""
    brief = _brief(template, "Weekend Sale", "Cold-pressed, ghar jaisa shudh", "Order now")
    png, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(), _flat((255, 255, 255)), "image/png"
    )
    found = report["legibility"]["contrast"]
    assert set(found) == {"headline", "subhead", "cta", "brandline"}
    assert min(found.values()) >= legibility.TEXT_CONTRAST, found
    for cls in ("headline", "subhead", "brandline"):
        ink = _ink(report, cls)
        assert ink["color"] == "rgb(255, 255, 255)" and ink["opacity"] == 1
        ground = _ground_behind(png, ink["box"])
        assert legibility.contrast_ratio(1.0, ground) >= legibility.TEXT_CONTRAST - 0.1, (
            cls,
            ground,
        )


async def test_a_bright_band_through_one_line_of_the_headline_is_measured(chromium):
    """A white band 20px tall behind the first line of a 130px headline was 8%
    of the headline's box: the 90th percentile never saw it, the report said
    18.75:1, and the lower half of 'Weekend' dissolved into the band. The
    ground is now read in .3em cells, line by line, and the worst cell counts."""
    import numpy as np

    brief = _brief(
        "centered_overlay", "Weekend Sale", "Cold-pressed, ghar jaisa shudh", "Order now"
    )
    laid = await compose.check_layout(brief, brief.units()[0], _brand())
    box = _ink(laid, "headline")["box"]
    w, h = brief.pixel_size()
    top = round(box["t"] + 0.28 * (box["b"] - box["t"]))
    photo = Image.new("RGB", (w, h), (20, 22, 26))
    ImageDraw.Draw(photo).rectangle([0, top, w, top + 20], fill=(255, 255, 255))
    buf = io.BytesIO()
    photo.save(buf, "PNG")
    png, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(), buf.getvalue(), "image/png"
    )
    assert [p["anchor"] for p in report["legibility"]["plates"]] == ["headline"]
    assert report["legibility"]["contrast"]["headline"] >= legibility.TEXT_CONTRAST
    # Independently: on the delivered frame the band behind the letters is dark
    # enough for white type -- the pixels in the band's rows that are not ink.
    with Image.open(io.BytesIO(png)) as im:
        rows = np.asarray(
            im.convert("RGB").crop((round(box["l"]), top + 4, round(box["r"]), top + 16))
        )
    lum = (rows / 255.0) ** 2.2 @ np.array([0.2126, 0.7152, 0.0722])
    ground = float(np.median(lum[lum < 0.85]))
    assert legibility.contrast_ratio(1.0, ground) >= legibility.TEXT_CONTRAST - 0.1, ground


def test_the_ground_is_read_in_cells_the_size_of_a_letter():
    """A bright feature that fills a cell is measured whole wherever it falls;
    a speck averages away inside its cell."""
    import numpy as np

    def read(plane):
        frame = Image.fromarray((plane * 255).astype("uint8"))
        return legibility.ground(frame, [(0, 0, plane.shape[1], plane.shape[0])], 100)

    assert legibility.windows_for(100) == (30, 15) and legibility.windows_for(130) == (39, 20)
    assert legibility.windows_for(4) == (legibility.GROUND_WINDOW_MIN,) * 2
    band = np.full((120, 600), 0.01)
    band[43:58, :] = 1.0  # a 15px band across the line, on no grid at all
    assert read(band)["bright"] > 0.95
    bar = np.full((120, 600), 0.01)
    bar[:, 217:232] = 1.0  # a 15px bar down through the line
    assert read(bar)["bright"] > 0.95
    blob = np.full((120, 600), 0.01)
    blob[41:71, 301:331] = 1.0  # a 30px blob behind one letter
    assert read(blob)["bright"] > 0.95
    speck = np.full((120, 600), 0.01)
    speck[60:64, 300:304] = 1.0
    assert read(speck)["bright"] < 0.05


async def test_a_dark_photograph_is_left_alone(chromium):
    brief = _brief("centered_overlay", "Weekend Sale", "Cold-pressed", "Order now")
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(), _flat((28, 32, 38)), "image/png"
    )
    assert report["legibility"]["plates"] == [] and report["legibility"]["mark_plate"] is None


async def test_each_cluster_of_words_gets_its_own_plate_not_one_slab(chromium, monkeypatch):
    """poster_stack sets the headline at the top and the CTA and name at the
    foot. One plate for 'the text' was a dark slab across the middle of the
    photograph, where the product is and no words are."""
    brief = _brief("poster_stack", "Weekend Sale", "Cold-pressed", "Order now")
    w, h = brief.pixel_size()
    # k pinned at the design strength so both ends of the layout need a plate
    monkeypatch.setattr(legibility, "MAX_BOOST", 1.0)
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(), _flat((255, 255, 255)), "image/png"
    )
    plates = report["legibility"]["plates"]
    assert sorted(p["anchor"] for p in plates) == ["brandline", "headline"], plates
    for plate in plates:
        left, top, right, bottom = plate["box"]
        assert not (left < w / 2 < right and top < h / 2 < bottom), "a plate over the middle"
        assert bottom - top < 0.45 * h
        assert 0 < plate["alpha"] <= legibility.LOCAL_MAX


async def test_a_frame_that_cannot_be_made_legible_is_refused_with_its_numbers(
    chromium, monkeypatch
):
    monkeypatch.setattr(legibility, "LOCAL_MAX", 0.05)
    brief = _brief("centered_overlay", "Weekend Sale", "Cold-pressed", "Order now")
    with pytest.raises(compose.LegibilityError) as err:
        await compose.compose(
            brief, brief.units()[0], _brand(), _flat((255, 255, 255)), "image/png"
        )
    assert isinstance(err.value, compose.LayoutError) and err.value.position == 1
    assert "contrast:headline" in err.value.violations
    assert 1.0 < err.value.measured["headline"] < legibility.TEXT_CONTRAST
    assert str(err.value.measured["headline"]) in str(err.value)


async def test_a_refusal_reports_what_no_plate_could_fix_not_what_one_would_have(
    chromium, monkeypatch
):
    """The CTA sits on its own pill, so no plate can help it -- and the loop
    used to stop the moment it fell short, before the headline's plate was
    laid, so the refusal said 'headline 1.43:1' about a headline one round
    would have fixed. Held to 8:1, the pill's label falls short and the
    headline over white does not, because it is plated first."""
    monkeypatch.setattr(legibility, "TEXT_CONTRAST", 8.0)
    brief = _brief("centered_overlay", "Weekend Sale", "Cold-pressed", "Order now")
    with pytest.raises(compose.LegibilityError) as err:
        await compose.compose(
            brief, brief.units()[0], _brand(), _flat((255, 255, 255)), "image/png"
        )
    assert set(err.value.measured) == {"cta"}, err.value.measured


async def test_a_plate_lands_over_the_ground_it_was_laid_for_in_every_layout(chromium):
    """top_band's headline lives in .band, which was not positioned: the plate
    went into .col at z-index -1, BENEATH the band's own fill, and changed
    nothing -- the pass would have spent its rounds on an invisible plate and
    refused a valid job."""
    for template in sorted(compose.TEMPLATES):
        brief = _brief(template, "Weekend Sale", "Cold-pressed", "Order now")
        w, h = brief.pixel_size()
        browser = await compose.get_browser()
        page = await browser.new_page(viewport={"width": w, "height": h})
        try:
            html = compose.render_html(
                brief,
                brief.units()[0],
                _brand(),
                compose.as_data_uri(_flat((20, 20, 20)), "image/png"),
            )
            report = await compose._layout(page, brief, brief.units()[0], _brand(), html)
            feather = w * compose.PLATE_FEATHER
            for item in report["inks"]:
                if item["cls"] == "cta":
                    continue  # its ground is its own pill; no plate is ever laid for it
                await page.evaluate(compose.SCRIM_JS, {"k": None, "plates": [], "marks": []})
                before = legibility.ground(
                    await compose._frame(page, (0, 0, w, h), "no-type"), item["rows"], item["px"]
                )
                box = item["box"]
                plate = {"anchor": item["cls"], "colour": "#FFFFFF", "alpha": 0.9, "box": [
                    box["l"] - feather, box["t"] - feather, box["r"] + feather, box["b"] + feather,
                ]}  # fmt: skip
                await page.evaluate(compose.SCRIM_JS, {"k": None, "plates": [plate], "marks": []})
                after = legibility.ground(
                    await compose._frame(page, (0, 0, w, h), "no-type"), item["rows"], item["px"]
                )
                assert after["dark"] > before["bright"] + 0.3, (
                    template,
                    item["cls"],
                    before,
                    after,
                )
        finally:
            await page.close()


async def test_a_flat_studio_backdrop_is_a_photograph_and_gets_no_slack(chromium, monkeypatch):
    """A seamless grey backdrop measures as flat as a brand panel, so the
    panel's measurement slack let a brand name over it ship at 4.48:1 with no
    plate. Whether a ground is designed is read from the DOM: the headline
    sits in the band, the brand name sits on the picture. With the slack
    opened wide, the band's type is excused and the name over the photograph
    is still plated to the bar."""
    monkeypatch.setattr(legibility, "MEASURE_SLACK", 0.5)
    brief = _brief(
        "top_band", "Weekend Sale", "Cold-pressed, ghar jaisa shudh", "Order now", "story"
    )
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(), _flat((128, 128, 128)), "image/png"
    )
    assert _ink(report, "headline")["solid"] and _ink(report, "cta")["solid"]
    assert not _ink(report, "brandline")["solid"]
    assert [p["anchor"] for p in report["legibility"]["plates"]] == ["brandline"]
    assert report["legibility"]["contrast"]["brandline"] >= legibility.TEXT_CONTRAST
    assert legibility.designed(True, {"bright": 0.19, "dark": 0.185})
    assert not legibility.designed(False, {"bright": 0.19, "dark": 0.185})
    assert not legibility.designed(True, {"bright": 0.42, "dark": 0.11})


async def test_a_panel_ink_that_only_just_clears_the_bar_is_not_refused_by_the_measurement(
    chromium,
):
    """White on #767676 is 4.54:1 -- a pass, by the same formula the frame is
    then measured with. Reading it back off 8-bit pixels must not flip it."""
    palette = {"primary": "#767676", "accent": "#E4572E", "ink": "#FFFFFF"}
    assert 4.5 <= compose.contrast("#FFFFFF", "#767676") < 4.6
    brief = _brief("split_card", "Weekend Sale", "Cold-pressed", "Order now")
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(palette=palette), _flat((120, 130, 110)), "image/png"
    )
    found = report["legibility"]["contrast"]
    assert report["legibility"]["plates"] == []
    assert min(found.values()) >= legibility.TEXT_CONTRAST - legibility.MEASURE_SLACK, found


def test_the_slack_is_for_designed_grounds_only():
    """A panel or a pill that passed on paper is not plated over a rounding
    error; a photograph or gradient a hair under the bar IS -- the plate is
    ours to strengthen, and one more round took 4.48 to 4.6."""
    panel = {"bright": 0.1897, "dark": 0.1845}  # #777777 vs #767676: one 8-bit level
    photo = {"bright": 0.42, "dark": 0.11}
    assert legibility.is_flat(panel) and not legibility.is_flat(photo)
    assert legibility.bar_for("logo") == legibility.MARK_CONTRAST
    assert legibility.bar_for("headline") == legibility.TEXT_CONTRAST
    assert 0 < legibility.MEASURE_SLACK <= 0.05


def test_the_plate_is_computed_from_the_contrast_target():
    white_paper = {"bright": 1.0, "dark": 1.0}
    alpha = legibility.plate_alpha(1.0, white_paper, 0.0, legibility.TEXT_CONTRAST)
    assert 0.45 < alpha < 0.62, "what 4.5:1 takes on pure white -- the old cap was .42, blurred"
    # a second round is absolute, never stacked, and never weaker
    assert legibility.plate_alpha(1.0, {"bright": 0.16, "dark": 0.16}, alpha, 4.5) == alpha
    assert legibility.plate_alpha(1.0, {"bright": 0.02, "dark": 0.01}, 0.0, 4.5) == 0.0
    # dark ink is served by a light plate
    assert legibility.plate_colour(0.01) == "#FFFFFF" and legibility.plate_colour(1.0) == "#000000"
    assert legibility.plate_alpha(0.01, {"bright": 0.02, "dark": 0.0}, 0.0, 4.5) > 0.3


def test_opacity_counts_against_the_contrast():
    """.subhead carried opacity .90 and .brandline .88; the check saw the
    colour the stylesheet named, not the one that rendered."""
    ground = {"bright": 0.17, "dark": 0.17}
    solid = legibility.contrast_on(1.0, 1.0, ground)
    assert round(solid, 2) == round(1.05 / 0.22, 2)
    assert legibility.contrast_on(1.0, 0.88, ground) < solid
    base = (compose.TEMPLATE_DIR / "_base.html.j2").read_text(encoding="utf-8")
    for selector in ("  .subhead {", "  .brandline {"):
        rule = base.split(selector, 1)[1].split("}", 1)[0]
        assert "opacity" not in rule, selector


def test_a_boost_above_one_is_a_real_boost():
    """`opacity: 1.85` is `opacity: 1`. The strength is a multiplier on every
    stop's alpha instead."""
    for name in sorted(compose.TYPE_OVER_PHOTO):
        css = (compose.TEMPLATE_DIR / f"{name}.html.j2").read_text(encoding="utf-8")
        rule = css.split(".scrim {", 1)[1].split("); }", 1)[0]
        assert rule.count("rgba(") == rule.count("*var(--k)") > 0, name
    assert "scrim.style.opacity" not in compose.SCRIM_JS and "--k" in compose.SCRIM_JS
    assert 1.0 < legibility.MAX_BOOST <= 1.5, "1.85, made real, blacks out the foot of the photo"


# --------------------------------------------------------------------------- #
# 3. the brand mark over a photograph is protected too
# --------------------------------------------------------------------------- #
async def test_a_white_logo_on_a_white_photograph_gets_a_plate(chromium):
    logo = _emblem((255, 255, 255, 255))
    brief = _brief("lower_third", "Weekend Sale", "Cold-pressed", "Order now")
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(logo=logo), _flat((255, 255, 255)), "image/png"
    )
    plate = report["legibility"]["mark_plate"]
    assert plate and plate["colour"] == compose._DARK and plate["radius"] > 0
    assert report["legibility"]["contrast"]["logo"] >= legibility.MARK_CONTRAST
    # inside the clear space FIT_JS proved empty
    box, mark = report["boxes"]["logo"], plate["box"]
    clear = compose.LOGO_CLEAR * (box["b"] - box["t"])
    assert mark[0] >= box["l"] - clear and mark[3] <= box["b"] + clear


async def test_a_logo_that_already_separates_is_left_alone(chromium):
    logo = _emblem((255, 255, 255, 255))
    brief = _brief("lower_third", "Weekend Sale", "Cold-pressed", "Order now")
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(logo=logo), _flat((28, 32, 38)), "image/png"
    )
    assert report["legibility"]["mark_plate"] is None
    assert report["legibility"]["contrast"]["logo"] >= legibility.MARK_CONTRAST


async def test_a_dark_logo_on_a_dark_panel_gets_a_light_plate(chromium):
    logo = _emblem((18, 40, 32, 255))
    brief = _brief("split_card", "Weekend Sale", "Cold-pressed", "Order now")
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(logo=logo), _flat((120, 130, 110)), "image/png"
    )
    assert report["legibility"]["mark_plate"]["colour"] == compose._LIGHT
    assert report["legibility"]["contrast"]["logo"] >= legibility.MARK_CONTRAST


async def test_a_logo_file_with_its_background_baked_in_sits_on_a_deliberate_card(chromium):
    """The white-background JPEG: it shipped as a bare white rectangle with a
    drop shadow on the brand's dark panel."""
    logo = _emblem((200, 30, 40, 255), opaque_ground=(255, 255, 255, 255))
    brief = _brief("split_card", "Weekend Sale", "Cold-pressed", "Order now")
    _, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(logo=logo), _flat((120, 130, 110)), "image/png"
    )
    plate = report["legibility"]["mark_plate"]
    assert plate["colour"] == "#FFFFFF" and plate["alpha"] == 1.0 and plate["radius"] >= 8


def test_the_mark_is_read_off_the_rendered_frame():
    ground = Image.new("RGB", (120, 120), (250, 250, 250))
    with_disc = ground.copy()
    ImageDraw.Draw(with_disc).ellipse([10, 10, 110, 110], fill=(255, 255, 255))
    looks = legibility.mark_stats(with_disc, ground, (0, 0, 120, 120))
    assert looks["opaque"] is False and looks["luminance"] > 0.9  # it IS its ground
    with_card = ground.copy()
    ImageDraw.Draw(with_card).rectangle([0, 0, 119, 119], fill=(20, 20, 20))
    looks = legibility.mark_stats(with_card, ground, (0, 0, 120, 120))
    assert looks["opaque"] is True and looks["edge"] == "#141414" and looks["luminance"] < 0.02


# --------------------------------------------------------------------------- #
# 4. the font guard checks what is actually rendered
# --------------------------------------------------------------------------- #
async def test_a_carousel_slide_with_no_body_type_is_not_refused_for_the_body_face(
    chromium, monkeypatch
):
    """The prompt's own example carousel, for a brand with a logo: slide 1 has
    no subhead, shows no CTA (last slide only) and no brand name -- nothing is
    set in Inter, Chromium never fetches it, and the guard refused the render
    with 'brand faces did not load: Inter'."""
    monkeypatch.setattr(compose.settings, "compose_require_fonts", True)
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    brand = _brand(logo=_emblem((255, 255, 255, 255)))
    for slide in brief.units():
        report = await compose.check_layout(brief, slide, brand)
        assert report["violations"] == []
    assert await pipeline.layout_gate(brief, brief.units(), brand) is None


async def test_a_face_without_the_weight_it_is_set_in_is_refused(chromium, monkeypatch):
    """Family-level checking passed Manrope 600 for a headline set in Manrope
    800. Inter ships 400/600/700: a headline asking it for 800 would be a
    synthesised bold, which is not the brand's face."""
    monkeypatch.setattr(compose.settings, "compose_require_fonts", True)
    brand = _brand()
    brand.fonts = {"heading": "Inter", "body": "Inter"}
    brief = _brief("lower_third", "Weekend Sale")
    with pytest.raises(compose.BrandFontUnavailable, match="Inter 800"):
        await compose.check_layout(brief, brief.units()[0], brand)


async def test_fonts_that_never_settle_are_a_refusal_not_a_warning(chromium, monkeypatch):
    """Every vendored face is font-display: block -- a face still loading
    paints invisible text. The timeout used to be logged and ignored."""
    monkeypatch.setattr(compose.settings, "compose_require_fonts", True)
    monkeypatch.setattr(compose, "FONT_WAIT_S", 0)  # nothing settles in no time at all
    brief = _brief("lower_third", "Weekend Sale")
    with pytest.raises(compose.BrandFontUnavailable, match="did not settle"):
        await compose.check_layout(brief, brief.units()[0], _brand())


# --------------------------------------------------------------------------- #
# 5. a long brand name never causes a permanent refusal
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["single", "story"])
@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_a_long_shop_name_wraps_inside_the_safe_zone(chromium, template, kind):
    brief = _brief(template, "Weekend Sale", "Fresh stock this Friday", "Order now", kind)
    report = await compose.check_layout(brief, brief.units()[0], _brand(LONG_NAME))
    name = _ink(report, "brandline")
    assert 1 <= len(name["lines"]) <= 2 and " ".join(name["lines"]) == LONG_NAME
    assert name["px"] >= compose.BRANDLINE_MIN * 1080 and name["opacity"] == 1
    w, h = brief.pixel_size()
    pad = compose.padding_for(w, h)
    assert name["box"]["l"] >= pad["pad_x"] - 1 and name["box"]["r"] <= w - pad["pad_x"] + 1


async def test_a_name_that_cannot_be_set_is_not_blamed_on_the_copy(chromium):
    """The agent was told to 'shorten the headline' for a violation that was
    about the brand's NAME, and looped on rewrites that could never succeed."""
    brand = _brand(
        "Sri Venkateshwara Traders General Stores Provisions " * 2 + "and Sons Bengaluru"
    )
    brief = _brief("lower_third", "Weekend Sale", "Fresh stock", "Order now")
    res = await pipeline.layout_gate(brief, brief.units(), brand)
    assert res["ok"] is False and res["charged"] == 0
    assert res["reason"] == "brand_mark_does_not_fit"
    assert res["slides"][0]["about"] == "brand_mark"
    assert "too_many_lines:brandline" in res["slides"][0]["detail"]
    assert "do not shorten" in res["hint"] and "update_brand" in res["hint"]
    assert "Shorten the headline" not in res["hint"]


async def test_an_emoji_in_the_brand_name_is_named_not_called_too_long(chromium):
    """'Sri Stores ✨' refused every render as tofu:brandline, and the hint
    said the name was too long -- the agent asked for a SHORTER name, the
    owner sent 'Sri ✨', and it was refused again. The hint names the
    character and the one call that cures it."""
    brief = _brief("lower_third", "Weekend Sale", "Fresh stock", "Order now")
    res = await pipeline.layout_gate(brief, brief.units(), _brand("Sri Stores 大"))
    assert res["reason"] == "brand_mark_does_not_fit" and res["charged"] == 0
    assert "U+5927 ('大')" in res["hint"] and "update_brand(name=" in res["hint"]
    rest = res["hint"].replace("not too long", "")
    assert "not too long" in res["hint"] and "too long" not in rest
    assert pipeline._tofu_in_name([{"detail": ["tofu:brandline:U+200B"]}]) == ["U+200B"]
    assert pipeline._tofu_in_name([{"detail": ["too_many_lines:brandline"]}]) == []


async def test_update_brand_refuses_a_name_the_faces_cannot_set(monkeypatch):
    """The same bar at the door: a name is copy set on every creative, so a
    name with a pictograph is refused where a headline with one is."""
    import types
    from contextlib import contextmanager

    from app.agent import tools as T

    brand = types.SimpleNamespace(name="Old", template_prefs={}, palette={}, category="food")

    @contextmanager
    def scope():
        yield types.SimpleNamespace(get=lambda model, key: brand)

    monkeypatch.setattr(T, "session_scope", scope)
    ctx = types.SimpleNamespace(brand_id="b")
    res = await T._update_brand(ctx, {"name": "Sri Stores ✨"})
    assert res["ok"] is False and res["reason"] == "name_cannot_be_set"
    assert "U+2728" in res["error"] and brand.name == "Old", "nothing was saved"
    res = await T._update_brand(ctx, {"name": "  Sri   Stores "})
    assert res["ok"] and brand.name == "Sri Stores", "whitespace collapsed, as it is set"


async def test_a_typeface_that_did_not_load_is_not_blamed_on_the_copy(chromium, monkeypatch):
    monkeypatch.setattr(compose.settings, "compose_require_fonts", True)
    brand = _brand()
    brand.fonts = {"heading": "No Such Face Anywhere", "body": "Inter"}
    brief = _brief("lower_third", "Weekend Sale")
    res = await pipeline.layout_gate(brief, brief.units(), brand)
    assert res["reason"] == "brand_font_unavailable" and res["charged"] == 0
    assert "No Such Face Anywhere" in res["slides"][0]["detail"]
    assert "NOT a copy problem" in res["hint"]


def test_copy_crowding_the_mark_is_still_a_copy_problem():
    assert pipeline._about_the_mark("unsafe:brandline")
    assert pipeline._about_the_mark("too_many_lines:brandline")
    assert pipeline._about_the_mark("logo_not_loaded")
    assert pipeline._about_the_mark("logo_clearspace:edge")
    assert pipeline._about_the_mark("tofu:brandline:U+5927")
    assert not pipeline._about_the_mark("overlap:headline+logo")
    assert not pipeline._about_the_mark("logo_clearspace:subhead")
    assert not pipeline._about_the_mark("clipped:headline")
    assert not pipeline._about_the_mark("overflow:panel")
    # A container spilling follows whatever spilled; on its own it is copy.
    assert pipeline._blame(["overflow:panel", "clipped:brandline"]) == (True, ["clipped:brandline"])
    assert pipeline._blame(["overflow:panel", "clipped:headline"]) == (
        False, ["overflow:panel", "clipped:headline"]
    )  # fmt: skip
    assert pipeline._blame(["overflow:content"]) == (False, ["overflow:content"])
    assert pipeline._blame(["outside:card", "unsafe:logo"]) == (True, ["unsafe:logo"])


@pytest.mark.parametrize("template", ["split_card", "poster_stack", "frame_card"])
async def test_a_name_too_wide_for_the_panel_is_not_blamed_on_the_copy_either(chromium, template):
    """One 45-letter word for a name overflows the panel, the card or the
    content column, and 'overflow:panel' counted as a copy violation: the
    slide was 'copy', the agent was told to shorten a headline that had
    nothing to do with it, and shortening could never help."""
    brand = _brand("Venkateshwaratradersandgeneralstoresbengaluru")
    brief = _brief(template, "Weekend Sale", "Fresh stock", "Order now")
    res = await pipeline.layout_gate(brief, brief.units(), brand)
    assert res["reason"] == "brand_mark_does_not_fit" and res["charged"] == 0
    slide = res["slides"][0]
    assert slide["about"] == "brand_mark"
    assert "the copy is too long for the layout" not in slide["problems"]
    assert "a word in the brand name is wider than the layout" in slide["problems"]
    assert "Shorten the headline" not in res["hint"] and "update_brand" in res["hint"]


# --------------------------------------------------------------------------- #
# 6. Indic type has room, and boxes are real ink
# --------------------------------------------------------------------------- #
async def test_the_ink_of_two_lines_never_touches(chromium):
    """Hindi in Poppins at the shared 1.14 measured -20px between lines."""
    brief = _brief("centered_overlay", HINDI)
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    headline = _ink(report, "headline")
    assert len(headline["lines"]) >= 2 and headline["leading"] >= 1.38
    # the detector, with the leading forced shut where FIT_JS cannot open it
    with pytest.raises(compose.LayoutError) as err:
        await _layout_with(
            brief, _brand(), ".headline { line-height: .8 !important; max-width: 50% !important; }"
        )
    assert "linegap:headline" in err.value.violations


async def test_leading_opens_only_where_ink_would_really_collide(chromium):
    """A descender directly over an i-dot gets the room it needs; a Latin
    headline with nothing hanging over anything stays as tight as designed."""
    tight = _brief("split_card", "SUMMER SALE ENDS SUNDAY NIGHT AT TEN")
    report = await compose.check_layout(tight, tight.units()[0], _brand())
    assert len(_ink(report, "headline")["lines"]) >= 2
    assert _ink(report, "headline")["leading"] == 0.98
    hung = _brief("lower_third", "Cold-pressed groundnut oil is back in stock from this Friday")
    report = await compose.check_layout(hung, hung.units()[0], _brand())
    assert 0.98 < _ink(report, "headline")["leading"] <= 0.98 + compose.MAX_EXTRA_LEADING


@pytest.mark.parametrize(
    "headline", ["Happy gypsy yoga", HINDI, TAMIL_WORD], ids=["descenders", "hindi", "tamil"]
)
async def test_the_reported_box_holds_every_pixel_of_ink(chromium, headline):
    """The vertical extent came from the block box, which at line-height .98 is
    SHORTER than the letters: a 'y' hung 15px below the safe zone unnoticed.
    Rendered for real, then read back: no ink outside the box, none below the
    safe zone."""
    import numpy as np

    brief = _brief("lower_third", headline)
    png, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand(logo=_emblem((255, 255, 255, 255))), _flat((20, 20, 20)),
        "image/png",
    )  # fmt: skip
    box = _ink(report, "headline")["box"]
    with Image.open(io.BytesIO(png)) as im:
        bright = np.asarray(im.convert("L")) > 170
    w, h = brief.pixel_size()
    pad = compose.padding_for(w, h)
    assert box["b"] <= h - pad["pad_bottom"] + 1
    assert not bright[round(box["b"]) + 2 :, :].any(), "ink below the reported box"
    # between the logo (top) and the headline there is only the accent rule
    rows = np.where(bright[round(h * 0.3) :, :].any(axis=1))[0] + round(h * 0.3)
    assert rows.min() >= box["t"] - 2, "ink above the reported box"
    assert rows.max() <= box["b"] + 1 and rows.max() >= box["b"] - 4, "the box is the ink"


@pytest.mark.parametrize(
    "headline",
    ["ਵਿਸਾਖੀ ਦੀਆਂ ਲੱਖ ਲੱਖ ਵਧਾਈਆਂ", "দুর্গাপূজার শুভেচ্ছা", "महाशिवरात्रि"],
    ids=["punjabi", "bengali", "hindi"],
)
@pytest.mark.parametrize("template", ["lower_third", "split_card", "poster_stack"])
async def test_the_headbar_of_indic_type_stays_inside_the_safe_zone(chromium, template, headline):
    """The horizontal extent came from Range rects, which are advance boxes:
    the shirorekha of Gurmukhi, Bengali and Devanagari type starts a pixel or
    two left of the first letter's box, and 10-38 px of every such headline
    sat in the strip the safe zone keeps clear while `unsafe` held. The ink
    overhang is measured and the block padded by it, like the ascenders."""
    import numpy as np

    brief = _brief(template, headline)
    png, report = await compose.compose_with_report(
        brief, brief.units()[0], _brand("Kadamba"), _flat((20, 20, 20)), "image/png"
    )
    box = _ink(report, "headline")["box"]
    pad = compose.padding_for(*brief.pixel_size())["pad_x"]
    assert box["l"] >= pad - 0.5, box
    with Image.open(io.BytesIO(png)) as im:
        bright = np.asarray(im.convert("L")) > 140
    rows = bright[round(box["t"]) : round(box["b"]), :]
    assert not rows[:, :pad].any(), "ink left of the safe zone"
    assert rows[:, pad : pad + 3].any(), "the type still starts at the safe zone"


# --------------------------------------------------------------------------- #
# 7. copy the faces cannot set
# --------------------------------------------------------------------------- #
async def test_a_character_no_face_of_ours_covers_is_a_violation(chromium, monkeypatch):
    """The page's own check, the second gate. The brief refuses these first
    (below); the page must still refuse them on its own, because it is what
    stands between a brand NAME the brief never sees and a box on the frame."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="U\\+5927"):
        _brief("lower_third", "Sale 大 today", None, "Order ส now")
    real = compose.render_html

    def past_the_brief(*args, **kwargs):
        html = real(*args, **kwargs)
        return html.replace(">Weekend Sale<", ">Sale 大 today<").replace(
            ">Order now<", ">Order ส now<"
        )

    monkeypatch.setattr(compose, "render_html", past_the_brief)
    brief = _brief("lower_third", "Weekend Sale", None, "Order now")
    with pytest.raises(compose.LayoutError) as err:
        await compose.check_layout(brief, brief.units()[0], _brand())
    assert "tofu:headline:U+5927" in err.value.violations
    assert "tofu:cta:U+0E2A" in err.value.violations
    res = await pipeline.layout_gate(brief, brief.units(), _brand())
    assert res["reason"] == "copy_does_not_fit" and "U+" in res["hint"]
    monkeypatch.setattr(compose, "render_html", real)
    brief = _brief("lower_third", "Weekend Sale", None, "Order now")
    res = await pipeline.layout_gate(brief, brief.units(), _brand("大 Stores"))
    assert res["reason"] == "brand_mark_does_not_fit"
    assert res["slides"][0]["detail"] == ["tofu:brandline:U+5927"]


async def test_the_rupee_sign_and_ordinary_punctuation_are_covered(chromium):
    brief = _brief(
        "split_card", "Flat ₹249 — today only!", "50% off, “fresh” & hot… ½ price ™ ©", "Pay ₹249 •"
    )
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    assert report["violations"] == []


async def test_the_brief_and_the_compositor_agree_on_the_arrow(chromium):
    """Same character, same answer at both gates: the brief refuses 'Order
    now →' before anything is laid out, and the page -- given it anyway --
    refuses it as tofu. It used to pass the first and fail the second, and
    the agent was told to shorten the copy."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="U\\+2192"):
        _brief("lower_third", "Weekend Sale", None, "Order now →")
    brief = _brief("lower_third", "Weekend Sale", None, "Order now")
    slide = brief.units()[0]
    w, h = brief.pixel_size()
    browser = await compose.get_browser()
    page = await browser.new_page(viewport={"width": w, "height": h})
    try:
        html = compose.render_html(brief, slide, _brand(), compose._BLANK_BG)
        html = html.replace(">Order now<", ">Order now →<", 1)
        with pytest.raises(compose.LayoutError) as err:
            await compose._layout(page, brief, slide, _brand(), html)
        assert err.value.violations == ["tofu:cta:U+2192"]
    finally:
        await page.close()


async def test_urdu_is_laid_out_from_the_right(chromium):
    brief = _brief("lower_third", URDU, None, "ابھی")
    w, _ = brief.pixel_size()
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    pad = compose.padding_for(*brief.pixel_size())["pad_x"]
    for cls in ("headline", "cta", "brandline"):
        assert abs(report["boxes"][cls]["r"] - (w - pad)) <= 6, (cls, report["boxes"][cls])
    # and Latin copy is still laid out from the left
    brief = _brief("lower_third", "Weekend Sale", None, "Order now")
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    assert abs(report["boxes"]["headline"]["l"] - pad) <= 6


# --------------------------------------------------------------------------- #
# 8. hierarchy and line count
# --------------------------------------------------------------------------- #
async def test_a_subhead_as_big_as_its_headline_is_a_violation(chromium):
    brief = _brief("split_card", "Weekend Sale", "Cold-pressed, ghar jaisa shudh")
    with pytest.raises(compose.LayoutError) as err:
        await _layout_with(
            brief,
            _brand(),
            ".headline { font-size: 50px !important; } .subhead { font-size: 40px !important; }",
        )
    assert "hierarchy" in err.value.violations


async def test_a_headline_that_runs_to_a_paragraph_is_a_violation(chromium):
    brief = _brief("lower_third", "Cold pressed oil is back in stock from Friday for all of you")
    with pytest.raises(compose.LayoutError) as err:
        await _layout_with(brief, _brand(), ".headline { max-width: 40% !important; }")
    assert "too_many_lines:headline" in err.value.violations
    assert compose.MAX_LINES == {"headline": 4, "subhead": 3, "brandline": 2}
    assert compose.HEADLINE_MIN >= compose.HIERARCHY * compose.SUBHEAD_MIN - 0.002


# --------------------------------------------------------------------------- #
# 10. 2x for the delivered frame, 1x for the proof; floors; ordinary copy passes
# --------------------------------------------------------------------------- #
async def test_the_delivered_frame_is_rasterised_at_2x_and_the_layout_proof_at_1x(
    chromium, monkeypatch
):
    browser = await compose.get_browser()
    real, scales = browser.new_page, []

    async def new_page(**kw):
        scales.append(kw.get("device_scale_factor", 1))
        return await real(**kw)

    monkeypatch.setattr(browser, "new_page", new_page)
    brief = _brief("lower_third", "Weekend Sale", "Cold-pressed", "Order now")
    await compose.check_layout(brief, brief.units()[0], _brand())
    png = await compose.compose(
        brief, brief.units()[0], _brand(), _flat((90, 110, 100)), "image/png"
    )
    assert scales == [1, 2]
    assert compose.image_size(png) == brief.pixel_size(), "Lanczos back to the export size"


def test_the_smallest_type_survives_a_platform_re_encode():
    assert compose.SUBHEAD_MIN == compose.CTA_MIN == 0.030
    assert compose.BRANDLINE_MIN >= 0.026
    assert compose._fit_config(1080, 1350)["min"] == {
        "headline": 48, "subhead": 32, "cta": 32, "brandline": 28,
    }  # fmt: skip


@pytest.mark.parametrize("kind", ["single", "story"])
@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_ordinary_copy_is_set_at_its_design_size_everywhere(chromium, template, kind):
    """A guarantee that refuses a valid job is a bug too. The brief's own
    example, in every layout, as a post and a story, with no logo, an emblem
    and a wordmark: no violations, and nothing had to shrink."""
    wordmark = io.BytesIO()
    Image.new("RGBA", (900, 200), (255, 255, 255, 255)).save(wordmark, "PNG")
    for mark in ("none", "emblem", "wordmark"):
        brand = _brand()
        if mark != "none":
            data = _emblem((255, 255, 255, 255)) if mark == "emblem" else wordmark.getvalue()
            brand.logo_src = compose.as_data_uri(data, "image/png")
            brand.logo_analysis = {"has_wordmark": mark == "wordmark"}
        brief = _brief(template, EXAMPLE["headline"], EXAMPLE["subhead"], EXAMPLE["cta"], kind)
        report = await compose.check_layout(brief, brief.units()[0], brand)
        assert report["violations"] == [], (template, kind, mark)
        for cls, size in report["sizes"].items():
            assert size["px"] == size["design"], (template, kind, mark, cls, size)
