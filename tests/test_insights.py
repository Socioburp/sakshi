"""Taste, suggestions and the grid guard. Unit tests; the DB parts live in test_insights_db."""

from __future__ import annotations

from collections import Counter
from datetime import date

from app.creative import grid
from app.creative.brief import _TEXT_REQUEST, EXAMPLE, CreativeBrief
from app.creative.photoreal import PLAYBOOK, category_direction, photographic
from app.insights import events, suggest
from app.insights.profile import MIN_VOTES, Taste


# --------------------------------------------------------------------------- #
# playbook
# --------------------------------------------------------------------------- #
def test_playbook_never_asks_for_lettering():
    for _, clause in PLAYBOOK:
        assert not _TEXT_REQUEST.search(clause), clause


def test_playbook_matches_categories_the_owner_would_type():
    assert "wood" in category_direction("Cold-pressed oils and pickles")
    assert "pastel" in category_direction("Ayurvedic skincare")
    assert "velvet" in category_direction("Gold jewellery showroom")
    assert category_direction("") == "" and category_direction("quantum consulting") == ""


def test_playbook_clause_rides_along_once():
    p1, _ = photographic("a jar of pickle", None, category="pickles")
    p2, _ = photographic(p1, None, category="pickles")
    assert p1 == p2 and p1.count("worn wood") == 1


# --------------------------------------------------------------------------- #
# taste
# --------------------------------------------------------------------------- #
def test_taste_says_nothing_below_the_vote_floor():
    t = Taste(votes=MIN_VOTES - 1, template_scores={"split_card": (3, 0)})
    assert t.as_prompt_block() == ""


def test_taste_names_what_they_approve_and_what_they_send_back():
    t = Taste(
        votes=12,
        template_scores={"split_card": (5, 1), "centered_overlay": (1, 4)},
        aspect_scores={"4:5": (6, 1)},
        format_scores={"single": (5, 2), "carousel": (1, 3)},
        mood_likes=Counter({"warm": 4, "homely": 3}),
        mood_dislikes=Counter({"neon": 3, "warm": 1}),
        words_rewritten_rate=0.6,
        photo_like_rate=0.8,
    )
    block = t.as_prompt_block()
    assert "solid brand-colour panel" in block and "keep sending back" in block
    assert "4:5" in block and "single" in block
    assert "warm, homely" in block and "neon" in block
    assert "rewrite the words" in block and "own product photos" in block


def test_facts_of_a_brief_are_the_facts_the_votes_need():
    f = events.facts_of(EXAMPLE)
    assert f["template"] == "centered_overlay" and f["aspect"] == "4:5"
    assert f["format"] == "single" and f["slides"] == 1 and f["has_photo"] is False


# --------------------------------------------------------------------------- #
# suggestions
# --------------------------------------------------------------------------- #
def test_festival_calendar_loads_and_looks_ahead():
    fests = suggest.load_festivals()
    assert len(fests) >= 40
    up = suggest.upcoming_festivals(date(2026, 11, 2))
    names = [f["name"] for _, f in up]
    assert "Dhanteras" in names and "Diwali" in names
    assert up[0][1]["name"] == "Dhanteras", "nearest first"


def test_weekday_hooks():
    assert suggest._weekday_hook(date(2026, 9, 11))[1] == "Weekend special"  # Friday
    assert suggest._weekday_hook(date(2026, 10, 2))[1] == "Weekend special"  # Friday beats payday
    assert suggest._weekday_hook(date(2026, 10, 1))[1] == "New month, new stock"  # Thu, day 1
    assert suggest._weekday_hook(date(2026, 9, 14))[0] == "behind_the_scenes"  # Monday
    assert suggest._weekday_hook(date(2026, 9, 9)) is None  # Wednesday mid-month


# --------------------------------------------------------------------------- #
# grid
# --------------------------------------------------------------------------- #
def _fp(posts=9, aspect="4:5", template="split_card", moods=("warm", "homely")):
    fp = grid.Fingerprint(posts=posts)
    fp.aspects[aspect] = posts
    fp.templates[template] = posts
    fp.formats["single"] = posts
    for m in moods:
        fp.moods[m] = posts
    fp.headline_lengths = [14] * posts
    fp.photo_share = 0.7
    return fp


def test_grid_is_silent_until_there_is_a_signature():
    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "format": {"type": "single", "aspect_ratio": "1:1"}}
    )
    assert grid.check(_fp(posts=grid.MIN_POSTS - 1), brief) is None


def test_grid_flags_a_ratio_and_layout_break_and_offers_the_fix():
    brief = CreativeBrief.model_validate(
        {
            **EXAMPLE,
            "format": {"type": "single", "aspect_ratio": "1:1"},
            "template_id": "centered_overlay",
        }
    )
    note = grid.check(_fp(), brief)
    assert note is not None and note.severity == 2
    assert note.suggested_changes == {"format.aspect_ratio": "4:5", "template_id": "split_card"}
    fixed = grid.adjusted_payload(brief.model_dump(mode="json"), note.suggested_changes)
    fixed_brief = CreativeBrief.model_validate(fixed)
    assert fixed_brief.format.aspect_ratio == "4:5" and fixed_brief.template_id == "split_card"
    assert grid.check(_fp(), fixed_brief) is None, "the adjusted brief fits the grid"


def test_mood_drift_is_advice_not_a_stop():
    brief = CreativeBrief.model_validate(
        {
            **EXAMPLE,
            "template_id": "split_card",
            "visual_direction": {**EXAMPLE["visual_direction"], "mood": "neon, loud"},
        }
    )
    note = grid.check(_fp(), brief)
    assert note is not None and note.severity == 0
    assert any("mood" in d for d in note.deviations)


def test_grid_describes_the_signature_in_owner_words():
    assert "4:5 posts" in _fp().describe() and "solid brand-colour panel" in _fp().describe()
