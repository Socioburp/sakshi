"""The revision ladder: every version of a creative knows which version it is,
the owner's words are stored with the change, a change is verified before it
is made, and the agent is told which rung it is on.

Pure tests first; the database parts follow the *_db pattern (skip without
Postgres, run in CI after `alembic upgrade head`).
"""

from __future__ import annotations

import importlib.util
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agent.tools import TOOLS, _owner_request
from app.creative import pipeline
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief
from app.db import repo

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0014_brief_root.py"


# --------------------------------------------------------------------------- #
# lineage arithmetic
# --------------------------------------------------------------------------- #
class _Rows:
    """db.get over an in-memory set of Brief-shaped rows."""

    def __init__(self, *rows):
        self.rows = {r.id: r for r in rows}

    def get(self, model, key):
        return self.rows.get(key)

    def flush(self):
        pass


_ACCOUNT, _BRAND = uuid.uuid4(), uuid.uuid4()


def _chain(n: int, *, broken_at: int | None = None):
    """n briefs, each the child of the one before. `broken_at` leaves that
    version's parent link unset -- the picture-revision bug this package fixes."""
    rows, parent = [], None
    for i in range(1, n + 1):
        parent_id = None if (parent is None or broken_at == i) else parent.id
        row = types.SimpleNamespace(
            id=uuid.uuid4(),
            account_id=_ACCOUNT,
            brand_id=_BRAND,
            payload=dict(EXAMPLE),
            status="draft",
            parent_brief_id=parent_id,
            root_brief_id=(parent.root_brief_id if parent_id else None),
            version=i,
        )
        if row.root_brief_id is None:
            row.root_brief_id = row.id
        rows.append(row)
        parent = row
    return rows


def test_a_root_has_had_no_change_requests():
    (root,) = _chain(1)
    db = _Rows(root)
    assert repo.revision_no(db, root.id) == 0
    assert repo.lineage(db, root.id) == [root]


def test_revision_no_counts_the_hops_to_the_root():
    rows = _chain(4)
    db = _Rows(*rows)
    assert [repo.revision_no(db, r.id) for r in rows] == [0, 1, 2, 3]
    assert repo.lineage(db, rows[-1].id) == rows, "root first, the asked-for version last"


def test_a_broken_parent_link_undercounts_which_is_why_it_must_never_break():
    rows = _chain(4, broken_at=3)
    db = _Rows(*rows)
    assert repo.revision_no(db, rows[-1].id) == 1, "v3 became a root: v4 looks like one change"


def test_an_unknown_brief_is_a_root_not_a_crash():
    assert repo.revision_no(_Rows(), uuid.uuid4()) == 0
    assert repo.lineage(_Rows(), uuid.uuid4()) == []


def test_a_cycle_cannot_hang_the_walk():
    a, b = _chain(2)
    a.parent_brief_id = b.id
    assert len(repo.lineage(_Rows(a, b), b.id)) == 2


def test_the_backfill_walks_parents_from_every_root():
    spec = importlib.util.spec_from_file_location("m0014", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "WITH RECURSIVE" in mod.BACKFILL and "parent_brief_id IS NULL" in mod.BACKFILL
    assert "root_brief_id IS NULL" in mod.BACKFILL, "re-running it never rewrites a stitched row"
    assert mod.revision == "0014" and mod.down_revision == "0013"


# --------------------------------------------------------------------------- #
# a change is verified, a non-change refused
# --------------------------------------------------------------------------- #
def _dump(payload: dict) -> dict:
    return CreativeBrief.model_validate(payload).model_dump(mode="json")


def test_the_diff_names_every_field_that_changed_and_nothing_else():
    old = _dump(EXAMPLE)
    new = _dump(
        {
            **EXAMPLE,
            "headline": "Aaj hi lein",
            "caption": {**EXAMPLE["caption"], "hashtags": ["#oil"]},
            "template_id": "split_card",
        }
    )
    diff = pipeline.payload_diff(old, new)
    assert set(diff) == {"headline", "caption.hashtags", "template_id"}
    assert diff["headline"] == [EXAMPLE["headline"], "Aaj hi lein"]
    assert diff["template_id"] == ["centered_overlay", "split_card"]


def test_whitespace_that_validation_strips_is_not_a_change():
    old = _dump(EXAMPLE)
    new = _dump(pipeline._merge(EXAMPLE, {"headline": "  " + EXAMPLE["headline"] + "  "}))
    assert pipeline.payload_diff(old, new) == {}


def test_a_carousel_diff_names_the_slide():
    old = _dump(EXAMPLE_CAROUSEL)
    slides = [dict(s) for s in EXAMPLE_CAROUSEL["slides"]]
    slides[1] = {**slides[1], "headline": "Second slide, new words"}
    new = _dump({**EXAMPLE_CAROUSEL, "slides": slides})
    diff = pipeline.payload_diff(old, new)
    assert list(diff) == ["slides[2].headline"]


def test_long_values_are_cut_so_an_event_row_stays_readable():
    long = "x" * 400
    diff = pipeline.payload_diff({"alt_text": "a"}, {"alt_text": long})
    assert len(diff["alt_text"][1]) == pipeline.DIFF_VALUE_LIMIT


def test_a_wish_the_brief_cannot_express_is_named():
    assert pipeline.unsupported_changes({"badge": "NEW", "headline": "x", "price": 99}) == [
        "badge",
        "price",
    ]
    assert pipeline.unsupported_changes({"headline": "x", "caption": {}}) == []


async def test_an_unsupported_change_is_refused_before_any_database_work(monkeypatch):
    def no_db():
        raise AssertionError("nothing may be loaded or stored for a refused change")

    monkeypatch.setattr(pipeline, "session_scope", no_db)
    res = await pipeline.recompose(
        types.SimpleNamespace(brand_id=uuid.uuid4()),
        brief_id=uuid.uuid4(),
        changes={"logo_size": "big", "headline": "Fine"},
        owner_request="make the logo bigger",
    )
    assert res["ok"] is False and res["reason"] == "unsupported_change"
    assert res["unsupported"] == ["logo_size"]
    assert "regenerate_image" in res["hint"] and "headline" in res["hint"]


async def test_a_change_that_changes_nothing_is_refused_and_nothing_is_saved(monkeypatch):
    from contextlib import contextmanager

    parent = types.SimpleNamespace(id=uuid.uuid4(), payload=_dump(EXAMPLE), version=1)
    saved = []

    class Db:
        def get(self, model, key):
            return parent if model.__name__ == "Brief" else types.SimpleNamespace(id=key)

    @contextmanager
    def scope():
        yield Db()

    monkeypatch.setattr(pipeline, "session_scope", scope)
    monkeypatch.setattr(pipeline.repo, "save_brief", lambda *a, **k: saved.append(k))
    monkeypatch.setattr(pipeline, "layout_gate", None)  # must not be reached either
    ctx = types.SimpleNamespace(brand_id=uuid.uuid4(), account_id=uuid.uuid4(), message_id=None)
    for changes in ({}, {"headline": None}, {"headline": EXAMPLE["headline"]}):
        res = await pipeline.recompose(ctx, brief_id=parent.id, changes=changes, owner_request="?")
        assert res["ok"] is False and res["reason"] == "nothing_changed", changes
        assert "regenerate_image" in res["hint"]
    assert saved == []


# --------------------------------------------------------------------------- #
# the tool surface
# --------------------------------------------------------------------------- #
def test_the_owner_s_words_are_required_on_both_revision_tools():
    for name in ("revise_creative", "regenerate_image"):
        tool = next(t for t in TOOLS if t["name"] == name)
        assert "owner_request" in tool["input_schema"]["required"], name
        assert tool["input_schema"]["properties"]["owner_request"]["maxLength"] == 300


def test_revise_no_longer_promises_a_badge_or_a_price():
    revise = next(t for t in TOOLS if t["name"] == "revise_creative")
    text = revise["description"]
    assert "CTA, badge, price" not in text
    assert "nothing renders a badge" in text and "applied" in text
    assert set(revise["input_schema"]["properties"]["changes"]["properties"]) <= pipeline.REVISABLE


def test_an_empty_owner_request_is_stored_as_nothing_asked():
    assert _owner_request({"owner_request": "   "}) is None
    assert _owner_request({}) is None
    assert _owner_request({"owner_request": "x" * 400}) == "x" * 300


# --------------------------------------------------------------------------- #
# approval closes the loop
# --------------------------------------------------------------------------- #
def test_the_approve_vote_records_which_rung_it_was_won_on(monkeypatch):
    from app.insights import votes

    recorded: list[dict] = []
    monkeypatch.setattr(votes, "record", lambda db, **kw: recorded.append(kw))
    rows = _chain(3)
    db = _Rows(*rows)

    votes.approve(db, str(rows[-1].id), remember=False)
    votes.approve(db, str(rows[0].id), remember=False)

    third, first = (r["meta"] for r in recorded)
    assert third["revisions_before_approval"] == 2 and third["first_time_right"] is False
    assert third["version"] == 3 and third["root_brief_id"] == str(rows[0].id)
    assert first["revisions_before_approval"] == 0 and first["first_time_right"] is True
    assert first["version"] == 1 and first["root_brief_id"] == str(rows[0].id)
    assert all(r["kind"] == "approve" for r in recorded)


def test_a_root_written_before_the_column_still_names_itself_on_approval(monkeypatch):
    from app.insights import votes

    recorded: list[dict] = []
    monkeypatch.setattr(votes, "record", lambda db, **kw: recorded.append(kw))
    (root,) = _chain(1)
    root.root_brief_id = None  # a pre-0014 row the backfill has not reached
    votes.approve(_Rows(root), str(root.id), remember=False)
    assert recorded[0]["meta"]["root_brief_id"] == str(root.id)


def test_approval_lands_on_the_brief_not_only_on_its_slides(monkeypatch):
    rows = _chain(2)
    slides = [
        types.SimpleNamespace(approved_at=None, approved_via=None, status="ready") for _ in range(2)
    ]
    monkeypatch.setattr(repo, "creatives_for_brief", lambda db, bid: slides)
    db = _Rows(*rows)

    stranger = uuid.uuid4()
    assert repo.mark_approved(db, brief_id=str(rows[-1].id), via="button", account_id=stranger) == 0
    assert rows[-1].status == "draft", "a stranger's tap approves nothing"

    assert repo.mark_approved(db, brief_id=str(rows[-1].id), via="button", account_id=_ACCOUNT) == 2
    assert rows[-1].status == "approved" and all(s.status == "approved" for s in slides)
    assert rows[0].status == "draft", "only the version they tapped on"
    assert repo.mark_approved(db, brief_id=str(rows[-1].id), via="button", account_id=_ACCOUNT) == 0
    assert rows[-1].status == "approved", "a second tap changes nothing"
    assert repo.mark_approved(db, brief_id="not-a-uuid", via="button") == 0


# --------------------------------------------------------------------------- #
# against a real database
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_ready():
    from sqlalchemy import text as sql_text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("select root_brief_id from briefs limit 1"))
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
            name="Ladder Test",
            palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
            fonts={"heading": "Poppins", "body": "Inter"},
        )
        db.add(brand)
        db.flush()
        return acct.id, brand.id


def _ctx(account_id, brand_id, message_id=None):
    from app.agent.context import ToolContext
    from app.telemetry.stages import trace

    t = trace(account_id=account_id).__enter__()
    return ToolContext(account_id, brand_id, None, "", t, message_id=message_id)


def _brief(brief_id):
    from app.db.models import Brief
    from app.db.session import session_scope

    with session_scope() as db:
        b = db.get(Brief, uuid.UUID(str(brief_id)))
        return types.SimpleNamespace(
            id=b.id,
            parent_brief_id=b.parent_brief_id,
            root_brief_id=b.root_brief_id,
            version=b.version,
            status=b.status,
            source_message_id=b.source_message_id,
            payload=b.payload,
        )


async def test_a_picture_revision_is_version_two_of_the_same_creative(owner, blobs, chromium):
    from app.creative import pipeline
    from app.db.models import CreativeEvent
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    first = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE))
    assert first["ok"] and first["version"] == 1 and first["revision_no"] == 0
    assert first["root_brief_id"] == first["brief_id"], "a root names itself"

    redo = await pipeline.regenerate_image(
        ctx, brief_id=uuid.UUID(first["brief_id"]), new_prompt=None, owner_request="new picture"
    )
    assert redo["ok"], redo
    assert redo["version"] == 2 and redo["revision_no"] == 1
    child, root = _brief(redo["brief_id"]), _brief(first["brief_id"])
    assert child.parent_brief_id == root.id and child.root_brief_id == root.id
    assert root.root_brief_id == root.id and root.status == "superseded"
    with session_scope() as db:
        kinds = [
            e.kind
            for e in db.query(CreativeEvent)
            .filter(CreativeEvent.brand_id == brand_id)
            .order_by(CreativeEvent.created_at)
        ]
        assert kinds.count("created") == 1, "a revision is not a second creative"
        assert kinds.count("regenerate") == 1
        assert repo.revision_no(db, child.id) == 1


async def test_one_slide_of_a_carousel_is_a_revision_too(owner, blobs, chromium):
    from app.creative import pipeline

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    first = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    redo = await pipeline.regenerate_image(
        ctx,
        brief_id=uuid.UUID(first["brief_id"]),
        new_prompt=None,
        slide_position=2,
        owner_request="slide 2 looks dull",
    )
    assert redo["ok"] and redo["credits_charged"] == 1
    child = _brief(redo["brief_id"])
    assert child.version == 2 and child.root_brief_id == uuid.UUID(first["brief_id"])


async def test_words_then_picture_then_words_is_version_four(owner, blobs, chromium):
    from app.creative import pipeline
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    v1 = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE))
    v2 = await pipeline.recompose(
        ctx,
        brief_id=uuid.UUID(v1["brief_id"]),
        changes={"headline": "Aaj hi lein"},
        owner_request="headline in Hindi",
    )
    assert v2["ok"], v2
    v3 = await pipeline.regenerate_image(
        ctx, brief_id=uuid.UUID(v2["brief_id"]), new_prompt=None, owner_request="another picture"
    )
    assert v3["ok"], v3
    v4 = await pipeline.recompose(
        ctx,
        brief_id=uuid.UUID(v3["brief_id"]),
        changes={"cta": "Order today"},
        owner_request="change the button",
    )
    assert v4["ok"], v4
    assert (v2["version"], v3["version"], v4["version"]) == (2, 3, 4)
    assert (v2["revision_no"], v3["revision_no"], v4["revision_no"]) == (1, 2, 3)
    root = uuid.UUID(v1["brief_id"])
    assert all(r["root_brief_id"] == str(root) for r in (v1, v2, v3, v4))
    with session_scope() as db:
        chain = repo.lineage(db, uuid.UUID(v4["brief_id"]))
        assert [b.version for b in chain] == [1, 2, 3, 4]
        assert [b.status for b in chain] == ["superseded"] * 3 + ["draft"]


def test_the_backfill_stitches_a_chain_written_before_the_column(owner):
    from sqlalchemy import text as sql_text

    from app.db.models import Brief
    from app.db.session import session_scope

    spec = importlib.util.spec_from_file_location("m0014", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    account_id, brand_id = owner
    with session_scope() as db:
        root = Brief(account_id=account_id, brand_id=brand_id, payload=EXAMPLE)
        db.add(root)
        db.flush()
        mid = Brief(
            account_id=account_id,
            brand_id=brand_id,
            payload=EXAMPLE,
            parent_brief_id=root.id,
            version=2,
        )
        db.add(mid)
        db.flush()
        leaf = Brief(
            account_id=account_id,
            brand_id=brand_id,
            payload=EXAMPLE,
            parent_brief_id=mid.id,
            version=3,
        )
        db.add(leaf)
        db.flush()
        # A picture revision saved by the old bug: version 1, no parent. The
        # link was never written, so the backfill cannot honestly stitch it.
        stray = Brief(account_id=account_id, brand_id=brand_id, payload=EXAMPLE)
        db.add(stray)
        db.flush()
        ids = (root.id, mid.id, leaf.id, stray.id)
        assert all(db.get(Brief, i).root_brief_id is None for i in ids)
        db.execute(sql_text(mod.BACKFILL))
        db.expire_all()
        assert [db.get(Brief, i).root_brief_id for i in ids] == [root.id] * 3 + [stray.id]
        assert repo.revision_no(db, leaf.id) == 2
        assert repo.revision_no(db, stray.id) == 0, "a root of its own, not a crash"
        # Re-running it is harmless: a stitched row is never rewritten.
        db.execute(sql_text(mod.BACKFILL))
        db.expire_all()
        assert db.get(Brief, leaf.id).root_brief_id == root.id


def _session_with_brief(db, account_id, brand_id, *, approved=False, expired=False):
    from app.db.models import Account, Creative

    acct = db.get(Account, account_id)
    wa = f"91{uuid.uuid4().int % 10**10:010d}"
    sess = repo.touch_session(db, acct, wa, inbound=True)
    brief = repo.save_brief(db, account_id=account_id, brand_id=brand_id, payload=EXAMPLE)
    db.add(
        Creative(
            brief_id=brief.id,
            brand_id=brand_id,
            template="centered_overlay",
            slide_position=1,
            status="approved" if approved else "ready",
            approved_at=datetime.now(UTC) if approved else None,
            expires_at=datetime.now(UTC) + timedelta(days=-1 if expired else 7),
        )
    )
    sess.active_brief_id = brief.id
    sess.window_expires_at = datetime.now(UTC) - timedelta(hours=1)  # the window has closed
    db.flush()
    return acct, wa, brief


def test_the_creative_they_were_shown_survives_the_window_closing(owner):
    from app.db.session import session_scope

    account_id, brand_id = owner
    with session_scope() as db:
        acct, wa, brief = _session_with_brief(db, account_id, brand_id)
        fresh = repo.touch_session(db, acct, wa, inbound=True)
        assert fresh.active_brief_id == brief.id, "'change the headline' next morning targets it"
        assert fresh.window_expires_at > datetime.now(UTC)

        acct, wa, brief = _session_with_brief(db, account_id, brand_id, approved=True)
        assert repo.touch_session(db, acct, wa, inbound=True).active_brief_id is None

        acct, wa, brief = _session_with_brief(db, account_id, brand_id, expired=True)
        assert repo.touch_session(db, acct, wa, inbound=True).active_brief_id is None


def _fake_embeddings(monkeypatch):
    from app.memory import embed

    monkeypatch.setattr(
        embed,
        "embed_texts",
        lambda texts, input_type="document": [[0.1] * embed.settings.embed_dim for _ in texts],
    )


async def test_the_owners_words_and_the_diff_are_on_the_revise_event(
    owner, blobs, chromium, monkeypatch
):
    from app.db.models import BrandMemory, CreativeEvent, Message
    from app.db.session import session_scope

    _fake_embeddings(monkeypatch)
    account_id, brand_id = owner
    first = await pipeline.generate(
        _ctx(account_id, brand_id), CreativeBrief.model_validate(EXAMPLE)
    )
    with session_scope() as db:
        msg = Message(
            account_id=account_id,
            provider="mock",
            direction="in",
            kind="audio",
            transcript="headline Hindi mein karo aur button Order today",
        )
        db.add(msg)
        db.flush()
        message_id = msg.id
    ctx = _ctx(account_id, brand_id, message_id=message_id)
    revised = await pipeline.recompose(
        ctx,
        brief_id=uuid.UUID(first["brief_id"]),
        changes={"headline": "Aaj hi lein", "cta": "Order today"},
        owner_request="headline in Hindi and the button should say Order today",
    )
    assert revised["ok"], revised
    assert set(revised["applied"]) == {"headline", "cta"}
    assert revised["applied"]["headline"] == [EXAMPLE["headline"], "Aaj hi lein"]
    assert _brief(revised["brief_id"]).source_message_id == message_id
    with session_scope() as db:
        ev = (
            db.query(CreativeEvent)
            .filter(CreativeEvent.brand_id == brand_id, CreativeEvent.kind == "revise")
            .one()
        )
        assert ev.brief_id == uuid.UUID(first["brief_id"])
        m = ev.meta
        assert m["owner_request"].startswith("headline in Hindi")
        assert m["request_text"] == "headline Hindi mein karo aur button Order today"
        assert m["revision_no"] == 1 and m["version"] == 1
        assert m["new_brief_id"] == revised["brief_id"]
        assert m["root_brief_id"] == first["brief_id"]
        assert m["diff"] == revised["applied"]
        mem = db.query(BrandMemory).filter(BrandMemory.brand_id == brand_id).one()
        assert mem.kind == "feedback" and mem.source_ref == f"brief:{revised['brief_id']}"
        assert mem.content.startswith("Owner asked: headline in Hindi")
        assert "changed cta, headline" in mem.content


async def test_the_regenerate_event_carries_the_prompt_the_words_and_the_new_version(
    owner, blobs, chromium, monkeypatch
):
    from app.db.models import BrandMemory, CreativeEvent
    from app.db.session import session_scope

    _fake_embeddings(monkeypatch)
    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    first = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    redo = await pipeline.regenerate_image(
        ctx,
        brief_id=uuid.UUID(first["brief_id"]),
        new_prompt="a brass thali of pickles on a stone counter, hard afternoon sun",
        slide_position=2,
        owner_request="slide 2 is too dark, show the pickles",
    )
    assert redo["ok"], redo
    with session_scope() as db:
        ev = (
            db.query(CreativeEvent)
            .filter(CreativeEvent.brand_id == brand_id, CreativeEvent.kind == "regenerate")
            .one()
        )
        m = ev.meta
        assert m["new_prompt"].startswith("a brass thali") and m["slide_position"] == 2
        assert m["owner_request"] == "slide 2 is too dark, show the pickles"
        assert m["new_brief_id"] == redo["brief_id"] and m["revision_no"] == 1
        assert m["root_brief_id"] == first["brief_id"] and m["ok"] is True
        assert list(m["diff"]) == ["slides[2].visual_direction"]
        mem = db.query(BrandMemory).filter(BrandMemory.brand_id == brand_id).one()
        assert mem.kind == "rejection" and mem.source_ref == f"brief:{redo['brief_id']}"
        assert "slide 2 picture" in mem.content


async def test_approval_after_one_change_is_recorded_as_not_first_time_right(
    owner, blobs, chromium
):
    """The webhook's path on a 'Post to Instagram' tap: repo.mark_approved,
    then votes.approve. The 'approve' event is where "was the first result
    good enough" is answered, so it must say which rung it was won on."""
    from app.db.models import CreativeEvent
    from app.db.session import session_scope
    from app.insights import votes

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    v1 = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE))
    v2 = await pipeline.recompose(
        ctx,
        brief_id=uuid.UUID(v1["brief_id"]),
        changes={"headline": "Aaj hi lein"},
        owner_request="headline in Hindi",
    )
    assert v2["ok"], v2
    with session_scope() as db:
        stamped = repo.mark_approved(
            db, brief_id=v2["brief_id"], via="button", account_id=account_id
        )
        assert stamped == 1
        votes.approve(db, v2["brief_id"], remember=False)

    root, leaf = _brief(v1["brief_id"]), _brief(v2["brief_id"])
    assert (root.status, leaf.status) == ("superseded", "approved")
    with session_scope() as db:
        ev = (
            db.query(CreativeEvent)
            .filter(CreativeEvent.brand_id == brand_id, CreativeEvent.kind == "approve")
            .one()
        )
        assert ev.brief_id == leaf.id
        m = ev.meta
        assert m["revisions_before_approval"] == 1 and m["first_time_right"] is False
        assert m["version"] == 2 and m["root_brief_id"] == v1["brief_id"]
        assert m["template"] == "centered_overlay", "the taste facts are still there"
        assert [b.status for b in repo.lineage(db, leaf.id)] == ["superseded", "approved"]


# --------------------------------------------------------------------------- #
# the ladder is visible to the agent
# --------------------------------------------------------------------------- #
def test_the_prompt_teaches_the_ladder():
    from app.agent.prompts import SYSTEM

    body = SYSTEM.split("## Changes", 1)[1].split("## ", 1)[0]
    assert "## Changes" in SYSTEM and SYSTEM.index("## Changes") > SYSTEM.index("cheaper tool")
    for phrase in (
        "WHOLE conversation",
        "ONE call",
        "ONE clarifying question",
        "Never two",
        "`owner_request`",
        "`applied`",
        "last they need",
        "FINAL version",
        "restate every outstanding wish",
        "fresh creative instead of a fourth version",
    ):
        assert phrase in body, phrase


def test_the_rung_rule_hardens_with_every_change_request():
    from app.agent.runner import rung_rule

    assert "last they need" in rung_rule(0) and "FINAL" not in rung_rule(0)
    assert "FINAL version" in rung_rule(1) and "restate every outstanding wish" in rung_rule(1)
    assert "fresh creative" not in rung_rule(1)
    assert "fresh creative (create_creative)" in rung_rule(2) and "2 change requests" in rung_rule(
        2
    )
    assert "fourth version" in rung_rule(3)


def test_the_current_creative_block_counts_from_the_database(monkeypatch):
    from app.agent import runner

    rows = _chain(3)
    db = _Rows(*rows)
    monkeypatch.setattr(
        runner.repo,
        "creatives_for_brief",
        lambda db_, bid: [types.SimpleNamespace(approved_at=None)],
    )
    sess = types.SimpleNamespace(active_brief_id=rows[-1].id, state={})
    block = runner._current_brief_block(db, sess)
    assert "Version 3 of this creative; change requests so far: 2." in block
    assert "FINAL version" in block and "fresh creative" in block
    assert str(rows[-1].id) in block and "NOT yet approved" in block
    assert runner._current_brief_block(db, types.SimpleNamespace(active_brief_id=None)) == ""
