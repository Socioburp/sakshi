"""One photographer per brand: the same inside a brand, different across brands."""

from __future__ import annotations

import types

from app.creative import brandkit, photoreal, shotplan


def test_every_style_fits_the_budget_the_default_one_did():
    for key, art in shotplan.SHOOT_STYLES.items():
        assert len(art) <= len(shotplan.ART_DIRECTION), key
        assert "grain" in art and "photography" in art, key
    assert shotplan.SHOOT_STYLES["daylight"] == shotplan.ART_DIRECTION


def test_a_brands_style_is_stable_and_comes_from_its_look():
    for look, options in shotplan.STYLES_BY_LOOK.items():
        assert look in brandkit.LOOKS and all(o in shotplan.SHOOT_STYLES for o in options)
        picked = {shotplan.pick_style(look, f"brand-{i}") for i in range(40)}
        assert picked == set(options), "both candidates are actually used"
    assert shotplan.pick_style("warm", "Sri Sweets") == shotplan.pick_style("warm", "Sri Sweets")
    assert shotplan.pick_style("no-such-look", "x") == "daylight"


def test_the_look_sets_the_style_once_and_it_is_stored():
    brand = types.SimpleNamespace(name="Aura Skin", category="premium skincare", fonts={},
                                  template_prefs={})  # fmt: skip
    look = brandkit.apply(brand)
    assert look.key == "editorial"
    assert brand.template_prefs["shoot"] in shotplan.STYLES_BY_LOOK["editorial"]
    assert shotplan.style_of(brand.template_prefs, brand.name) == brand.template_prefs["shoot"]


def test_a_brand_from_before_this_existed_is_consistent_without_a_backfill():
    prefs = {"look": "bold", "signature": "bar"}  # no "shoot"
    first = shotplan.style_of(prefs, "Fix My Phone")
    assert first in shotplan.STYLES_BY_LOOK["bold"]
    assert all(shotplan.style_of(prefs, "Fix My Phone") == first for _ in range(5))
    assert shotplan.style_of({"shoot": "not-a-style", "look": "bold"}, "Fix My Phone") == first
    assert shotplan.style_of(None, "") == "daylight"


def test_every_slide_of_a_carousel_shares_the_brands_style_and_only_the_camera_moves():
    clauses = [shotplan.camera_clause(i, 6, "moody") for i in range(1, 7)]
    assert len(set(clauses)) == 6
    assert all(shotplan.SHOOT_STYLES["moody"] in c for c in clauses)
    assert not any(shotplan.ART_DIRECTION in c for c in clauses)


def test_two_brands_no_longer_get_the_same_photograph_described():
    a, _ = photoreal.photographic("a brass diya on a ledge", "text", style="moody")
    b, _ = photoreal.photographic("a brass diya on a ledge", "text", style="graphic_studio")
    c, _ = photoreal.photographic("a brass diya on a ledge", "text")
    assert len({a, b, c}) == 3
    assert "low-key light" in a and "hard-edged shadow" in b
    assert photoreal.CAMERA_DIRECTION in c, "no style given: exactly what it always was"
    for prompt in (a, b, c):
        assert prompt.count(photoreal.CAMERA_MARKER) == 1
        again, _ = photoreal.photographic(prompt, "text", style="moody")
        assert again == prompt, "still idempotent"
