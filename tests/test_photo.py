"""The two halves of "make it look real": pick a real photo, or ask for one."""

from dataclasses import dataclass

from app.creative import photoref
from app.creative.brief import EXAMPLE, CreativeBrief
from app.creative.photoreal import PHOTOREAL_NEGATIVE, photographic


@dataclass
class Asset:
    id: str
    kind: str = "product"
    label: str | None = None
    width: int | None = 1600
    height: int | None = 1600


def test_camera_language_is_added_once():
    p1, n1 = photographic("coconut oil bottle on a jute mat", "text, logo", mood="homely")
    assert "50mm prime" in p1 and "homely" in p1
    p2, n2 = photographic(p1, n1)
    assert p2 == p1, "enrichment must survive regenerate_image without stacking"
    assert n2 == n1


def test_rendered_look_is_named_in_the_negative():
    _, negative = photographic("a hot kadai with oil shimmering", None)
    for word in ("3d render", "cgi", "airbrushed", "plastic surfaces"):
        assert word in negative
    # The caller's own negative terms are kept, never replaced.
    _, kept = photographic("x" * 20, "distorted hands")
    assert kept.startswith("distorted hands")
    assert len(kept) > len(PHOTOREAL_NEGATIVE)


def test_enrichment_never_asks_the_model_for_letterforms():
    """The whole cost model rests on the image carrying no type."""
    prompt, _ = photographic("jars of pickle on a shelf", None)
    CreativeBrief.model_validate(
        {**EXAMPLE, "visual_direction": {**EXAMPLE["visual_direction"], "prompt": prompt}}
    )


def test_copy_space_follows_the_layout_not_the_slide_position():
    """The 'hero' rung asked for the subject filling the middle and the lower
    third kept quiet -- on centered_overlay, whose type runs through the
    middle. The clause now comes from the layout and the measured type block;
    the rung says lens, distance and angle and nothing about words."""
    from app.creative import shotplan

    for shot in shotplan.LADDER:
        for word in ("quiet", "empty", "clean", "copy", "cropped in hard"):
            assert word not in shot.framing and word not in shot.camera, (shot.key, word)
    hero, _ = photographic("a jar of pickle", None, position=2, slide_count=4,
                           template="centered_overlay", text_box=(0.1, 0.32, 0.9, 0.7))  # fmt: skip
    assert "Keep the band from 32% to 70% of the height" in hero
    assert "never across the middle" in hero and "lower third" not in hero
    lower, _ = photographic("a jar of pickle", None, template="lower_third")
    assert "from 55% to 93%" in lower and "upper part of the frame" in lower
    poster, _ = photographic("a jar of pickle", None, template="poster_stack",
                             text_box=(0.08, 0.07, 0.6, 0.42))  # fmt: skip
    assert "on the left" in poster and "lower part of the frame" in poster
    panel, _ = photographic("a jar of pickle", None, template="split_card")
    assert "No words will be set on this picture" in panel
    # A single post gets one too (it had none), before the camera clause so
    # a FLUX trim takes the lens, not the copy space.
    single, _ = photographic("a jar of pickle", None, template="centered_overlay")
    assert "Keep the band" in single and single.index("Keep the band") < single.index("shot on")
    again, _ = photographic(single, None, template="lower_third")
    assert again == single, "idempotent: a regenerate does not stack a second clause"


def test_the_detail_rung_is_a_texture_study_not_a_cropped_subject():
    """'cropped in hard' contradicted the gate's subject_cropped rejection and
    bought retries on every carousel."""
    from app.creative import shotplan

    detail = shotplan.SHOT_BY_KEY["detail"]
    assert "cropped" not in detail.framing and "no whole object" in detail.framing


def test_a_real_photo_wins_when_the_words_actually_match():
    assets = [
        Asset("a", label="coconut oil bottle, 500ml"),
        Asset("b", label="shop front at diwali", kind="shop"),
    ]
    assert photoref.choose("Weekend Sale on coconut oil", assets) == "a"


def test_no_match_falls_through_to_the_model():
    """A photograph of the wrong product is worse than a generated one."""
    assets = [Asset("a", label="team photo at the counter", kind="team")]
    assert photoref.choose("Fresh mango season starts now", assets) is None


def test_logos_and_thumbnails_are_never_backgrounds():
    assert photoref.choose("coconut oil", [Asset("a", kind="logo", label="coconut oil")]) is None
    tiny = Asset("a", label="coconut oil", width=320, height=320)
    assert photoref.choose("coconut oil", [tiny]) is None


def test_one_scene_word_is_not_enough():
    """Regression: a shopfront photo was winning a post about mangoes.

    The only shared word was "light" -- from the lighting notes in
    visual_direction.prompt, not from anything the owner is selling.
    """
    shopfront = Asset("shop", kind="shop", label="diwali shop front lights")
    assert (
        photoref.choose(
            "Fresh alphonso mangoes are here",
            [shopfront],
            direction="soft morning window light, shallow depth of field",
        )
        is None
    )


def test_the_headline_outweighs_the_scene_description():
    photo = Asset("a", label="coconut oil bottle")
    assert photoref.choose("Coconut oil, fresh press", [photo]) == "a"


def test_stemming_does_not_mangle_words():
    """rstrip('s') turned "glass" into "gla" and changed what matched."""
    assert photoref._singular("glass") == "glass"
    assert photoref._singular("lights") == "light"
    assert photoref._singular("bus") == "bus"


def test_ties_go_to_the_larger_photograph():
    small = Asset("small", label="coconut oil", width=1000, height=1000)
    big = Asset("big", label="coconut oil", width=2400, height=2400)
    assert photoref.choose("coconut oil please", [small, big]) == "big"
