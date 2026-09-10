"""Votes, taste, suggestions, the grid guard and the daily nudge against a real database."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.creative.brief import EXAMPLE


@pytest.fixture
def db_ready():
    from sqlalchemy import text as sql_text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("select 1 from creative_events limit 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database with the schema available: {str(exc)[:80]}")


@pytest.fixture
def owner(db_ready):
    from app.db.models import Account, Brand
    from app.db.session import session_scope

    with session_scope() as db:
        acct = Account(wa_phone=f"test-{uuid.uuid4().hex[:12]}", credits_balance=10)
        db.add(acct)
        db.flush()
        brand = Brand(
            account_id=acct.id,
            name="Insights Test",
            category="cold-pressed oils",
            palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
            fonts={"heading": "Poppins", "body": "Inter"},
        )
        db.add(brand)
        db.flush()
        return acct.id, brand.id


def _brief_row(db, account_id, brand_id, payload, *, status="approved"):
    from app.db.models import Brief, Creative

    b = Brief(account_id=account_id, brand_id=brand_id, payload=payload)
    db.add(b)
    db.flush()
    db.add(
        Creative(
            brief_id=b.id,
            brand_id=brand_id,
            template=payload.get("template_id") or "centered_overlay",
            slide_position=1,
            status=status,
            approved_at=datetime.now(UTC) if status in ("approved", "published") else None,
        )
    )
    db.flush()
    return b


def test_a_tap_becomes_a_vote_and_a_memory(owner, monkeypatch):
    from app.db.models import BrandMemory, CreativeEvent
    from app.db.session import session_scope
    from app.insights import votes
    from app.memory import embed

    monkeypatch.setattr(
        embed,
        "embed_texts",
        lambda texts, input_type="document": [[0.1] * embed.settings.embed_dim for _ in texts],
    )
    account_id, brand_id = owner
    with session_scope() as db:
        b = _brief_row(db, account_id, brand_id, EXAMPLE)
        votes.approve(db, str(b.id))
        votes.change_picture(db, str(b.id), account_id)
        votes.change_words(db, str(b.id), uuid.uuid4())  # a stranger's tap: ignored
    with session_scope() as db:
        kinds = sorted(
            e.kind for e in db.query(CreativeEvent).filter(CreativeEvent.brand_id == brand_id)
        )
        assert kinds == ["approve", "change_picture"]
        mem = {m.kind for m in db.query(BrandMemory).filter(BrandMemory.brand_id == brand_id)}
        assert mem == {"style_anchor", "rejection"}


def test_taste_is_learned_from_votes(owner):
    from app.db.session import session_scope
    from app.insights import events
    from app.insights.profile import taste

    account_id, brand_id = owner
    with session_scope() as db:
        for _ in range(4):
            events.record(
                db,
                kind="approve",
                account_id=account_id,
                brand_id=brand_id,
                meta={
                    "template": "split_card",
                    "aspect": "4:5",
                    "format": "single",
                    "mood": "warm",
                },
            )
        for _ in range(3):
            events.record(
                db,
                kind="change_picture",
                account_id=account_id,
                brand_id=brand_id,
                meta={
                    "template": "centered_overlay",
                    "aspect": "1:1",
                    "format": "single",
                    "mood": "neon",
                },
            )
        t = taste(db, brand_id)
    assert t.enough and t.best(t.template_scores) == "split_card"
    assert t.worst(t.template_scores) == "centered_overlay"
    block = t.as_prompt_block()
    assert "solid brand-colour panel" in block and "neon" in block


def test_grid_guard_stops_a_break_before_charging(owner):
    from app.agent.context import ToolContext
    from app.agent.tools import _create_creative
    from app.db.models import Account
    from app.db.session import session_scope
    from app.telemetry.stages import trace

    account_id, brand_id = owner
    usual = {
        **EXAMPLE,
        "template_id": "split_card",
        "format": {"type": "single", "aspect_ratio": "4:5"},
    }
    with session_scope() as db:
        for _ in range(7):
            _brief_row(db, account_id, brand_id, usual)
    odd = {
        **EXAMPLE,
        "template_id": "centered_overlay",
        "format": {"type": "single", "aspect_ratio": "1:1"},
    }
    with trace(account_id=account_id) as t:
        ctx = ToolContext(account_id, brand_id, None, "", t)
        res = _run(_create_creative(ctx, {"brief": odd}))
    assert res["reason"] == "grid_deviation" and res["charged"] == 0
    assert res["adjusted_brief"]["format"]["aspect_ratio"] == "4:5"
    assert res["adjusted_brief"]["template_id"] == "split_card"
    with session_scope() as db:
        assert db.get(Account, account_id).credits_balance == 10, "nothing was charged"


def test_suggestions_prefer_the_festival_then_the_unused_photo(owner):
    from app.db.models import Brand, BrandAsset
    from app.db.session import session_scope
    from app.insights import suggest

    account_id, brand_id = owner
    with session_scope() as db:
        db.add(
            BrandAsset(
                brand_id=brand_id,
                kind="product",
                label="coconut oil 500ml",
                storage_key="x",
                width=1600,
                height=1600,
            )
        )
        brand = db.get(Brand, brand_id)
        ideas = suggest.suggest(db, brand, today=date(2026, 11, 3))  # Dhanteras in 3 days
        assert ideas[0].festival == "Dhanteras" and ideas[0].intent == "festive"
        assert ideas[1].reference_asset_id is not None and "coconut oil" in ideas[1].why
        assert any(i.format == "carousel" for i in ideas)
        quiet = suggest.suggest(db, brand, today=date(2026, 7, 8))  # a Wednesday, nothing near
        assert quiet[0].reference_asset_id is not None, "with no festival the unused photo leads"


async def test_daily_nudge_sends_once_with_three_buttons(owner, monkeypatch):
    from app.channels.whatsapp import send
    from app.db import repo
    from app.db.models import Account, CreativeEvent, WaSession
    from app.db.session import session_scope
    from app.queue import handlers

    account_id, brand_id = owner
    wa = f"9199{uuid.uuid4().int % 10**8:08d}"
    with session_scope() as db:
        acct = db.get(Account, account_id)
        repo.touch_session(db, acct, wa, inbound=True)  # inside the 24h window
        # The photo-day checklist has its own test; this one is about ideas.
        from app.db.models import Brand

        b = db.get(Brand, brand_id)
        b.template_prefs = {**(b.template_prefs or {}), "shotlist_sent": True}
    sent = []

    async def fake_send_text(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(send, "send_text", fake_send_text)
    payload = {"account_id": str(account_id), "brand_id": str(brand_id), "wa_id": wa}
    await handlers.daily_suggestion(payload)
    await handlers.daily_suggestion(payload)  # same day: must not nag
    assert len(sent) == 1
    assert [b.title for b in sent[0]["buttons"]] == ["Make it", "Another idea", "Not today"]
    with session_scope() as db:
        sess = repo.latest_session(db, wa, account_id=account_id)
        assert sess.state.get("suggestions"), "the tap next turn must mean something"
        assert (
            db.query(CreativeEvent)
            .filter(CreativeEvent.brand_id == brand_id, CreativeEvent.kind == "suggested")
            .count()
            == 1
        )
        db.query(WaSession).filter(WaSession.id == sess.id).update({"closed_at": datetime.now(UTC)})


async def test_daily_nudge_respects_opt_out(owner, monkeypatch):
    from app.channels.whatsapp import send
    from app.db.models import Brand
    from app.db.session import session_scope
    from app.queue import handlers

    account_id, brand_id = owner
    with session_scope() as db:
        b = db.get(Brand, brand_id)
        b.template_prefs = {"daily_nudge": False}
    sent = []

    async def fake_send_text(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(send, "send_text", fake_send_text)
    await handlers.daily_suggestion(
        {"account_id": str(account_id), "brand_id": str(brand_id), "wa_id": "9199"}
    )
    assert sent == []


def _run(coro):
    import asyncio

    return asyncio.run(coro)


async def test_first_nudge_is_the_photo_checklist_when_photos_are_scarce(owner, monkeypatch):
    """Fewer than three photos on file: the daily job sends the photo-day
    checklist once (no buttons), then goes back to ideas."""
    from app.channels.whatsapp import send
    from app.db import repo
    from app.db.models import Account, Brand
    from app.db.session import session_scope
    from app.queue import handlers

    account_id, brand_id = owner
    wa = f"9199{uuid.uuid4().int % 10**8:08d}"
    with session_scope() as db:
        acct = db.get(Account, account_id)
        repo.touch_session(db, acct, wa, inbound=True)
    sent = []

    async def fake_send_text(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(send, "send_text", fake_send_text)
    payload = {"account_id": str(account_id), "brand_id": str(brand_id), "wa_id": wa}
    await handlers.daily_suggestion(payload)
    assert len(sent) == 1 and sent[0]["text"].startswith("Photo day!")
    assert "buttons" not in sent[0]
    with session_scope() as db:
        assert db.get(Brand, brand_id).template_prefs.get("shotlist_sent") is True
    await handlers.daily_suggestion(payload)  # the checklist is sent once; then an idea
    assert len(sent) == 2 and [b.id for b in sent[1]["buttons"]] == ["make:1", "next", "skip"]


def test_plan_build_arcs_mix_and_rebuild_keeps_made_slots(owner):
    from app.db.models import Brand, BrandAsset
    from app.db.session import session_scope
    from app.insights import plan as P

    account_id, brand_id = owner
    with session_scope() as db:
        for label, key in (("groundnut oil", "x"), ("coconut oil", "y")):
            db.add(BrandAsset(brand_id=brand_id, kind="product", label=label, storage_key=key))
        brand = db.get(Brand, brand_id)
        row = P.build(db, brand, date(2026, 11, 1), goal="footfall", cadence=4)
        slots = row.slots
        assert row.goal == "footfall" and row.cadence == 4
        dates = [s["date"] for s in slots]
        assert dates == sorted(dates) and len(set(dates)) == len(dates)
        # Diwali (8 Nov) arc: 3 Nov teaser, 6 Nov offer, 7 Nov last day.
        by_date = {s["date"]: s for s in slots}
        assert by_date["2026-11-03"]["campaign"] == "Diwali"
        offer = by_date["2026-11-06"]
        assert offer["intent"] == "festive" and "special" in offer["headline_idea"]
        assert by_date["2026-11-07"]["headline_idea"].startswith("Last day")
        # Offers dominate a footfall month; education is a carousel.
        pillars = [s["pillar"] for s in slots if not s.get("campaign")]
        assert pillars.count("offer") >= max(pillars.count(p) for p in set(pillars)) - 1
        plain = [s for s in slots if not s.get("campaign")]
        assert all(s["format"] == "carousel" for s in plain if s["pillar"] == "education")
        assert all(a["pillar"] != b["pillar"] for a, b in zip(plain, plain[1:], strict=False))
        assert "groundnut oil" in " ".join(s["headline_idea"] for s in slots)

        # A slot gets made; a replan with a new goal keeps it.
        first = slots[0]["date"]
        assert P.mark(db, brand_id, first, status="made", brief_id=uuid.uuid4())
        row = P.build(db, brand, date(2026, 11, 1), goal="leads", cadence=3)
        kept = [s for s in row.slots if s["date"] == first]
        assert kept and kept[0]["status"] == "made"
        assert P.describe(row)["posts_made"] == 1 and P.describe(row)["goal"] == "leads"
        assert "Diwali" in P.describe(row)["campaigns"]


def test_todays_plan_slot_is_the_first_suggestion(owner):
    from app.db.models import Brand
    from app.db.session import session_scope
    from app.insights import plan as P
    from app.insights import suggest

    account_id, brand_id = owner
    with session_scope() as db:
        brand = db.get(Brand, brand_id)
        row = P.build(db, brand, date(2026, 10, 1), goal="awareness", cadence=7)
        today = date.fromisoformat(row.slots[10]["date"])
        ideas = suggest.suggest(db, brand, today=today)
        assert ideas[0].plan_slot == today.isoformat()
        assert ideas[0].headline_idea == row.slots[10]["headline_idea"]
        assert ideas[0].as_dict()["plan_slot"] == today.isoformat()
        # Tomorrow's slot is not offered today (a missed one earlier this week is).
        assert all(i.plan_slot in (None, today.isoformat()) for i in ideas)


async def test_plan_month_tool_builds_shows_and_marks_the_slot(owner, monkeypatch):
    from app.agent import tools
    from app.agent.context import ToolContext
    from app.db.models import WaSession
    from app.db.session import session_scope
    from app.insights import plan as P
    from app.telemetry.stages import trace

    account_id, brand_id = owner
    with session_scope() as db:
        sess = WaSession(account_id=account_id, wa_id=f"91{uuid.uuid4().int % 10**10:010d}")
        db.add(sess)
        db.flush()
        session_id = sess.id
    t = trace(account_id=account_id).__enter__()
    ctx = ToolContext(account_id, brand_id, session_id, "", t)

    empty = await tools.dispatch(ctx, "plan_month", {})
    assert empty["ok"] and empty["plan"] is None and "ONE question" in empty["hint"]
    built = await tools.dispatch(
        ctx, "plan_month", {"goal": "launch", "launch": "Amla hair oil", "cadence": 5}
    )
    assert built["ok"] and built["built"] and built["plan"]["goal"] == "launch"
    assert built["plan"]["posts_planned"] >= 18
    with session_scope() as db:
        row = P.current(db, brand_id, P.date.today())
        assert row is not None and any("Amla hair oil" in s["headline_idea"] for s in row.slots)
    shown = await tools.dispatch(ctx, "plan_month", {})
    assert shown["ok"] and not shown["built"] and shown["plan"]["goal"] == "launch"

    # Building suggestion #1 that came from the plan closes its slot.
    with session_scope() as db:
        row = P.current(db, brand_id, P.date.today())
        slot_date = next(s["date"] for s in row.slots if s["status"] == "planned")
        sess = db.get(WaSession, session_id)
        sess.state = {"suggestions": [{"rank": 1, "plan_slot": slot_date, "headline_idea": "x"}]}

    async def fake_generate(ctx_, brief, **kw):
        return {"ok": True, "brief_id": str(uuid.uuid4()), "creative_ids": [], "image_urls": []}

    monkeypatch.setattr(tools.pipeline, "generate", fake_generate)
    from app.creative.brief import EXAMPLE

    res = await tools.dispatch(ctx, "create_creative", {"brief": EXAMPLE, "suggestion_rank": 1})
    assert res["ok"]
    with session_scope() as db:
        row = P.current(db, brand_id, P.date.today())
        made = [s for s in row.slots if s["date"] == slot_date]
        assert made and made[0]["status"] == "made" and made[0]["brief_id"] == res["brief_id"]


async def test_category_picks_the_brand_kit_once_and_look_can_be_changed(owner):
    from app.agent import tools
    from app.agent.context import ToolContext
    from app.db.models import Brand
    from app.db.session import session_scope
    from app.telemetry.stages import trace

    account_id, brand_id = owner
    t = trace(account_id=account_id).__enter__()
    ctx = ToolContext(account_id, brand_id, None, "", t)
    res = await tools.dispatch(ctx, "update_brand", {"category": "premium skincare"})
    assert "look=editorial" in res["updated"]
    with session_scope() as db:
        b = db.get(Brand, brand_id)
        assert b.fonts["heading"] == "Playfair Display"
        assert b.template_prefs["look"] == "editorial" and b.template_prefs["signature"] == "none"
    # A later category edit does not silently re-skin a brand that has posts.
    await tools.dispatch(ctx, "update_brand", {"category": "skincare and bakery"})
    with session_scope() as db:
        assert db.get(Brand, brand_id).template_prefs["look"] == "editorial"
    # The owner asks for a louder feel.
    res = await tools.dispatch(ctx, "update_brand", {"look": "bold"})
    assert "look" in res["updated"]
    with session_scope() as db:
        b = db.get(Brand, brand_id)
        assert b.fonts["heading"] == "Manrope" and b.template_prefs["signature"] == "bar"


# --------------------------------------------------------------------------- #
# Instagram Insights
# --------------------------------------------------------------------------- #
def _connect_instagram(db, brand_id):
    from app.db.models import IgAccount
    from app.integrations.instagram import client as ig

    row = IgAccount(
        brand_id=brand_id,
        ig_user_id="17841400000000000",
        username="mock_shop",
        access_token="IGAA-mock",
        scopes=ig.requested_scopes(),
        status="connected",
    )
    db.add(row)
    db.flush()
    return row


async def test_insights_sync_reads_every_post_and_links_ours_to_its_brief(owner):
    from app.db.models import Brand, IgAccountStat, PostMetric, Publication
    from app.db.session import session_scope
    from app.insights import performance
    from app.integrations.instagram import fixtures

    account_id, brand_id = owner
    with session_scope() as db:
        ig_row = _connect_instagram(db, brand_id)
        # A post made here, published to the id the mock publish returns.
        b = _brief_row(
            db, account_id, brand_id, {**EXAMPLE, "template_id": "lower_third"}, status="published"
        )
        creative = (
            db.query(__import__("app.db.models", fromlist=["Creative"]).Creative)
            .filter_by(brief_id=b.id)
            .one()
        )
        db.add(
            Publication(
                creative_id=creative.id,
                ig_account_id=ig_row.id,
                ig_media_id=fixtures.MOCK_MEDIA_ID,
                status="published",
                published_at=datetime.now(UTC),
            )
        )

    result = await performance.sync_brand(brand_id)
    assert result["ok"] and result["posts"] == 7 and result["refreshed"] == 7
    with session_scope() as db:
        rows = {r.ig_media_id: r for r in db.query(PostMetric).filter_by(brand_id=brand_id)}
        assert len(rows) == 7
        ours = rows[fixtures.MOCK_MEDIA_ID]
        assert ours.publication_id is not None
        assert ours.facts["template"] == "lower_third" and ours.facts["headline"]
        assert ours.reach == 640 and ours.saved == 6 and ours.posted_at is not None
        reel = rows["17900000000000101"]
        assert reel.media_product_type == "REELS" and reel.reach == 2900 and reel.facts == {}
        stat = db.query(IgAccountStat).filter_by(brand_id=brand_id).one()
        assert stat.followers == 1234 and stat.reach > 0
        assert db.get(Brand, brand_id).template_prefs.get("insights_synced_at")

    again = await performance.sync_brand(brand_id)
    assert again["ok"] and again["posts"] == 7 and again["refreshed"] == 0, (
        "nothing is re-read within the hour"
    )
    assert await performance.sync_if_due(brand_id) is None, "synced today: the daily job skips"
    with session_scope() as db:
        assert db.query(PostMetric).filter_by(brand_id=brand_id).count() == 7


async def test_insights_feed_the_prompt_the_defaults_and_a_sequel_idea(owner):
    from app.db.models import Brand
    from app.db.session import session_scope
    from app.insights import performance, suggest

    account_id, brand_id = owner
    with session_scope() as db:
        _connect_instagram(db, brand_id)
    await performance.sync_brand(brand_id)
    with session_scope() as db:
        perf = performance.build(db, brand_id)
        assert perf.posts == 7 and perf.enough
        assert perf.top[0]["kind"] == "carousel", "saves and shares per reach put the how-to first"
        block = perf.as_prompt_block()
        assert "Reels reach" in block and "Carousels are saved most" in block
        assert "3 ways to use groundnut oil" in block
        brand = db.get(Brand, brand_id)
        ideas = suggest.suggest(db, brand)
        sequel = next(i for i in ideas if i.sequel_of)
        assert sequel.sequel_of == "17900000000000102"
        assert sequel.format == "carousel" and sequel.intent == "educational"
        assert "1.8x your usual" in sequel.why and "58 saves" in sequel.why
        assert sequel.headline_idea.endswith(": part 2")
        # Ranked above the unused photo and the how-to, below only a festival/plan slot.
        kinds = [(i.festival is not None, i.sequel_of is not None) for i in ideas]
        first_sequel = kinds.index((False, True))
        assert all(f for f, _ in kinds[:first_sequel]), "only a festival outranks it"
        suggest.record_suggested(db, brand, ideas)
        ideas2 = suggest.suggest(db, brand)
        assert not any(i.sequel_of for i in ideas2), "offered once a month, not every day"
        summary = performance.summary(db, brand_id)
        assert summary["posts"] == 7 and summary["top"][0]["reach"] == 1250
        assert summary["by_format"]["reels"]["posts"] == 1
        assert "media_id" not in summary["top"][0]


def test_week_readout_compares_last_week_with_the_one_before(owner):
    from datetime import date

    from app.db.models import IgAccountStat, PostMetric
    from app.db.session import session_scope
    from app.insights import performance

    _, brand_id = owner
    monday = date(2026, 9, 14)
    with session_scope() as db:
        assert performance.week_readout(db, brand_id, monday) is None, "nothing yet: no readout"
        for mid, day, reach, saved, shares, caption in (
            ("a1", date(2026, 9, 8), 900, 12, 3, "Fresh batch"),
            ("a2", date(2026, 9, 10), 1500, 30, 20, "How we press it"),
            ("b1", date(2026, 9, 2), 1000, 8, 2, "Back in stock"),
        ):
            db.add(
                PostMetric(
                    brand_id=brand_id,
                    ig_media_id=mid,
                    posted_at=datetime(day.year, day.month, day.day, 8, tzinfo=UTC),
                    reach=reach,
                    saved=saved,
                    shares=shares,
                    likes=10,
                    caption=caption,
                )
            )
        db.add(IgAccountStat(brand_id=brand_id, day=date(2026, 9, 13), followers=1300))
        db.add(IgAccountStat(brand_id=brand_id, day=date(2026, 9, 6), followers=1280))
        db.flush()
        text = performance.week_readout(db, brand_id, monday)
        assert text == (
            "Last week on Instagram: 2 posts, reach 2,400 (up 140% on the week before), "
            '42 saves. Best: "How we press it" with 30 saves. Followers: 1,300 (+20).'
        )
        hi = performance.week_readout(db, brand_id, monday, "hi")
        assert hi.startswith("Pichhle hafte Instagram par: 2 posts") and "zyada" in hi
        assert performance.week_readout(db, brand_id, date(2026, 9, 28)) is None


async def test_post_performance_tool_and_the_hourly_sweep(owner, monkeypatch):
    from app.agent.context import ToolContext
    from app.agent.tools import _post_performance
    from app.db.models import Job
    from app.db.session import session_scope
    from app.insights import performance
    from app.queue import client
    from app.telemetry.stages import trace

    account_id, brand_id = owner
    ctx = ToolContext(account_id, brand_id, None, "", trace(account_id=account_id).__enter__())
    out = await _post_performance(ctx, {})
    assert out["ok"] is False and out["reason"] == "instagram_not_connected"
    with session_scope() as db:
        _connect_instagram(db, brand_id)
    out = await _post_performance(ctx, {})
    assert out["ok"] and out["posts"] == 0 and "No numbers yet" in out["hint"]

    pushed = []
    monkeypatch.setattr(client, "_push", lambda envelope, at=None: pushed.append(envelope) or True)
    assert performance.sweep() >= 1
    with session_scope() as db:
        jobs = db.query(Job).filter(Job.kind == "sync_insights").all()
        mine = [j for j in jobs if j.payload.get("brand_id") == str(brand_id)]
        assert len(mine) == 1
    before = len(pushed)
    performance.sweep()  # the same day is deduped: no second job for this brand
    assert not [j for j in pushed[before:] if str(brand_id) in j]
    with session_scope() as db:
        jobs = db.query(Job).filter(Job.kind == "sync_insights").all()
        assert len([j for j in jobs if j.payload.get("brand_id") == str(brand_id)]) == 1

    await performance.sync_brand(brand_id)
    out = await _post_performance(ctx, {})
    assert out["ok"] and out["posts"] == 7 and out["enough"]
    assert out["top"][0]["title"].startswith("3 ways")
    assert out["best_time"] is None or out["best_time"].endswith("IST")  # weekday-dependent
    before = len(pushed)
    performance.sweep()  # synced today: nothing new for this brand
    assert not [j for j in pushed[before:] if str(brand_id) in j]
