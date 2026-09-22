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


def _chain(n: int, *, broken_at: int | None = None):
    """n briefs, each the child of the one before. `broken_at` leaves that
    version's parent link unset -- the picture-revision bug this package fixes."""
    rows, parent = [], None
    for i in range(1, n + 1):
        row = types.SimpleNamespace(
            id=uuid.uuid4(),
            parent_brief_id=None if (parent is None or broken_at == i) else parent.id,
            version=i,
        )
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
        ctx, brief_id=uuid.UUID(first["brief_id"]), new_prompt=None
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
    )
    assert v2["ok"], v2
    v3 = await pipeline.regenerate_image(ctx, brief_id=uuid.UUID(v2["brief_id"]), new_prompt=None)
    assert v3["ok"], v3
    v4 = await pipeline.recompose(
        ctx,
        brief_id=uuid.UUID(v3["brief_id"]),
        changes={"cta": "Order today"},
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
        ids = (root.id, mid.id, leaf.id)
        assert all(db.get(Brief, i).root_brief_id is None for i in ids)
        db.execute(sql_text(mod.BACKFILL))
        db.expire_all()
        assert [db.get(Brief, i).root_brief_id for i in ids] == [root.id] * 3
        assert repo.revision_no(db, leaf.id) == 2


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
