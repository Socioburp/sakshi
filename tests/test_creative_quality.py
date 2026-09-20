"""The creative-quality fixes, each pinned against the failure it was for.

Every test here names a thing an owner complained about. If one of these goes
red, the complaint is back.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.config import settings
from app.creative import dedupe, legibility, shotplan
from app.creative.imagegen import providers as P


def _png(w=400, h=500, fill=(120, 90, 60), *, busy=False, stripes=False) -> bytes:
    im = Image.new("RGB", (w, h), fill)
    d = ImageDraw.Draw(im)
    if stripes:
        for x in range(0, w, 7):
            d.line([(x, 0), (x, h)], fill=(20, 20, 20), width=3)
    if busy:
        for i in range(14):
            x, y = (i * 37) % w, (i * 53) % h
            d.ellipse([x, y, x + 60, y + 60], fill=((i * 31) % 255, (i * 17) % 255, 90))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# "the images are low quality"
# --------------------------------------------------------------------------- #
def test_quality_defaults_are_not_the_cheap_tier():
    """Every default used to be the fast/distilled variant.

    schnell at 4 steps, klein 4B and gpt-image-1 at "medium" are built to win
    on latency. They are the right default for a draft and the wrong one for
    the only picture an owner will ever put on their grid.
    """
    assert "schnell" not in settings.imagegen_fal_model
    assert "schnell" not in settings.imagegen_replicate_model
    assert "klein" not in settings.imagegen_bfl_model
    assert settings.imagegen_openai_quality == "high"


def test_steps_follow_the_model_family():
    # schnell is timestep-distilled and gains nothing past 4.
    assert P.steps_for("fal-ai/flux/schnell") == 4
    # dev keeps improving to ~28. This used to be sent as nothing at all, so
    # a dev model ran at whatever the vendor's default happened to be.
    assert P.steps_for("fal-ai/flux/dev") == 28
    assert P.steps_for("flux-2-pro") == 28
    assert P.steps_for("something-unknown") == P.STEPS_FALLBACK


def test_steps_can_be_overridden(monkeypatch):
    monkeypatch.setattr(P.settings, "imagegen_steps", 12)
    assert P.steps_for("fal-ai/flux/schnell") == 12
    # and stays inside a sane range whatever the env says
    monkeypatch.setattr(P.settings, "imagegen_steps", 9999)
    assert P.steps_for("fal-ai/flux/dev") == 50


def test_source_is_lossless_by_default():
    """A JPEG from the vendor was encoded, composited over, screenshotted and
    encoded again -- two generation losses before the owner saw it."""
    assert P.source_format() == ("png", "image/png")


def test_megapixels_cover_the_delivery_size():
    # 1080x1350 is 1.46MP and 1080x1080 is 1.17MP: "1" generated below the
    # delivery size for both and let the browser upscale.
    assert P.megapixels_for(1080, 1350) == "2"
    assert P.megapixels_for(1080, 1080) == "2"
    assert P.megapixels_for(800, 800) == "1"


# --------------------------------------------------------------------------- #
# "every image which is created should be different"
# --------------------------------------------------------------------------- #
def test_every_slide_gets_a_different_shot():
    for n in range(2, 7):
        keys = [shotplan.shot_for(i, n).key for i in range(1, n + 1)]
        assert len(set(keys)) == n, f"{n}-slide carousel repeats a shot: {keys}"


def test_every_slide_gets_a_different_camera_clause():
    clauses = [shotplan.camera_clause(i, 6) for i in range(1, 7)]
    assert len(set(clauses)) == 6
    # ...but the art direction is identical, so it still reads as one shoot.
    assert all(shotplan.ART_DIRECTION in c for c in clauses)


def test_the_last_slide_is_the_one_that_carries_the_cta():
    """compose.py only shows the CTA on the final slide, so that slide needs
    the calmest, most graphic frame -- the one with room for type."""
    for n in range(2, 7):
        assert shotplan.order_for(n)[-1] in ("graphic", "detail")


def test_seeds_are_stable_and_separated():
    a = shotplan.seed_for("brief-1", 1)
    assert a == shotplan.seed_for("brief-1", 1)  # a recompose is reproducible
    assert a != shotplan.seed_for("brief-1", 2)  # slides diverge
    assert a != shotplan.seed_for("brief-2", 1)  # a new request is fresh
    assert a != shotplan.seed_for("brief-1", 1, salt=1)  # a re-roll is different


# --------------------------------------------------------------------------- #
# "the images which are created are overlapping"
# --------------------------------------------------------------------------- #
def test_duplicate_slides_are_caught():
    same_a = _png(busy=True)
    same_b = _png(busy=True)
    different = _png(fill=(20, 40, 90), stripes=True)
    assert dedupe.distance(dedupe.dhash(same_a), dedupe.dhash(same_b)) <= (
        dedupe.DUPLICATE_DISTANCE
    )
    assert dedupe.distance(dedupe.dhash(same_a), dedupe.dhash(different)) > (
        dedupe.DUPLICATE_DISTANCE
    )


def test_the_later_slide_is_the_one_asked_to_try_again():
    """Slide 1 is the feed thumbnail and carries the post; it keeps its
    picture and the collider is the one redone."""
    h = dedupe.dhash(_png(busy=True))
    dupes = dedupe.find_duplicates({1: h, 2: dedupe.dhash(_png(stripes=True)), 3: h})
    assert [(d.position, d.matches) for d in dupes] == [(3, 1)]


def test_variety_report_surfaces_a_flat_carousel():
    h = dedupe.dhash(_png(busy=True))
    r = dedupe.report({1: h, 2: h, 3: h})
    assert r["slides"] == 3 and r["min_distance"] == 0
    assert len(r["duplicates"]) == 2


# --------------------------------------------------------------------------- #
# "the text is overlapping the image"
# --------------------------------------------------------------------------- #
def test_scrim_gets_stronger_on_a_bright_background():
    dark, _ = legibility.scrim_boost(_png(fill=(40, 40, 40)), "centered_overlay")
    mid, _ = legibility.scrim_boost(_png(fill=(128, 128, 128)), "centered_overlay")
    bright, _ = legibility.scrim_boost(_png(fill=(235, 235, 235)), "centered_overlay")
    assert dark < mid < bright
    assert mid == 1.0  # the scrim as designed, on the background it was designed for


def test_a_busy_background_only_ever_adds_scrim():
    calm, _ = legibility.scrim_boost(_png(fill=(200, 200, 200)), "centered_overlay")
    busy, _ = legibility.scrim_boost(_png(fill=(200, 200, 200), stripes=True), "centered_overlay")
    assert busy >= calm


def test_templates_with_no_type_over_the_photo_are_left_alone():
    for template in ("frame_card", "top_band"):
        boost, why = legibility.scrim_boost(_png(fill=(250, 250, 250)), template)
        assert boost == 1.0 and "reason" in why


def test_a_broken_background_never_fails_the_slide():
    boost, why = legibility.scrim_boost(b"not an image", "centered_overlay")
    assert boost == 1.0 and why["reason"] == "measurement failed"


def test_every_template_has_a_measured_band():
    """A template whose copy moves and whose band does not is measured in the
    wrong place, which is worse than not measuring at all."""
    from app.creative.compose import TEMPLATES

    assert set(TEMPLATES) <= set(legibility.TYPE_BANDS)
