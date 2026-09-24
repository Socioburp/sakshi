"""The bot says what it knew -- and ONLY what it knew.

The differentiator is that Sakshi understands the brand. That is worth nothing
if the owner cannot see it, and worth less than nothing if the bot ever claims
to remember something it did not use. Every fact is derived in code; these
tests are mostly about what must NOT be said.
"""

from __future__ import annotations

import types

from app.creative import remembered
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief


def _mem(content: str):
    return types.SimpleNamespace(content=content)


def _grounding(**lanes):
    return types.SimpleNamespace(hits=lanes)


def _kinds(facts):
    return [f["kind"] for f in facts]


def test_nothing_known_means_nothing_said():
    brief = CreativeBrief.model_validate(EXAMPLE)
    assert remembered.build(brief=brief) == []
    assert remembered.build(brief=brief, grounding=_grounding(), photo_labels={}) == []


def test_their_own_photo_is_the_first_thing_said():
    brief = CreativeBrief.model_validate(EXAMPLE)
    facts = remembered.build(
        brief=brief,
        photo_labels={1: "kaju katli box"},
        usual_template=brief.template_id,
        palette={"primary": "#123B2E"},
        generated=False,
    )
    assert _kinds(facts) == ["own_photo", "usual_layout"]
    assert facts[0]["fact"] == "used their own photo of kaju katli box"


def test_a_carousel_says_which_slide_and_stops_at_two_photos():
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    facts = remembered.build(brief=brief, photo_labels={1: "oil bottle", 2: None, 3: "kadai"})
    photos = [f["fact"] for f in facts if f["kind"] == "own_photo"]
    assert photos == [
        "used their own photo of oil bottle on slide 1",
        "used their own photo of the product on slide 2",
    ]


def test_a_catalogue_item_is_claimed_only_if_it_is_actually_in_the_copy():
    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "headline": "Coconut oil, cold pressed", "subhead": "500ml, Rs 249"}
    )
    used = _grounding(catalog=[(_mem("Cold pressed coconut oil 500ml, Rs 249"), 0.8)])
    assert _kinds(remembered.build(brief=brief, grounding=used)) == ["catalogue"]

    # Retrieved, in front of the writer -- but the post is about something else.
    unused = _grounding(catalog=[(_mem("Sesame laddoo gift tin, Rs 480"), 0.7)])
    assert remembered.build(brief=brief, grounding=unused) == []


def test_common_shop_words_do_not_count_as_using_the_catalogue():
    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "headline": "Fresh special offer", "subhead": "Best price, order on WhatsApp"}
    )
    hits = _grounding(catalog=[(_mem("Fresh paneer, special price, order only"), 0.9)])
    assert remembered.build(brief=brief, grounding=hits) == []


def test_a_rejection_is_spoken_only_when_it_is_about_this_request():
    brief = CreativeBrief.model_validate(EXAMPLE)
    near = _grounding(rejection=[(_mem("no red backgrounds, it looks cheap"), 0.61)])
    facts = remembered.build(brief=brief, grounding=near)
    assert _kinds(facts) == ["avoided"] and "no red backgrounds" in facts[0]["fact"]

    # Retrieval's floor is 0.25 on purpose; most of that net is not about THIS post.
    far = _grounding(rejection=[(_mem("never show bare feet"), 0.27)])
    assert remembered.build(brief=brief, grounding=far) == []
    assert remembered.REJECTION_RELEVANT > 0.25


def test_the_usual_layout_is_claimed_only_when_it_really_is_their_usual():
    brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": "split_card"})
    assert _kinds(remembered.build(brief=brief, usual_template="split_card")) == ["usual_layout"]
    assert remembered.build(brief=brief, usual_template="lower_third") == []
    assert remembered.build(brief=brief, usual_template=None) == [], "no signature yet"


def test_the_reference_set_is_claimed_only_when_this_creative_used_its_layout():
    """A brand onboarded with a set our designers made has something true to say
    from its very first creative -- and saying it about a creative that did not
    use that layout is exactly the hollow claim this module exists to prevent."""
    brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": "frame_card"})
    facts = remembered.build(brief=brief, seeded_family="frame_card")
    assert _kinds(facts) == ["reference_style"]
    assert facts[0]["fact"] == "built in the style of the first set our team made for them"
    assert remembered.build(brief=brief, seeded_family="split_card") == []
    assert remembered.build(brief=brief, seeded_family=None) == [], "never onboarded with a set"


def test_their_own_approved_layout_outranks_the_set_we_seeded_them_with():
    """Once their own posts have a signature, that is the better thing to say --
    and both at once is one fact said twice."""
    brief = CreativeBrief.model_validate({**EXAMPLE, "template_id": "frame_card"})
    facts = remembered.build(brief=brief, usual_template="frame_card", seeded_family="frame_card")
    assert _kinds(facts) == ["usual_layout"]


def test_colours_are_claimed_only_for_a_generated_picture_and_only_real_hex():
    brief = CreativeBrief.model_validate(EXAMPLE)
    palette = {"primary": "#123b2e", "accent": "orange"}
    facts = remembered.build(brief=brief, palette=palette, generated=True)
    assert _kinds(facts) == ["brand_colours"] and "#123B2E" in facts[0]["fact"]
    assert "orange" not in facts[0]["fact"]
    assert remembered.build(brief=brief, palette=palette, generated=False) == []
    assert remembered.build(brief=brief, palette={"primary": "green"}, generated=True) == []


def test_the_list_is_short_and_long_memories_are_trimmed_without_an_ellipsis_lie():
    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "headline": "Coconut oil cold pressed", "template_id": "lower_third"}
    )
    long = "Cold pressed coconut oil " + "made in small batches every week " * 8
    facts = remembered.build(
        brief=brief,
        grounding=_grounding(
            catalog=[(_mem(long), 0.9)], rejection=[(_mem("no red backgrounds"), 0.7)]
        ),
        photo_labels={1: "oil bottle"},
        usual_template="lower_third",
        palette={"primary": "#123B2E"},
        generated=True,
    )
    assert len(facts) == remembered.MAX_FACTS
    assert _kinds(facts)[0] == "own_photo" and "brand_colours" not in _kinds(facts)
    assert all(len(f["fact"]) < 140 for f in facts)


def test_the_agent_is_told_to_say_only_what_is_on_the_list():
    from app.agent import prompts

    text = " ".join(
        v for v in vars(prompts).values() if isinstance(v, str) and "remembered" in v
    ).lower()
    assert "only what is in `remembered`" in text
    assert "never add a memory" in remembered.HINT.lower()
