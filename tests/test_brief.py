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
