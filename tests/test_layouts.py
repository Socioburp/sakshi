"""Six layouts, three aspects, one brand kit: everything fits and is grid-safe."""

from __future__ import annotations

import io
import json
import types

import pytest

from app.creative import brandkit, compose
from app.creative.brief import EXAMPLE, POST_SIZE, CreativeBrief
from app.creative.imagegen.base import ImageRequest
from app.creative.imagegen.providers import MockImageProvider

LONG_HEADLINE = "Cold-pressed groundnut oil, back in stock this weekend"
LONG_SUBHEAD = "Small batches, pressed on Tuesdays and Fridays in Indiranagar"


def test_grid_insets_follow_the_3_4_profile_grid():
    assert compose.grid_insets(1080, 1350) == (34, 0)  # 4:5 loses a thin strip each side
    assert compose.grid_insets(1080, 1080) == (135, 0)  # 1:1 loses 12.5% each side
    assert compose.grid_insets(1080, 1920) == (0, 240)  # 9:16 loses a band top and bottom
    pad = compose.padding_for(1080, 1080)
    assert pad["pad_x"] >= 135 + 32 and pad["pad_top"] == compose.SAFE_PAD
    # The post size: the 34px grid trim is cleared by the 90px safe zone.
    post = compose.padding_for(*POST_SIZE)
    assert compose.SAFE_PAD == 90 and min(post.values()) >= compose.SAFE_PAD
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


# --------------------------------------------------------------------------- #
# the deterministic guarantees
# --------------------------------------------------------------------------- #
AT_LIMIT_HEADLINE = "Cold-pressed groundnut oil is back in stock from this Friday"  # 60
AT_LIMIT_SUBHEAD = (
    "Small batches pressed on Tuesdays and Fridays in Indiranagar, bottled the same evening, fresh"
)
AT_LIMIT_CTA = "Order on WhatsApp 98450 12345"
assert len(AT_LIMIT_HEADLINE) == 60 and 90 <= len(AT_LIMIT_SUBHEAD) <= 100

COPY = {
    "very_short_headline": ("Sale", None, "Shop"),
    "long_headline": (LONG_HEADLINE, LONG_SUBHEAD, "Order on WhatsApp"),
    "headline_at_the_character_limit": (AT_LIMIT_HEADLINE, None, None),
    "multi_line_subhead": ("Weekend Sale", AT_LIMIT_SUBHEAD, "Order now"),
    "cta_overflow": ("Weekend Sale", None, AT_LIMIT_CTA),
    "everything_at_its_limit": (AT_LIMIT_HEADLINE, AT_LIMIT_SUBHEAD, AT_LIMIT_CTA),
    "one_unbreakable_word": ("Supercalifragilisticexpialidocious", None, None),
}


def _logo(kind: str) -> bytes:
    from PIL import Image, ImageDraw

    size = (900, 200) if kind == "wordmark" else (400, 400)
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([0, 0, size[0] - 1, size[1] - 1], fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _brand_with(logo: str, signature: str = "corner"):
    brand = _brand(signature)
    if logo != "none":
        brand.logo_src = compose.as_data_uri(_logo(logo), "image/png")
        brand.logo_analysis = {"has_wordmark": logo == "wordmark"}
    return brand


def _brief(template: str, copy, kind: str = "single") -> CreativeBrief:
    payload = json.loads(json.dumps(EXAMPLE))
    headline, subhead, cta = copy
    payload.update(template_id=template, headline=headline, subhead=subhead, cta=cta)
    payload["format"] = {"type": kind}
    return CreativeBrief.model_validate(payload)


@pytest.fixture
async def chromium():
    try:
        await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium: {exc}")
    yield
    await compose.shutdown()


def _hit(a, b) -> bool:
    return (
        a["l"] < b["r"] - 1 and b["l"] < a["r"] - 1 and a["t"] < b["b"] - 1 and b["t"] < a["b"] - 1
    )


@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_every_layout_holds_every_guarantee_for_every_copy_shape(chromium, template):
    """Short, long, at the limit, multi-line, an overlong CTA, one giant word;
    with no mark, an emblem and a wordmark; as a post and as a reel. The
    compositor never has to give up, and the geometry is re-checked here in
    Python so the test does not simply trust the page's own verdict."""
    combos = [("single", n, m) for n in COPY for m in ("emblem", "wordmark")]
    combos += [("single", "everything_at_its_limit", "none")]
    combos += [
        ("reel", "everything_at_its_limit", "wordmark"),
        ("reel", "very_short_headline", "emblem"),
    ]
    for kind, name, logo in combos:
        for copy in (COPY[name],):
            for _ in (0,):
                brief = _brief(template, copy, kind)
                report = await compose.check_layout(brief, brief.units()[0], _brand_with(logo))
                where = (template, kind, name, logo)
                assert report["violations"] == [], where
                w, h = brief.pixel_size()
                pad = compose.padding_for(w, h)
                boxes = report["boxes"]
                for cls, b in boxes.items():
                    if cls == "rule":
                        continue
                    # inside the safe zone: never cropped, never in the grid trim
                    assert b["l"] >= pad["pad_x"] - 1 and b["r"] <= w - pad["pad_x"] + 1, (
                        where,
                        cls,
                    )
                    assert b["t"] >= pad["pad_top"] - 1, (where, cls)
                    assert b["b"] <= h - pad["pad_bottom"] + 1, (where, cls)
                names = list(boxes)
                for i, a in enumerate(names):
                    for b in names[i + 1 :]:
                        assert not _hit(boxes[a], boxes[b]), (where, a, b)
                for cls, size in report["sizes"].items():
                    floor = getattr(compose, f"{cls.upper()}_MIN") * w
                    assert size["px"] >= min(floor, size["design"]) - 1, (where, cls, size)
                    assert size["px"] <= size["design"] + 0.01, (where, cls, size)


async def test_the_fit_search_gives_back_what_an_element_did_not_need_to_lose(chromium):
    """An overlong CTA is the CTA's problem. The headline keeps its size."""
    brief = _brief("split_card", COPY["cta_overflow"])
    report = await compose.check_layout(brief, brief.units()[0], _brand_with("wordmark"))
    assert report["sizes"]["headline"]["px"] == report["sizes"]["headline"]["design"]


async def test_the_logo_keeps_its_width_and_its_clear_space_in_every_layout(chromium):
    widths = set()
    for template in compose.TEMPLATES:
        brief = _brief(template, COPY["long_headline"])
        report = await compose.check_layout(brief, brief.units()[0], _brand_with("emblem"))
        logo = report["boxes"]["logo"]
        widths.add(round(logo["r"] - logo["l"]))
        clear = compose.LOGO_CLEAR * (logo["b"] - logo["t"])
        grown = {"l": logo["l"] - clear, "t": logo["t"] - clear,
                 "r": logo["r"] + clear, "b": logo["b"] + clear}  # fmt: skip
        for cls, box in report["boxes"].items():
            if cls != "logo":
                assert not _hit(grown, box), (template, cls)
    assert widths == {round(1080 * compose.LOGO_WIDTH["emblem"])}, "one size, every layout"


async def test_copy_that_cannot_fit_fails_the_render_and_never_ships(chromium, monkeypatch):
    """Below the type floor there is no output at all: not a smaller face, not
    a cropped one, not an ellipsis."""
    monkeypatch.setattr(compose, "SAFE_PAD", 420)  # a canvas nothing long can be set in
    brief = _brief("frame_card", COPY["everything_at_its_limit"])
    slide, brand = brief.units()[0], _brand_with("wordmark")
    with pytest.raises(compose.LayoutError) as err:
        await compose.check_layout(brief, slide, brand)
    assert err.value.violations and err.value.position == 1
    bg = await MockImageProvider().generate(ImageRequest(prompt="x", width=1600, height=2000))
    with pytest.raises(compose.LayoutError):
        await compose.compose(brief, slide, brand, bg.data)


async def test_the_layout_gate_stops_the_job_before_any_charge(chromium, monkeypatch):
    from app.creative import pipeline

    monkeypatch.setattr(compose, "SAFE_PAD", 420)
    brief = _brief("frame_card", COPY["everything_at_its_limit"])
    res = await pipeline.layout_gate(brief, brief.units(), _brand_with("wordmark"))
    assert res["ok"] is False and res["reason"] == "copy_does_not_fit" and res["charged"] == 0
    assert res["slides"][0]["slide"] == 1 and res["slides"][0]["problems"]
    monkeypatch.undo()
    assert await pipeline.layout_gate(brief, brief.units(), _brand_with("wordmark")) is None


async def test_a_chromium_crash_costs_one_retry_not_the_creative(chromium, monkeypatch):
    calls = {"n": 0}
    real = compose._check_layout_once

    async def dies_once(brief, slide, brand):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Page.set_content: Target closed")
        return await real(brief, slide, brand)

    monkeypatch.setattr(compose, "_check_layout_once", dies_once)
    brief = _brief("lower_third", COPY["long_headline"])
    report = await compose.check_layout(brief, brief.units()[0], _brand_with("none"))
    assert calls["n"] == 2 and report["violations"] == []


async def test_a_refusal_is_a_verdict_and_is_never_retried(chromium, monkeypatch):
    calls = {"n": 0}

    async def refuses(brief, slide, brand):
        calls["n"] += 1
        raise compose.LayoutError(["overflow:panel"], position=1)

    monkeypatch.setattr(compose, "_check_layout_once", refuses)
    brief = _brief("split_card", COPY["long_headline"])
    with pytest.raises(compose.LayoutError):
        await compose.check_layout(brief, brief.units()[0], _brand_with("none"))
    assert calls["n"] == 1


def test_nothing_truncates_and_nothing_ellipsises_anywhere():
    import pathlib

    root = pathlib.Path(compose.TEMPLATE_DIR)
    for path in root.glob("*.j2"):
        css = path.read_text(encoding="utf-8")
        for banned in ("text-overflow", "ellipsis", "line-clamp", "\u2026"):
            assert banned not in css, (path.name, banned)
    # A text container may not hide what spills out of it.
    for name in ("split_card", "frame_card", "top_band", "lower_third", "poster_stack"):
        css = (root / f"{name}.html.j2").read_text(encoding="utf-8")
        for block in (".panel {", ".card {", ".band {", ".content {"):
            if block in css:
                rule = css.split(block, 1)[1].split("}", 1)[0]
                assert "overflow: hidden" not in rule, (name, block)


@pytest.mark.parametrize("template", ["lower_third", "frame_card", "top_band"])
async def test_no_text_pixels_in_the_strip_the_profile_grid_trims(chromium, template):
    """Rendered for real over a flat ground, then read back: in the 34px the
    grid trims from each side, every row is one colour. A glyph, a logo edge
    or a CTA corner in that strip would break the row."""
    from PIL import Image

    flat = io.BytesIO()
    Image.new("RGB", (1600, 2000), (90, 110, 100)).save(flat, "PNG")
    brief = _brief(template, COPY["everything_at_its_limit"])
    png = await compose.compose(
        brief, brief.units()[0], _brand_with("wordmark", "none"), flat.getvalue(), "image/png"
    )
    im = Image.open(io.BytesIO(png)).convert("L")
    assert im.size == (1080, 1350)
    trim = compose.grid_insets(1080, 1350)[0]
    assert trim == 34
    for x0 in (0, 1080 - trim):
        strip = im.crop((x0, 0, x0 + trim, 1350))
        px = strip.load()
        for y in range(0, 1350, 3):
            row = [px[x, y] for x in range(trim)]
            # film grain moves a row a few
            # levels; type on a dark ground moves it by a hundred or more.
            assert max(row) - min(row) <= 40, (template, x0, y, max(row) - min(row))


async def test_a_brand_face_that_does_not_load_refuses_the_render(chromium, monkeypatch):
    monkeypatch.setattr(compose.settings, "compose_require_fonts", True)
    brand = _brand_with("none")
    brand.fonts = {"heading": "No Such Face Anywhere", "body": "Inter"}
    brief = _brief("lower_third", COPY["long_headline"])
    with pytest.raises(compose.BrandFontUnavailable, match="No Such Face Anywhere"):
        await compose.check_layout(brief, brief.units()[0], brand)


def test_the_scrim_is_measured_under_the_words_not_in_a_fixed_band():
    """Bright behind the text block, dark everywhere else: a band average says
    'fine', the measured box says 'this needs help'."""
    from PIL import Image

    from app.creative import legibility

    im = Image.new("RGB", (400, 500), (20, 20, 20))
    im.paste((245, 245, 245), (40, 300, 360, 420))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    box = (0.1, 0.6, 0.9, 0.84)
    boost, local, why = legibility.scrim_for_box(buf.getvalue(), box)
    assert boost > 1.3 and 0 < local <= legibility.LOCAL_MAX and why["box"] == list(box)
    calm, none, _ = legibility.scrim_for_box(buf.getvalue(), (0.1, 0.05, 0.9, 0.3))
    assert calm < 1.0 and none == 0.0
    assert legibility.scrim_for_box(b"not an image", box)[:2] == (1.0, 0.0)
