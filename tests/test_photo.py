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


def test_ties_go_to_the_larger_photograph():
    small = Asset("small", label="coconut oil", width=1000, height=1000)
    big = Asset("big", label="coconut oil", width=2400, height=2400)
    assert photoref.choose("coconut oil please", [small, big]) == "big"
