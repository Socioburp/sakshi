"""Pipeline behaviour that only a real database can prove.

Skips cleanly without one (CI has no Postgres). Locally, with the schema
applied, these are the checks that caught the review findings.
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
            conn.execute(sql_text("select 1 from creatives limit 1"))
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
    """These compose real pixels; skip where Chromium cannot launch."""
    from app.creative import compose

    try:
        await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Chromium for the compositor: {str(exc)[:80]}")
    yield
    await compose.shutdown()


@pytest.fixture
def owner(db_ready, chromium):
    from app.db.models import Account, Brand
    from app.db.session import session_scope

    with session_scope() as db:
        acct = Account(wa_phone=f"test-{uuid.uuid4().hex[:12]}", credits_balance=10)
        db.add(acct)
        db.flush()
        brand = Brand(
            account_id=acct.id,
            name="Pipeline Test",
            palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
            fonts={"heading": "Poppins", "body": "Inter"},
        )
        db.add(brand)
        db.flush()
        return acct.id, brand.id


def _ctx(account_id, brand_id):
    from app.agent.context import ToolContext
    from app.telemetry.stages import trace

    t = trace(account_id=account_id).__enter__()
    # No live WhatsApp session on purpose: delivery must be reported honestly.
    return ToolContext(account_id, brand_id, None, "", t)


def _balance(account_id) -> int:
    from app.db.models import Account
    from app.db.session import session_scope

    with session_scope() as db:
        return db.get(Account, account_id).credits_balance


async def test_regenerating_one_slide_charges_one(owner, blobs):
    from app.creative import pipeline

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    first = await pipeline.generate(ctx, brief)
    assert first["ok"] and first["credits_charged"] == 3
    assert _balance(account_id) == 7

    redo = await pipeline.regenerate_image(
        ctx, brief_id=uuid.UUID(first["brief_id"]), new_prompt=None, slide_position=2
    )
    assert redo["ok"], redo
    assert redo["credits_charged"] == 1, "one slide redone must cost one credit, not three"
    assert _balance(account_id) == 6

    bad = await pipeline.regenerate_image(
        ctx, brief_id=uuid.UUID(first["brief_id"]), new_prompt=None, slide_position=9
    )
    assert bad == {"ok": False, "reason": "unknown_slide", "hint": bad["hint"]}
    assert _balance(account_id) == 6


async def test_made_up_reference_asset_is_billed_and_generated(owner, blobs):
    from app.creative import pipeline

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    brief = CreativeBrief.model_validate(
        {
            **EXAMPLE,
            "visual_direction": {
                **EXAMPLE["visual_direction"],
                "reference_asset_id": str(uuid.uuid4()),
            },
        }
    )
    res = await pipeline.generate(ctx, brief)
    assert res["ok"] and res["credits_charged"] == 1
    assert _balance(account_id) == 9


async def test_delivery_is_reported_honestly(owner, blobs):
    from app.creative import pipeline

    account_id, brand_id = owner
    res = await pipeline.generate(_ctx(account_id, brand_id), CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"]
    assert res["shown_to_user"] is False
    assert "NOT deliver" in res["note"]
    assert res["credits_left"] == 9


async def test_low_credit_nudge(owner, blobs):
    from app.creative import pipeline
    from app.db.models import Account
    from app.db.session import session_scope

    account_id, brand_id = owner
    with session_scope() as db:
        db.get(Account, account_id).credits_balance = 3
    res = await pipeline.generate(_ctx(account_id, brand_id), CreativeBrief.model_validate(EXAMPLE))
    assert res["credits_left"] == 2 and "credits_note" in res


async def test_new_prompt_without_a_position_reaches_every_slide(owner, blobs):
    from app.creative import pipeline
    from app.db.models import Brief
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    first = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    redo = await pipeline.regenerate_image(
        ctx,
        brief_id=uuid.UUID(first["brief_id"]),
        new_prompt="a brass thali of pickles on a stone counter, hard afternoon sun",
        slide_position=None,
    )
    assert redo["ok"] and redo["credits_charged"] == 3
    with session_scope() as db:
        payload = db.get(Brief, uuid.UUID(redo["brief_id"])).payload
    assert all("brass thali" in s["visual_direction"]["prompt"] for s in payload["slides"])


async def test_one_slide_cannot_be_redone_on_an_expired_draft(owner, blobs):
    from app.creative import pipeline
    from app.db.models import Creative
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    first = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    with session_scope() as db:
        for c in db.query(Creative).filter(Creative.brand_id == brand_id):
            c.expires_at = datetime.now(UTC) - timedelta(days=1)
    res = await pipeline.regenerate_image(
        ctx, brief_id=uuid.UUID(first["brief_id"]), new_prompt=None, slide_position=2
    )
    assert res["reason"] == "background_expired"
    assert _balance(account_id) == 7, "a refused redo must not charge"


async def test_reused_slides_keep_their_background_key(owner, blobs):
    from app.creative import pipeline
    from app.db.models import Creative
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    first = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    with session_scope() as db:
        old = {
            c.slide_position: c.background_key
            for c in db.query(Creative).filter(Creative.brief_id == uuid.UUID(first["brief_id"]))
        }
    bg_puts_before = len([k for k in blobs if "bg." in k])
    redo = await pipeline.regenerate_image(
        ctx, brief_id=uuid.UUID(first["brief_id"]), new_prompt=None, slide_position=2
    )
    with session_scope() as db:
        new = {
            c.slide_position: (c.background_key, c.imagegen_provider)
            for c in db.query(Creative).filter(Creative.brief_id == uuid.UUID(redo["brief_id"]))
        }
    assert new[1] == (old[1], "reused") and new[3] == (old[3], "reused")
    assert new[2][0] != old[2]
    assert len([k for k in blobs if "bg." in k]) == bg_puts_before + 1


async def test_recompose_refuses_an_expired_draft(owner, blobs):
    from app.creative import pipeline
    from app.db.models import Creative
    from app.db.session import session_scope

    account_id, brand_id = owner
    ctx = _ctx(account_id, brand_id)
    first = await pipeline.generate(ctx, CreativeBrief.model_validate(EXAMPLE))
    with session_scope() as db:
        for c in db.query(Creative).filter(Creative.brand_id == brand_id):
            c.expires_at = datetime.now(UTC) - timedelta(days=1)
    res = await pipeline.recompose(
        ctx, brief_id=uuid.UUID(first["brief_id"]), changes={"headline": "Aaj hi lein"}
    )
    assert res["reason"] == "background_expired"
    assert _balance(account_id) == 9, "a refused revision must not charge"
