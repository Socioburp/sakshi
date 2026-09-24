"""The agency's compliance desk, copy desk and photo desk, as code."""

from __future__ import annotations

import json
import types

from app.agent.prompts import build_system
from app.creative import claims, copybook, shotlist
from app.creative.brief import EXAMPLE, CreativeBrief


def _brief(**over):
    payload = json.loads(json.dumps(EXAMPLE))
    payload.update(over)
    return CreativeBrief.model_validate(payload)


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #
def test_skincare_cure_and_fairness_claims_are_blocked_with_rewrites():
    b = _brief(headline="Cures acne in 7 days", subhead="Fairness cream, 100% safe")
    hits = claims.check(b, "skincare brand")
    rules = {v.rule: v for v in hits}
    assert {"cure", "skin_tone", "safety_absolute"} <= set(rules)
    assert all(v.severity == "block" for v in rules.values())
    assert "helps" in rules["cure"].rewrite
    assert claims.blocking(hits) and hits[0].severity == "block"


def test_general_rules_apply_to_every_category_and_only_advise():
    b = _brief(headline="No.1 bakery in the city", cta="Order now")
    hits = claims.check(b, "bakery")
    assert [v.rule for v in hits] == ["superlative"]
    assert hits[0].severity == "advise" and not claims.blocking(hits)


def test_food_organic_needs_certification_unless_substantiated():
    b = _brief(headline="100% organic cold-pressed oil")
    assert claims.blocking(claims.check(b, "cold-pressed oils"))
    # The owner has the Jaivik Bharat certificate on file.
    assert not claims.blocking(claims.check(b, "cold-pressed oils", ["100% organic"]))


def test_finance_and_education_guarantees_are_blocked():
    fin = _brief(headline="Guaranteed returns of 18%", subhead="Risk-free")
    assert {v.rule for v in claims.blocking(claims.check(fin, "investment advisory"))} == {
        "returns_guarantee"
    }
    edu = _brief(headline="100% placement guaranteed")
    assert claims.blocking(claims.check(edu, "coaching institute"))


def test_clean_copy_passes_and_hinglish_cure_is_caught():
    assert claims.check(_brief(headline="Fresh mangoes, order today"), "fruit shop") == []
    b = _brief(headline="Baalon ka ilaj — 7 din mein")
    assert claims.blocking(claims.check(b, "hair oil"))


def test_word_boundaries_do_not_over_match():
    # "treat" inside "retreat" and "heal" inside "health" are not claims.
    b = _brief(headline="Weekend retreat for your health", subhead="Fair prices, honest work")
    assert claims.check(b, "wellness spa") == []


def test_claim_prompt_block_names_the_industry_rules():
    block = claims.prompt_block("skincare")
    assert "server enforces" in block and "cure" in block and "substantiated" in block
    # A bakery is food: FSSAI rules apply; a hardware shop gets the general rule only.
    assert "organic" in claims.prompt_block("bakery")
    assert "Every claim must be something the owner can prove" in claims.prompt_block("hardware")


# --------------------------------------------------------------------------- #
# copybook
# --------------------------------------------------------------------------- #
def test_copybook_habits_follow_the_category():
    assert "ingredient" in copybook.habits("premium skincare")
    assert "dish" in copybook.habits("family restaurant")
    assert "occasion" in copybook.habits("saree boutique")
    assert "locality" in copybook.habits("hardware store")


def test_copybook_block_is_compact_and_carries_the_offer_rules():
    block = copybook.prompt_block("bakery", locality="Indiranagar")
    assert len(block) < 1500
    assert "Indiranagar" in block and "bundle" in block and "anchor" in block
    assert "promo:" in block and "festive:" in block
    assert "5-10" in block  # hashtags


def test_system_prompt_carries_copy_and_claim_blocks():
    brand = types.SimpleNamespace(
        name="Qyra",
        category="skincare",
        template_prefs={"locality": "Koramangala"},
        palette={},
        fonts={},
        logo_url="x",
        never_say=[],
        always_say=[],
        languages=[],
    )
    system = build_system(brand, "", language_block="")
    assert "## Copy that converts here" in system and "Koramangala" in system
    assert "## Claims (this industry has rules" in system and "skin tone" in system


# --------------------------------------------------------------------------- #
# shot lists
# --------------------------------------------------------------------------- #
def _asset(kind, label=None):
    return types.SimpleNamespace(kind=kind, label=label)


def test_shot_lists_follow_the_category():
    cafe = shotlist.shots_for("cafe")
    assert cafe[0].key == "hero" and "plate" in cafe[0].how
    assert shotlist.shots_for("hair salon")[0].key == "shop"
    assert shotlist.shots_for("saree boutique")[1].key == "worn"
    assert shotlist.shots_for("hardware")[0].key == "hero"


def test_coverage_names_the_one_photo_to_ask_for_next():
    assets = [_asset("logo", "Logo"), _asset("product", "coconut oil 500ml")]
    cov = shotlist.coverage(assets, "cold-pressed oils")
    assert cov["have"] == ["hero"]
    assert cov["next"]["key"] == "in_use" and "hands" in cov["next"]["how"]
    assert 0 < cov["score"] < 0.5
    more = assets + [
        _asset("product", "close-up of the label"),
        _asset("shop", "front"),
        _asset("team", "us"),
        _asset("other", "hands pouring oil on a salad"),
        _asset("product", "the whole range on a table"),
    ]
    full = shotlist.coverage(more, "cold-pressed oils")
    assert full["score"] == 1.0 and full["next"] is None


def test_checklist_is_one_message_in_the_owner_language():
    en = shotlist.checklist("bakery", "en")
    hi = shotlist.checklist("bakery", "hi")
    assert en.startswith("Photo day!") and "1. The dish" in en and "flash off" in en
    assert "Khidki" in hi and "5." in hi
