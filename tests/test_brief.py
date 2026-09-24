import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief, check_brand_rules

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "brief_schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text())


def test_examples_are_valid():
    b = CreativeBrief.model_validate(EXAMPLE)
    assert b.pixel_size() == (1080, 1350)
    assert b.is_carousel() is False
    assert len(b.units()) == 1

    c = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    assert c.is_carousel() and c.format.slide_count == 3
    assert [s.position for s in c.units()] == [1, 2, 3]


def test_matches_canonical_schema_limits():
    """The pydantic model must not drift from docs/brief_schema.json."""
    props = SCHEMA["properties"]
    fields = CreativeBrief.model_fields
    assert set(SCHEMA["required"]) <= set(fields)
    for name in ("headline", "subhead", "cta", "alt_text"):
        want = props[name]["maxLength"]
        got = next(m.max_length for m in fields[name].metadata if hasattr(m, "max_length"))
        assert got == want, f"{name}: model allows {got}, schema says {want}"
    model_intents = set(CreativeBrief.model_fields["intent"].annotation.__args__)
    assert set(props["intent"]["enum"]) == model_intents

    # visual_direction: the limits the code enforces are the ones the doc states.
    from app.creative.brief import MOOD_MAX, VisualDirection

    vd_doc = props["visual_direction"]["properties"]
    vd_fields = VisualDirection.model_fields

    def limit(field, attr):
        return next((getattr(m, attr) for m in field.metadata if hasattr(m, attr)), None)

    assert limit(vd_fields["prompt"], "max_length") == vd_doc["prompt"]["maxLength"]
    assert limit(vd_fields["prompt"], "min_length") == vd_doc["prompt"]["minLength"]
    assert limit(vd_fields["mood"], "max_length") == vd_doc["mood"]["maxLength"] == MOOD_MAX
    assert "seed" in vd_doc and "seed" in vd_fields
    assert props["format"]["properties"]["slide_count"]["maximum"] == 6


@pytest.mark.parametrize(
    "prompt",
    [
        "a shop front with the text SALE on the wall",
        "a bottle with a price tag showing 249",
        'a poster that says "Weekend Sale" in bold typography',
        "a banner with the brand logo in the corner",
        "a product shot with a watermark in the corner",
    ],
)
def test_visual_prompt_rejects_text(prompt):
    payload = {**EXAMPLE, "visual_direction": {"prompt": prompt}}
    with pytest.raises(ValidationError):
        CreativeBrief.model_validate(payload)


def test_visual_prompt_accepts_pure_imagery():
    ok = "a clay pot of turmeric on a jute mat, side light, shallow depth of field"
    b = CreativeBrief.model_validate({**EXAMPLE, "visual_direction": {"prompt": ok}})
    assert b.visual_direction.prompt == ok


def test_headline_limit_is_sixty():
    CreativeBrief.model_validate({**EXAMPLE, "headline": "x" * 60})
    with pytest.raises(ValidationError):
        CreativeBrief.model_validate({**EXAMPLE, "headline": "x" * 61})


def test_carousel_requires_slides():
    with pytest.raises(ValidationError):
        CreativeBrief.model_validate(
            {**EXAMPLE, "format": {"type": "carousel", "aspect_ratio": "1:1"}}
        )


def test_slides_rejected_on_single_post():
    with pytest.raises(ValidationError):
        CreativeBrief.model_validate({**EXAMPLE, "slides": EXAMPLE_CAROUSEL["slides"]})


def test_duplicate_slide_positions_rejected():
    bad = json.loads(json.dumps(EXAMPLE_CAROUSEL))
    bad["slides"][1]["position"] = 1
    with pytest.raises(ValidationError):
        CreativeBrief.model_validate(bad)


def test_hashtags_normalised_and_deduped():
    b = CreativeBrief.model_validate(
        {
            **EXAMPLE,
            "caption": {
                "body": "hi",
                "hashtags": ["coconut oil!", "#CoconutOil", " ", "blr"],
            },
        }
    )
    assert b.caption.hashtags == ["#coconutoil", "#blr"]


def test_never_say_gate_covers_slides():
    class B:
        never_say = ["cheap", "guaranteed"]

    bad = json.loads(json.dumps(EXAMPLE_CAROUSEL))
    bad["slides"][2]["headline"] = "Cheap and easy"
    brief = CreativeBrief.model_validate(bad)
    assert check_brand_rules(brief, B()) == ["cheap"]


def test_carousel_slide_limits_are_two_to_six():
    import json as _json

    from app.creative.brief import CAROUSEL_MAX, CAROUSEL_MIN

    assert (CAROUSEL_MIN, CAROUSEL_MAX) == (2, 6)
    base = EXAMPLE_CAROUSEL["slides"][0]

    def with_slides(n):
        payload = _json.loads(_json.dumps(EXAMPLE_CAROUSEL))
        payload["slides"] = [{**base, "position": i, "headline": f"S{i}"} for i in range(1, n + 1)]
        payload["format"]["slide_count"] = n
        return payload

    for n in (2, 6):
        assert len(CreativeBrief.model_validate(with_slides(n)).slides) == n
    for n in (1, 7):
        with pytest.raises(ValidationError):
            CreativeBrief.model_validate(with_slides(n))


def test_schema_and_model_agree_on_the_carousel_cap():
    assert SCHEMA["properties"]["slides"]["maxItems"] == 6
    assert SCHEMA["properties"]["format"]["properties"]["slide_count"]["maximum"] == 6


# --------------------------------------------------------------------------- #
# copy the faces cannot set is stopped here, before any layout or charge
# --------------------------------------------------------------------------- #
PARTY, FIRE, SPARKLES, STAR = chr(0x1F389), chr(0x1F525), chr(0x2728), chr(0x2605)
VS16, ZWJ, ZWNJ, RLO = chr(0xFE0F), chr(0x200D), chr(0x200C), chr(0x202E)


@pytest.mark.parametrize(
    "field,value",
    [
        ("headline", f"Big Sale {PARTY}{FIRE}"),
        ("headline", f"New {SPARKLES} arrivals"),
        ("subhead", f"5{STAR} rated by our customers"),
        ("cta", f"Order now {chr(0x27A1)}{VS16}"),
        ("headline", f"Family {chr(0x1F468)}{ZWJ}{chr(0x1F469)} pack"),
        ("headline", f"1{VS16}{chr(0x20E3)} day only"),
        ("headline", f"Sale {chr(0x1F1EE)}{chr(0x1F1F3)}"),
        ("subhead", f"Fresh {RLO}stock"),
        ("cta", "Order\x07 now"),
    ],
)
def test_emoji_pictographs_and_control_characters_are_refused_in_composited_copy(field, value):
    """No vendored face has them. They were set in whatever emoji font the
    render host had -- unmeasured, unbranded, different on every machine."""
    with pytest.raises(ValidationError) as err:
        CreativeBrief.model_validate({**EXAMPLE, field: value})
    message = str(err.value)
    assert field in message and "U+" in message
    assert "Rewrite" in message and "caption.body" in message, "something the agent can act on"


def test_a_slide_is_held_to_the_same_rule():
    payload = json.loads(json.dumps(EXAMPLE_CAROUSEL))
    payload["slides"][1]["subhead"] = f"No smell {FIRE}"
    with pytest.raises(ValidationError, match="cannot set"):
        CreativeBrief.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cta", "Order now \u2192"),
        ("headline", "\u2190 Swipe for more"),
        ("subhead", "Stock \u2265 100 pieces, \u221e choice"),
        ("headline", "Fresh \u25a0 Local \u25cf Pure"),
    ],
)
def test_a_symbol_no_vendored_face_carries_is_refused_at_the_brief(field, value):
    """'Order now \u2192' passed the brief -- the arrow is in no pictograph block --
    and was then refused by the compositor as tofu:cta:U+2192, with the agent
    told the COPY did not fit. Google Fonts' Latin subset carries \u2191 and \u2193 only;
    what the brief refuses is read off the vendored faces' own declarations."""
    with pytest.raises(ValidationError) as err:
        CreativeBrief.model_validate({**EXAMPLE, field: value})
    message = str(err.value)
    assert field in message and "U+" in message and "arrows" in message and "Rewrite" in message


def test_the_coverage_check_reads_the_vendored_faces():
    from app.creative import fonts

    ranges = fonts.vendored_ranges()
    assert (0x2191, 0x2191) in ranges["Inter"] and (0x2193, 0x2193) in ranges["Poppins"]
    assert any(lo <= 0x0B95 <= hi for lo, hi in ranges["Noto Sans Tamil"]), "Tamil KA"
    assert fonts.unsettable("Order now \u2192 \u2190") == ["\u2192", "\u2190"]
    punctuation = "Flat \u20b9249 \u2014 \u201cfresh\u201d & hot\u2026 \u00bd \u00d7 \u2122 \u00a9"
    assert fonts.unsettable(punctuation + " \u2022 \u2116") == []
    tamil = "\u0b87\u0ba9\u0bcd\u0bb1\u0bc1 \u0b86\u0bb0\u0bcd\u0b9f\u0bb0\u0bcd \u20b9249"
    assert fonts.unsettable(tamil) == []
    assert fonts.unsettable("  \u200d\u200c\u00a0") == [], "nothing invisible is ever tofu"


def test_ordinary_copy_in_every_script_is_untouched():
    hindi_with_joiner = (
        f"{chr(0x0915)}{chr(0x094D)}{ZWJ}{chr(0x0937)} {chr(0x0930)}{chr(0x094D)}{ZWNJ}"
    )
    for headline in (
        "Flat \u20b9249 \u2014 today only!",
        "50% off, \u201cfresh\u201d & hot\u2026 \u00bd price \u2122",
        "\u0906\u091c \u0939\u0940 \u0911\u0930\u094d\u0921\u0930 \u0915\u0930\u0947\u0902",
        "\u0b87\u0ba9\u0bcd\u0bb1\u0bc1 \u0b86\u0bb0\u0bcd\u0b9f\u0bb0\u0bcd",
        "\u0622\u062c \u06c1\u06cc \u0622\u0631\u0688\u0631",
        hindi_with_joiner,
    ):
        assert CreativeBrief.model_validate({**EXAMPLE, "headline": headline}).headline == headline
    # Emoji belong in the caption, which Instagram sets, not the compositor.
    caption = {"body": f"Sale! {PARTY}", "hashtags": [], "language": "en"}
    assert CreativeBrief.model_validate({**EXAMPLE, "caption": caption}).caption.body.endswith(
        PARTY
    )


def test_whitespace_in_copy_is_collapsed_the_way_the_browser_will():
    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "headline": "  Weekend\n Sale\t", "subhead": None, "cta": " Order   now "}
    )
    assert (brief.headline, brief.subhead, brief.cta) == ("Weekend Sale", None, "Order now")
    with pytest.raises(ValidationError):
        CreativeBrief.model_validate({**EXAMPLE, "headline": " \n "})


def test_a_slide_with_a_blank_headline_is_refused_like_the_post():
    """Slide.headline had no floor: three spaces collapsed to '' and the slide
    was composed and charged with no headline at all."""
    payload = json.loads(json.dumps(EXAMPLE_CAROUSEL))
    payload["slides"][1]["headline"] = "   "
    with pytest.raises(ValidationError, match="empty once whitespace"):
        CreativeBrief.model_validate(payload)
