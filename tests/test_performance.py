"""Instagram Insights without a database: the client's parsing, the scoring,
the re-read schedule, and what the prompt block says."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

import pytest

from app.insights import performance as perf
from app.integrations.instagram import client as ig
from app.integrations.instagram import fixtures, insights


def _row(**kw):
    base = dict(
        media_type="IMAGE",
        media_product_type="FEED",
        reach=1000,
        views=1100,
        likes=50,
        comments=5,
        saved=10,
        shares=5,
        follows=1,
        caption="Fresh batch",
        facts={},
        posted_at=datetime(2026, 9, 4, 13, 30, tzinfo=UTC),
        publication_id=None,
        synced_at=datetime(2026, 9, 6, tzinfo=UTC),
        ig_media_id="1",
        permalink=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_graph_timestamps_and_insight_replies_are_parsed():
    assert insights.parse_ts("2025-07-02T10:30:00+0000") == datetime(2025, 7, 2, 10, 30, tzinfo=UTC)
    assert insights.parse_ts("2025-07-02T10:30:00+00:00").tzinfo is not None
    assert insights.parse_ts("garbage") is None and insights.parse_ts(None) is None
    lifetime = {
        "data": [
            {"name": "reach", "period": "lifetime", "values": [{"value": 640}]},
            {"name": "saved", "period": "lifetime", "values": [{"value": 6}]},
            {"name": "views", "total_value": {"value": 700}},
            {"name": "broken", "values": [{"value": None}]},
        ]
    }
    assert insights._values(lifetime) == {"reach": 640, "saved": 6, "views": 700}
    series = {
        "data": [
            {
                "name": "reach",
                "period": "day",
                "values": [{"value": 100, "end_time": "x"}, {"value": 150, "end_time": "y"}],
            }
        ]
    }
    assert insights._values(series) == {"reach": 250}, "a time series is summed"


def test_media_kind_and_story_filtering():
    m = insights.IgMedia(id="1", media_type="VIDEO", media_product_type="REELS")
    assert m.is_reel and m.kind == "reel"
    assert insights.IgMedia(id="2", media_type="CAROUSEL_ALBUM").kind == "carousel"
    assert insights.IgMedia(id="3").kind == "single"


async def test_mock_media_is_recent_and_stories_and_old_posts_are_skipped(monkeypatch):
    monkeypatch.setattr(insights.settings, "instagram_mock", True)
    rows = await insights.list_media(access_token="t", ig_user_id="u")
    assert [r.id for r in rows][0] == fixtures.MOCK_MEDIA_ID, "the published post comes first"
    assert all(r.timestamp and r.timestamp > datetime.now(UTC) - timedelta(days=60) for r in rows)
    recent = await insights.list_media(
        access_token="t", ig_user_id="u", since=datetime.now(UTC) - timedelta(days=20)
    )
    assert len(recent) == 4 and all(
        r.timestamp >= datetime.now(UTC) - timedelta(days=20) for r in recent
    )
    two = await insights.list_media(access_token="t", ig_user_id="u", limit=2)
    assert len(two) == 2
    story = insights._media_from(
        {"id": "9", "media_type": "IMAGE", "media_product_type": "STORY", "timestamp": None}
    )
    monkeypatch.setattr(fixtures, "mock_media", lambda now=None: [story.raw])
    assert await insights.list_media(access_token="t", ig_user_id="u") == []


async def test_media_insights_fill_likes_and_comments_from_the_media_object(monkeypatch):
    monkeypatch.setattr(insights.settings, "instagram_mock", True)
    m = insights.IgMedia(id="unknown", like_count=7, comments_count=2)
    got = await insights.media_insights(access_token="t", media=m)
    assert got["reach"] == 100 and got["likes"] == 5, "the fixture's own likes win"
    monkeypatch.setattr(fixtures, "mock_media_insights", lambda mid: {"reach": 100})
    got = await insights.media_insights(access_token="t", media=m)
    assert got == {"reach": 100, "likes": 7, "comments": 2}


def test_insights_scope_is_requested_only_when_it_can_be_granted(monkeypatch):
    monkeypatch.setattr(ig.settings, "instagram_mock", False)
    monkeypatch.setattr(ig.settings, "ig_insights_enabled", False)
    assert insights.INSIGHTS_SCOPE not in ig.requested_scopes()
    assert not insights.can_read_insights(["instagram_business_basic"])
    monkeypatch.setattr(ig.settings, "ig_insights_enabled", True)
    assert ig.requested_scopes()[-1] == "instagram_business_manage_insights"
    assert insights.can_read_insights(ig.requested_scopes())
    assert "instagram_business_manage_insights" in ig.authorize_url("s")
    monkeypatch.setattr(ig.settings, "instagram_mock", True)
    monkeypatch.setattr(ig.settings, "ig_insights_enabled", False)
    assert insights.can_read_insights([]), "the mock never blocks on a permission"


def test_score_weighs_saves_and_shares_over_likes():
    plain = _row(likes=100, comments=0, saved=0, shares=0, follows=0)
    kept = _row(likes=20, comments=0, saved=20, shares=10, follows=0)
    assert perf.score(kept) > perf.score(plain)
    lonely = _row(reach=0, likes=10, comments=0, saved=0, shares=0, follows=0)
    assert perf.rate(lonely) == 10.0, "zero reach never divides by zero"
    assert perf.kind_of(_row(media_type="VIDEO", media_product_type="REELS")) == "reel"
    assert perf.kind_of(_row(media_type="CAROUSEL_ALBUM")) == "carousel"
    assert perf.title_of(_row(facts={"headline": "Diwali special"})) == "Diwali special"
    assert perf.title_of(_row(caption="first line\nsecond")) == "first line"
    assert perf.title_of(_row(caption="")) == "an untitled post"
    assert perf.title_of(_row(caption="x" * 80)).endswith("...")


def test_reread_schedule_slows_as_a_post_settles():
    now = datetime(2026, 9, 10, tzinfo=UTC)
    m = insights.IgMedia(id="1", timestamp=now - timedelta(days=2))
    assert perf._due(m, None, now), "never read: due"
    assert not perf._due(m, (now - timedelta(hours=1), m.timestamp), now)
    assert perf._due(m, (now - timedelta(hours=7), m.timestamp), now)
    old = insights.IgMedia(id="2", timestamp=now - timedelta(days=60))
    assert not perf._due(old, (now - timedelta(days=10), old.timestamp), now)
    assert perf._due(old, (now - timedelta(days=15), old.timestamp), now)
    mid = insights.IgMedia(id="3", timestamp=now - timedelta(days=20))
    assert not perf._due(mid, (now - timedelta(days=2), mid.timestamp), now)
    assert perf._due(mid, (now - timedelta(days=4), mid.timestamp), now)


def test_best_needs_something_to_compare_with():
    a, b = perf.Stat(), perf.Stat()
    a.add(_row(reach=1000, likes=10))
    a.add(_row(reach=1000, likes=10))
    assert perf.Performance.best({"a": a}) is None
    b.add(_row(reach=1000, likes=50, saved=20))
    b.add(_row(reach=1000, likes=50, saved=20))
    assert perf.Performance.best({"a": a, "b": b})[0] == "b"
    assert perf.Performance.best({"a": a, "b": perf.Stat()}) is None, "one sample is not a stat"


def test_prompt_block_is_silent_below_min_posts_and_specific_above():
    p = perf.Performance(posts=perf.MIN_POSTS - 1)
    assert p.as_prompt_block() == ""
    p = perf.Performance(posts=7)
    single, reel, car = perf.Stat(), perf.Stat(), perf.Stat()
    for _ in range(3):
        single.add(_row(reach=500, saved=4))
    reel.add(_row(reach=2000, media_type="VIDEO", media_product_type="REELS", saved=10))
    car.add(_row(reach=900, media_type="CAROUSEL_ALBUM", saved=40))
    car.add(_row(reach=900, media_type="CAROUSEL_ALBUM", saved=30))
    p.by_kind = {"single": single, "reel": reel, "carousel": car}
    lt, sc = perf.Stat(), perf.Stat()
    for _ in range(2):
        lt.add(_row(reach=1000, saved=30, shares=10))
        sc.add(_row(reach=1000, saved=5, shares=1))
    p.by_template = {"lower_third": lt, "split_card": sc}
    p.top = [
        {"title": "How we press it", "kind": "reel", "reach": 2000, "saved": 10, "shares": 40},
    ]
    p.by_slot = {(4, 18): lt, (1, 10): sc}
    block = p.as_prompt_block()
    assert block.startswith("## What their followers respond to")
    assert "Reels reach 4.0x" in block
    assert "Carousels are saved most (avg 35 saves vs 4 on singles)" in block
    assert "photo on top, words in the lower third" in block
    assert '"How we press it" (reel, reach 2,000, 10 saves, 40 shares)' in block
    assert "Fri 6pm IST" in block
    assert 'Say "so far"' in block


def test_sequel_guess_reads_the_title_when_there_are_no_facts():
    assert perf._guess_intent({"title": "Weekend special: 10% off", "kind": "single"}) == "promo"
    assert perf._guess_intent({"title": "3 ways to use it", "kind": "single"}) == "educational"
    assert perf._guess_intent({"title": "x", "kind": "carousel"}) == "educational"
    assert perf._guess_intent({"title": "From our customers", "kind": "single"}) == "testimonial"
    assert perf._guess_intent({"title": "Back in stock", "kind": "single"}) == "behind_the_scenes"
    assert perf._guess_intent({"title": "x", "facts": {"intent": "festive"}}) == "festive"


@pytest.mark.parametrize(
    ("weekday", "hour", "name"),
    [(4, 18, "Fri 6pm"), (0, 0, "Mon 12am"), (6, 12, "Sun 12pm"), (2, 9, "Wed 9am")],
)
def test_slot_names(weekday, hour, name):
    assert perf._slot_name(weekday, hour) == name
