"""The onboarding kit: a brand starts from our own team's work, not from nothing.

A new paying client has no votes and no approved posts, so every learned lane
in the product is silent for their first creatives -- exactly when "perfect
first time" is what they are paying for. The kit fixes that by hand: staff
upload the reference creatives our designers made and the raw product photos,
and this is what those two folders are allowed to turn into.

The tests are named after the failure each one pins. The loudest of them is
the reference creative: it is a finished post, with its own headline and its
own logo, and the day one of those is composited over or handed to the image
model the client gets a creative with two headlines on it.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest
from PIL import Image

from app.creative import brandkit, compose, photoref, refstyle, shotplan


@dataclass
class Asset:
    id: str
    kind: str = "product"
    label: str | None = None
    width: int | None = 1600
    height: int | None = 1600


def _image(size=(1600, 2000), colour=(180, 150, 120)) -> bytes:
    """A fixture with real texture in it: a flat fill reads as soft to the
    quality pass, which would make every fixture a rejected photo."""
    im = Image.new("RGB", size, colour)
    px = im.load()
    for y in range(0, size[1], 3):
        for x in range(0, size[0], 3):
            px[x, y] = (colour[0] // 2, colour[1], colour[2] // 3)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return out.getvalue()


# --------------------------------------------------------------------------- #
# a reference creative is never a picture to build on
# --------------------------------------------------------------------------- #
def test_a_reference_creative_is_never_usable_as_a_photograph():
    """It already carries a headline and a logo: compositing over it ships two."""
    ref = Asset(id="ref", kind="reference", label="diwali mithai box post")
    assert not photoref.is_usable(ref)
    assert "reference" not in photoref.USABLE_KINDS


def test_a_reference_creative_never_wins_a_slide_however_well_it_matches():
    """The label of a reference names the product, so scoring alone would pick it."""
    ref = Asset(id="ref", kind="reference", label="kaju katli mithai box diwali")
    photo = Asset(id="photo", kind="product", label="kaju katli box")
    copy = "Kaju katli mithai box this Diwali"
    assert photoref.score(copy, "", ref) > photoref.score(copy, "", photo)
    assert photoref.choose(copy, [ref]) is None
    assert photoref.choose(copy, [ref, photo]) == "photo"


def test_the_photo_query_excludes_reference_assets():
    """_resolve_photos must not even load one: an explicit id cannot then slip through."""
    import uuid

    from app.creative.pipeline import _resolve_photos

    seen: dict[str, object] = {}

    class _Scalars:
        def __init__(self, stmt):
            seen["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        def __iter__(self):
            return iter(())

    class _Db:
        def scalars(self, stmt):
            return _Scalars(stmt)

    brief = type("B", (), {"headline": "x"})()
    assert _resolve_photos(_Db(), uuid.uuid4(), brief, []) == {}
    sql = seen["sql"]
    assert "'logo'" in sql and "'reference'" in sql and "NOT IN" in sql.upper()


# --------------------------------------------------------------------------- #
# the style pass: strict, or there is no style
# --------------------------------------------------------------------------- #
GOOD = {
    "layout": "lower_third",
    "type_place": "solid_panel",
    "palette": ["#1F5B3D", "#F3E7D3"],
    "mood": ["calm", "premium"],
    "product": "whole",
    "light": "moody",
    "summary": "A dark bottle low in the frame with the words on a cream panel.",
}


def _answer(**over) -> str:
    return "Here you go:\n```json\n" + json.dumps({**GOOD, **over}) + "\n```"


def test_every_layout_family_the_style_pass_may_name_is_a_real_template():
    """A seeded family naming a template the compositor does not have is a refused
    render on the client's first creative."""
    assert set(refstyle.LAYOUTS) == set(compose.TEMPLATES)
    assert all(light in shotplan.SHOOT_STYLES for light in refstyle.LIGHTS)


def test_a_good_answer_parses_even_wrapped_in_prose():
    ref = refstyle.parse(_answer())
    assert ref.layout == "lower_third" and ref.light == "moody"
    assert ref.palette == ["#1F5B3D", "#F3E7D3"]


def test_a_missing_key_is_refused_not_defaulted():
    for key in GOOD:
        short = {k: v for k, v in GOOD.items() if k != key}
        with pytest.raises(refstyle.ReferenceUnreadable):
            refstyle.parse(json.dumps(short))


def test_a_layout_family_we_cannot_render_is_refused():
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse(_answer(layout="magazine_spread"))
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse(_answer(light="golden_hour"))


def test_a_colour_that_is_not_a_hex_is_refused():
    for bad in (["forest green"], ["#1F5B3"], ["#GGGGGG"], []):
        with pytest.raises(refstyle.ReferenceUnreadable):
            refstyle.parse(_answer(palette=bad))


def test_no_json_at_all_is_refused():
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse("I cannot describe this image.")
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse("{layout: lower_third,}")


async def test_the_style_pass_refuses_rather_than_guesses_with_no_model(monkeypatch):
    monkeypatch.setattr(refstyle.settings, "anthropic_model", "")
    with pytest.raises(refstyle.ReferenceUnreadable):
        await refstyle.describe(_image())


# --------------------------------------------------------------------------- #
# the aggregate: one reference set -> one brand kit
# --------------------------------------------------------------------------- #
def _ref(layout="lower_third", light="moody", place="solid_panel", product="whole", palette=None):
    return refstyle.Reference(
        layout=layout,
        type_place=place,
        palette=palette or ["#1F5B3D", "#F3E7D3"],
        mood=["calm"],
        product=product,
        light=light,
        summary="a post",
    )


def test_the_aggregate_takes_the_dominant_layout_light_and_palette():
    kit = refstyle.aggregate(
        [
            _ref(),
            _ref(),
            _ref(layout="frame_card", light="bright_airy"),
            _ref(palette=["#215E40", "#FFFFFF"]),
        ]
    )
    assert kit.layout == "lower_third" and kit.light == "moody"
    # #1F5B3D and #215E40 are the same green to everyone except ==.
    assert kit.palette[0] == "#1F5B3D"
    assert kit.counts["layout"] == {"lower_third": 3, "frame_card": 1}


def test_a_set_that_agrees_on_nothing_seeds_no_standing_rule():
    """Four posts that each do something different are four posts, not a house style."""
    kit = refstyle.aggregate(
        [
            _ref(layout="lower_third", product="whole"),
            _ref(layout="frame_card", product="detail"),
            _ref(layout="top_band", product="in_context"),
            _ref(layout="poster_stack", product="flat_lay"),
        ]
    )
    assert not any("built as" in lesson for lesson in kit.lessons)
    assert not any("product is shown" in lesson for lesson in kit.lessons)
    assert kit.layout in refstyle.LAYOUTS  # a kit is still decided, deterministically


def test_the_lessons_are_the_sentences_the_agent_will_read():
    kit = refstyle.aggregate([_ref(), _ref(), _ref()])
    assert "their posts are built as lower_third -- keep new ones in that family" in kit.lessons
    assert "their posts set the words on a solid panel" in kit.lessons
    assert "their product is shown whole" in kit.lessons
    assert len(kit.lessons) <= refstyle.MAX_LESSONS


def test_the_kit_is_the_same_whatever_order_the_folder_lists_files_in():
    refs = [_ref(), _ref(layout="frame_card"), _ref(layout="top_band"), _ref()]
    first = refstyle.aggregate(refs)
    assert refstyle.aggregate(list(reversed(refs))).lessons == first.lessons


# --------------------------------------------------------------------------- #
# the kit lands on the brand
# --------------------------------------------------------------------------- #
class _Brand:
    def __init__(self, **kw):
        self.name = kw.get("name", "Anaya Foods")
        self.category = kw.get("category", "sweets")
        self.palette = kw.get("palette")
        self.fonts = None
        self.template_prefs = kw.get("template_prefs")


def test_the_reference_set_beats_the_category_guess():
    """The category picks a look from a word. The reference set is our own team's
    finished work for THIS brand, so it is the better evidence."""
    brand = _Brand(category="sweets")
    assert brandkit.pick("sweets").key == "warm"
    kit = refstyle.aggregate([_ref(layout="frame_card", light="bright_airy")] * 3)
    report = brandkit.seed_from_references(brand, kit)
    prefs = brand.template_prefs
    assert prefs["family"][0] == "frame_card"
    assert prefs["shoot"] == "bright_airy"
    assert prefs["look"] == report["look"] == "editorial"
    assert brand.fonts["heading"] == brandkit.LOOKS["editorial"].heading
    assert prefs["lessons"] == kit.lessons


def test_the_measured_logo_colour_survives_a_reference_set_that_disagrees():
    """The logo palette is counted pixels; the reference palette is a model reading
    a compressed post. Where they differ the pixels win the primary."""
    brand = _Brand(palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"})
    kit = refstyle.aggregate([_ref(palette=["#C81E5B"])] * 3)
    report = brandkit.seed_from_references(brand, kit)
    assert brand.palette["primary"] == "#123B2E"
    assert brand.palette["accent"] == "#C81E5B"
    assert "kept" in report["palette"]

    agreeing = _Brand(palette={"primary": "#1F5B3D", "ink": "#FFFFFF"})
    report = brandkit.seed_from_references(agreeing, refstyle.aggregate([_ref()] * 3))
    assert agreeing.palette["primary"] == "#1F5B3D"
    assert report["palette"] == "confirmed by the reference set"


def test_a_brand_with_no_logo_yet_takes_the_reference_colours():
    brand = _Brand(palette=None)
    brandkit.seed_from_references(brand, refstyle.aggregate([_ref()] * 3))
    assert brand.palette["primary"] == "#1F5B3D" and brand.palette["accent"] == "#F3E7D3"
