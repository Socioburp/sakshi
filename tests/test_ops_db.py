"""Worker, reaper, ingest and publish behaviour that needs a real database.

Skips cleanly without one. These are the paths a crash, a redeploy or a
forwarded link used to turn into a lost message, a kept credit or a post on
the wrong account.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief


@pytest.fixture
def db_ready():
    from sqlalchemy import text as sql_text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("select billed from creatives limit 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database with the schema available: {str(exc)[:80]}")


@pytest.fixture
def blobs(monkeypatch):
    from app.integrations.storage import r2

    store: dict[str, bytes] = {}
    monkeypatch.setattr(
        r2, "put", lambda key, data, ct=None: store.__setitem__(key, data) or f"file://{key}"
    )
    monkeypatch.setattr(r2, "get", lambda key: store[key])
    monkeypatch.setattr(r2, "public_url", lambda key: f"file://{key}")
    promoted: list[str] = []
    monkeypatch.setattr(
        r2,
        "promote",
        lambda key: promoted.append(key) or f"file://{key.replace('drafts/', 'published/', 1)}",
    )
    store["_promoted"] = promoted  # type: ignore[assignment]
    return store


@pytest.fixture
async def chromium():
    from app.creative import compose

    try:
        await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Chromium for the compositor: {str(exc)[:80]}")
    yield
    await compose.shutdown()


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
            name="Ops Test",
            palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
            fonts={"heading": "Poppins", "body": "Inter"},
        )
        db.add(brand)
        db.flush()
        return acct.id, brand.id


def _balance(account_id) -> int:
    from app.db.models import Account
    from app.db.session import session_scope

    with session_scope() as db:
        return db.get(Account, account_id).credits_balance


def _ctx(account_id, brand_id):
    from app.agent.context import ToolContext
    from app.telemetry.stages import trace

    t = trace(account_id=account_id).__enter__()
    return ToolContext(account_id, brand_id, None, "", t)


# --------------------------------------------------------------------------- #
# reaper
# --------------------------------------------------------------------------- #
def test_stuck_creatives_are_failed_and_billed_ones_refunded_once(owner):
    from app.creative.pipeline import reap_stuck_creatives
    from app.db.models import Brief, Creative
    from app.db.session import session_scope

    account_id, brand_id = owner
    old = datetime.now(UTC) - timedelta(minutes=30)
    with session_scope() as db:
        brief = Brief(account_id=account_id, brand_id=brand_id, payload=EXAMPLE)
        db.add(brief)
        db.flush()
        brief_id = brief.id
        for pos, billed in ((1, True), (2, False)):
            db.add(
                Creative(
                    brief_id=brief_id,
                    brand_id=brand_id,
                    template="centered_overlay",
                    slide_position=pos,
                    status="generating",
                    billed=billed,
                    created_at=old,
                )
            )
        # A fresh one must be left alone: it is still legitimately generating.
        db.add(
            Creative(
                brief_id=brief_id,
                brand_id=brand_id,
                template="centered_overlay",
                slide_position=3,
                status="generating",
                billed=True,
            )
        )
    before = _balance(account_id)
    reap_stuck_creatives()
    with session_scope() as db:
        by_pos = {
            c.slide_position: c.status
            for c in db.query(Creative).filter(Creative.brief_id == brief_id)
        }
    assert by_pos == {1: "failed", 2: "failed", 3: "generating"}
    assert _balance(account_id) == before + 1, "only the billed stuck slide is refunded"
    reap_stuck_creatives()
    assert _balance(account_id) == before + 1, "running the reaper again refunds nothing"
    with session_scope() as db:
        # The still-generating row would become stale for the next run.
        db.query(Creative).filter(Creative.brief_id == brief_id).delete()


def test_job_reaper_requeues_lost_pushes_and_retires_dead_workers(db_ready, monkeypatch):
    from app.db.models import Job
    from app.db.session import session_scope
    from app.queue import client

    pushed: list[str] = []
    monkeypatch.setattr(client, "_push", lambda envelope, at=None: pushed.append(envelope) or True)
    old = datetime.now(UTC) - timedelta(minutes=20)
    tag = uuid.uuid4().hex
    # An unknown kind: if a real worker ever sees these rows it retires them
    # on sight instead of trying to run a message that does not exist.
    kind = "test_reaper_noop"
    with session_scope() as db:
        db.add(Job(kind=kind, payload={"t": tag}, status="queued", created_at=old))
        db.add(Job(kind=kind, payload={"t": tag}, status="running", attempts=1, started_at=old))
        db.add(
            Job(
                kind=kind,
                payload={"t": tag},
                status="running",
                attempts=client.MAX_ATTEMPTS,
                started_at=old,
            )
        )
        db.add(
            Job(
                kind=kind,
                payload={"t": tag},
                status="running",
                attempts=1,
                started_at=datetime.now(UTC),
            )
        )  # alive: must be left alone
    counts = client.reap()
    assert counts["requeued"] >= 1 and counts["retried"] >= 1 and counts["dead"] >= 1
    mine = [e for e in pushed if tag in e]
    assert len(mine) == 2, (
        "the lost push and the dead worker's job are re-pushed; the exhausted one is not"
    )
    with session_scope() as db:
        rows = db.query(Job).filter(Job.payload["t"].astext == tag).all()
        statuses = sorted(r.status for r in rows)
        assert statuses == ["dead", "queued", "queued", "running"]
        for r in rows:
            db.delete(r)


async def test_worker_runs_a_job_once_even_if_delivered_twice(db_ready, monkeypatch):
    from app.db.models import Job
    from app.db.session import session_scope
    from app.queue import worker

    runs: list[dict] = []

    async def handler(payload):
        runs.append(payload)

    monkeypatch.setitem(worker.HANDLERS, "test_kind", handler)
    with session_scope() as db:
        job = Job(kind="test_kind", payload={"x": 1}, status="queued")
        db.add(job)
        db.flush()
        job_id = str(job.id)
    envelope = {"job_id": job_id, "kind": "test_kind", "payload": {"x": 1}}
    deliveries = iter([envelope, envelope, None])
    monkeypatch.setattr(worker, "dequeue", lambda timeout=5: next(deliveries))
    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert runs == [{"x": 1}], "the second delivery must be skipped by the row check"
    with session_scope() as db:
        assert db.get(Job, uuid.UUID(job_id)).status == "done"


async def test_failing_job_is_retried_then_dead(db_ready, monkeypatch):
    from app.db.models import Job
    from app.db.session import session_scope
    from app.queue import client, worker

    async def boom(payload):
        raise RuntimeError("vendor 503")

    monkeypatch.setitem(worker.HANDLERS, "boom", boom)
    scheduled: list[int] = []
    monkeypatch.setattr(client, "_push", lambda envelope, at=None: scheduled.append(1) or True)
    # No waiting in a test: a claim honours scheduled_for, so with real backoff
    # the second delivery would (correctly) be refused as "not yet due".
    monkeypatch.setattr(client, "BACKOFF", (0, 0))
    with session_scope() as db:
        job = Job(kind="boom", payload={}, status="queued")
        db.add(job)
        db.flush()
        job_id = str(job.id)
    envelope = {"job_id": job_id, "kind": "boom", "payload": {}}
    for attempt in range(1, client.MAX_ATTEMPTS + 1):
        monkeypatch.setattr(worker, "dequeue", lambda timeout=5, e=envelope: e)
        await worker.run_once()
        with session_scope() as db:
            row = db.get(Job, uuid.UUID(job_id))
            assert row.attempts == attempt
            assert row.status == ("queued" if attempt < client.MAX_ATTEMPTS else "dead")
    assert len(scheduled) == client.MAX_ATTEMPTS - 1
    with session_scope() as db:
        db.delete(db.get(Job, uuid.UUID(job_id)))


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def test_message_and_job_commit_together_and_duplicates_make_no_second_job(db_ready, monkeypatch):
    from app.channels.base import InboundMessage
    from app.channels.whatsapp import ingest as ing
    from app.db.models import Job, Message
    from app.db.session import session_scope
    from app.queue import client

    monkeypatch.setattr(client, "_push", lambda envelope, at=None: True)
    pmid = f"wamid.{uuid.uuid4().hex}"
    msg = InboundMessage(
        provider="mock",
        provider_message_id=pmid,
        wa_id=f"9199{uuid.uuid4().int % 10**8:08d}",
        kind="text",
        text="hi",
        timestamp=datetime.now(UTC),
    )
    first = ing.ingest(msg)
    assert first is not None
    assert ing.ingest(msg) is None, "the provider's retry is a duplicate"
    with session_scope() as db:
        assert db.get(Message, first) is not None
        jobs = db.query(Job).filter(Job.dedupe_key == f"mock:{pmid}").all()
        assert len(jobs) == 1 and jobs[0].status == "queued"
        # Never leave a runnable job for a dev worker on this database.
        jobs[0].status = "done"


async def test_a_message_answered_by_an_earlier_turn_is_skipped(db_ready, monkeypatch):
    """The burst: B arrives while A's turn runs. Coverage is recorded, not inferred."""
    from app.agent import runner
    from app.channels.base import InboundMessage
    from app.channels.whatsapp import ingest as ing
    from app.db.models import Job, Message
    from app.db.session import session_scope
    from app.queue import client
    from app.telemetry.stages import trace

    monkeypatch.setattr(client, "_push", lambda envelope, at=None: True)
    wa = f"9199{uuid.uuid4().int % 10**8:08d}"

    def inbound(text):
        return ing.ingest(
            InboundMessage(
                provider="mock",
                provider_message_id=f"wamid.{uuid.uuid4().hex}",
                wa_id=wa,
                kind="text",
                text=text,
                timestamp=datetime.now(UTC),
            )
        )

    a_id, b_id = inbound("sale post banao"), inbound("20% off likhna")
    with session_scope() as db:
        rows = [db.get(Message, a_id), db.get(Message, b_id)]
        covered = runner._covered(rows, rows[0])
    assert set(covered) == {a_id, b_id}
    runner._mark_answered(covered)
    with session_scope() as db:
        assert db.get(Message, b_id).answered_at is not None
    with trace(account_id=uuid.uuid4()) as t:
        res = await runner.run_turn(message_id=b_id, trace=t)
    assert res == {"ok": True, "reason": "already_answered", "tools": [], "media": []}
    with session_scope() as db:
        # Only this test's jobs -- never sweep a shared database.
        mine = {str(a_id), str(b_id)}
        for j in db.query(Job).filter(Job.status == "queued"):
            if j.payload.get("message_id") in mine:
                j.status = "done"


def test_first_contact_survives_a_concurrent_insert(db_ready):
    """Simulates the loser of the race: the row appears between select and insert."""
    from app.db import repo
    from app.db.models import Account
    from app.db.session import session_scope

    phone = f"test-{uuid.uuid4().hex[:12]}"
    with session_scope() as db:
        winner = repo.get_or_create_account(db, phone, "Winner")
        winner_id = winner.id
    with session_scope() as db:
        real_scalar = db.scalar
        calls = {"n": 0}

        def racing_scalar(stmt, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # what the loser saw: nothing there yet
            return real_scalar(stmt, *a, **kw)

        db.scalar = racing_scalar  # type: ignore[method-assign]
        loser = repo.get_or_create_account(db, phone, "Loser")
        assert loser.id == winner_id, "the loser must end up on the winner's row"
        assert db.query(Account).filter(Account.wa_phone == phone).count() == 1


def test_approval_is_scoped_to_the_tapping_account(owner):
    from app.db import repo
    from app.db.models import Brief, Creative
    from app.db.session import session_scope

    account_id, brand_id = owner
    with session_scope() as db:
        brief = Brief(account_id=account_id, brand_id=brand_id, payload=EXAMPLE)
        db.add(brief)
        db.flush()
        db.add(
            Creative(
                brief_id=brief.id,
                brand_id=brand_id,
                template="centered_overlay",
                slide_position=1,
                status="ready",
            )
        )
        db.flush()
        stranger = uuid.uuid4()
        assert (
            repo.mark_approved(db, brief_id=str(brief.id), via="button", account_id=stranger) == 0
        )
        assert (
            repo.mark_approved(db, brief_id=str(brief.id), via="button", account_id=account_id) == 1
        )


# --------------------------------------------------------------------------- #
# publish
# --------------------------------------------------------------------------- #
async def _approved_carousel(owner, blobs, chromium):
    from app.creative import pipeline
    from app.db import repo
    from app.db.models import IgAccount
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    res = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    assert res["ok"]
    with session_scope() as db:
        repo.mark_approved(db, brief_id=res["brief_id"], via="button", account_id=account_id)
        db.add(
            IgAccount(
                brand_id=brand_id,
                ig_user_id="17841400000000000",
                status="connected",
                access_token="tok",
                token_expires_at=datetime.now(UTC) + timedelta(days=2),
            )
        )
    return ctx, res["brief_id"]


async def test_publish_refuses_a_carousel_with_a_failed_slide(owner, blobs, chromium):
    from app.agent.tools import _publish_to_instagram
    from app.db.models import Creative
    from app.db.session import session_scope

    ctx, brief_id = await _approved_carousel(owner, blobs, chromium)
    with session_scope() as db:
        row = (
            db.query(Creative)
            .filter(Creative.brief_id == uuid.UUID(brief_id), Creative.slide_position == 2)
            .one()
        )
        row.status = "failed"
    res = await _publish_to_instagram(ctx, {"brief_id": brief_id})
    assert res["reason"] == "carousel_has_failed_slides" and res["failed_slides"] == [2]


async def test_publish_refreshes_an_expiring_token_and_promotes_out_of_drafts(
    owner, blobs, chromium
):
    from app.agent.tools import _publish_to_instagram
    from app.db.models import Creative, IgAccount
    from app.db.session import session_scope

    ctx, brief_id = await _approved_carousel(owner, blobs, chromium)
    res = await _publish_to_instagram(ctx, {"brief_id": brief_id})
    assert res["ok"], res
    with session_scope() as db:
        ig = db.query(IgAccount).filter(IgAccount.brand_id == ctx.brand_id).one()
        assert ig.token_expires_at > datetime.now(UTC) + timedelta(days=30), "token was refreshed"
        rows = db.query(Creative).filter(Creative.brief_id == uuid.UUID(brief_id)).all()
        assert all(
            r.status == "published" and r.composed_key.startswith("published/") for r in rows
        )
    assert len(blobs["_promoted"]) == 3


async def test_dead_token_disconnects_and_asks_to_reconnect(owner, blobs, chromium, monkeypatch):
    import httpx

    from app.agent import tools
    from app.db.models import IgAccount
    from app.db.session import session_scope

    ctx, brief_id = await _approved_carousel(owner, blobs, chromium)
    req = httpx.Request("POST", "https://graph.instagram.com/x")
    dead = httpx.HTTPStatusError(
        "400",
        request=req,
        response=httpx.Response(400, json={"error": {"code": 190}}, request=req),
    )

    async def rejected(**kw):
        raise dead

    monkeypatch.setattr(tools.ig, "create_media_container", rejected)
    res = await tools._publish_to_instagram(ctx, {"brief_id": brief_id})
    assert res["reason"] == "instagram_reconnect_required"
    with session_scope() as db:
        assert (
            db.query(IgAccount).filter(IgAccount.brand_id == ctx.brand_id).one().status
            == "disconnected"
        )


# --------------------------------------------------------------------------- #
# reels: the still, set in motion
# --------------------------------------------------------------------------- #
async def test_reel_is_rendered_sent_as_video_and_published_as_a_reel(owner, blobs, chromium):
    from app.agent.tools import _publish_to_instagram
    from app.creative import pipeline
    from app.db import repo
    from app.db.models import Creative, IgAccount, Publication
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "format": {"type": "reel", "aspect_ratio": "1:1"}}
    )
    res = await pipeline.generate(ctx, brief)
    assert res["ok"] and res["format"] == "reel"
    # One image call only: a reel costs the same as a single still.
    assert res["credits_charged"] == 1
    # The delivered url is the video, not the still.
    assert res["image_urls"][0].endswith("reel.mp4")

    with session_scope() as db:
        row = db.query(Creative).filter(Creative.brief_id == uuid.UUID(res["brief_id"])).one()
        assert row.width == 1080 and row.height == 1920
        assert row.video_key and row.video_url and row.video_key.endswith("reel.mp4")
        assert row.composed_key and row.composed_url, "the still cover is kept too"
        mp4 = blobs[row.video_key]
    # A real, playable MP4 shaped for Meta's fetcher.
    from app.creative import reel

    info = reel.probe(mp4)
    assert info["width"] == 1080 and info["height"] == 1920 and info["faststart"] is True

    with session_scope() as db:
        repo.mark_approved(db, brief_id=res["brief_id"], via="button", account_id=account_id)
        db.add(
            IgAccount(
                brand_id=brand_id,
                ig_user_id="17841400000000000",
                status="connected",
                access_token="tok",
                token_expires_at=datetime.now(UTC) + timedelta(days=2),
            )
        )
    pub = await _publish_to_instagram(ctx, {"brief_id": res["brief_id"]})
    assert pub["ok"] and pub["media_type"] == "REELS", pub
    with session_scope() as db:
        publication = (
            db.query(Publication)
            .join(Creative, Publication.creative_id == Creative.id)
            .filter(Creative.brief_id == uuid.UUID(res["brief_id"]))
            .one()
        )
        assert publication.media_type == "REELS"
        row = db.query(Creative).filter(Creative.brief_id == uuid.UUID(res["brief_id"])).one()
        # Both the still cover and the video are promoted out of the draft prefix.
        assert row.composed_key.startswith("published/")
        assert row.video_key.startswith("published/") and row.status == "published"


async def test_revising_a_reel_re_renders_the_video_free(owner, blobs, chromium):
    from app.creative import pipeline
    from app.db.models import Creative
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    brief = CreativeBrief.model_validate({**EXAMPLE, "format": {"type": "reel"}})
    first = await pipeline.generate(ctx, brief)
    assert first["ok"] and _balance(account_id) == 9

    revised = await pipeline.recompose(
        ctx, brief_id=uuid.UUID(first["brief_id"]), changes={"headline": "Now in bigger jars"}
    )
    assert revised["ok"] and revised["credits_charged"] == 0
    assert revised["image_urls"][0].endswith("reel.mp4")
    assert _balance(account_id) == 9, "a reel revision is free, like any copy change"
    with session_scope() as db:
        row = db.query(Creative).filter(Creative.brief_id == uuid.UUID(revised["brief_id"])).one()
        assert row.video_url and row.video_key.endswith("reel.mp4")
