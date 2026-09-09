"""Grounding lanes.

The pure tests run anywhere. The retrieval tests need a real Postgres with
pgvector, because the thing under test IS the vector query -- mocking the
database here would only assert that the mock works. They skip cleanly when
no database is reachable.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.config import settings
from app.db.models import BrandMemory
from app.memory import grounding as G


# --------------------------------------------------------------------------- #
# deterministic stand-in for Voyage: same words -> near-identical vectors
# --------------------------------------------------------------------------- #
def fake_vector(text: str) -> list[float]:
    vec = [0.0] * settings.embed_dim
    for word in {w for w in text.lower().split() if len(w) > 2}:
        idx = int(hashlib.sha1(word.encode()).hexdigest(), 16) % settings.embed_dim
        vec[idx] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


@pytest.fixture
def fake_embeddings(monkeypatch):
    def stub(text, input_type="query"):
        return fake_vector(text)

    for target in (
        "app.memory.embed.embed_one",
        "app.memory.grounding.embed_one",
        "app.memory.retrieve.embed_one",
    ):
        monkeypatch.setattr(target, stub)


# --------------------------------------------------------------------------- #
# pure
# --------------------------------------------------------------------------- #
def _mem(kind: str, content: str) -> BrandMemory:
    return BrandMemory(id=uuid.uuid4(), kind=kind, content=content)


def test_lanes_cover_the_three_contract_lists():
    assert {lane.key for lane in G.LANES} == {"catalog", "style", "rejection"}
    keys = set(G.Grounded().as_brief_grounding())
    assert keys == {"catalog_item_ids", "style_anchor_ids", "rejection_ids"}


def test_rejection_lane_has_the_loosest_threshold():
    """Asymmetric cost: missing a 'never do this' is expensive, surfacing a
    spurious one is cheap. The thresholds must reflect that."""
    lanes = G.LANE_BY_KEY
    assert lanes["rejection"].min_similarity < lanes["style"].min_similarity
    assert lanes["style"].min_similarity < lanes["catalog"].min_similarity


def test_prompt_block_keeps_lanes_apart():
    g = G.Grounded(
        hits={
            "catalog": [(_mem("product", "Coconut oil 500ml Rs 249"), 0.9)],
            "rejection": [(_mem("rejection", "No pictures of bare feet"), 0.4)],
        }
    )
    block = g.as_prompt_block()
    assert "Products this brand actually sells" in block
    assert "already turned down" in block
    # A merged list would let a rejection read as a suggestion.
    assert block.index("Coconut oil") < block.index("bare feet")
    assert "What has worked" not in block  # empty lane omitted entirely


def test_empty_grounding_renders_nothing():
    assert G.Grounded().as_prompt_block() == ""
    assert G.Grounded().is_empty() is True


def test_ids_map_to_the_brief_contract():
    prod, rej = _mem("product", "a"), _mem("rejection", "b")
    g = G.Grounded(hits={"catalog": [(prod, 0.9)], "rejection": [(rej, 0.5)]})
    out = g.as_brief_grounding()
    assert out["catalog_item_ids"] == [str(prod.id)]
    assert out["rejection_ids"] == [str(rej.id)]
    assert out["style_anchor_ids"] == []


def test_blank_query_does_not_hit_the_database():
    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("should not query on an empty message")

    assert G.ground(Boom(), brand_id=uuid.uuid4(), query="   ").is_empty()


# --------------------------------------------------------------------------- #
# against real pgvector
# --------------------------------------------------------------------------- #
@pytest.fixture
def seeded_brand(fake_embeddings):
    from sqlalchemy import text as sql_text

    from app.db.models import Account, Brand
    from app.db.session import engine, session_scope

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("select 1 from brand_memory limit 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database with the schema available: {str(exc)[:80]}")

    rows = [
        ("product", "Cold pressed coconut oil 500ml, Rs 249"),
        ("product", "Wood pressed groundnut oil 1 litre, Rs 420"),
        ("style_anchor", "The warm terracotta flatlay post did really well"),
        ("feedback", "Owner liked the softer morning light"),
        ("rejection", "Do not use the word cheap, they hated it"),
        ("rejection", "No stock photos of foreign models"),
        ("note", "Shop is closed on Tuesdays"),
    ]
    with session_scope() as db:
        acct = Account(wa_phone=f"test-{uuid.uuid4().hex[:12]}", credits_balance=0)
        db.add(acct)
        db.flush()
        brand = Brand(account_id=acct.id, name="Grounding Test")
        db.add(brand)
        db.flush()
        for kind, content in rows:
            db.add(
                BrandMemory(
                    brand_id=brand.id, kind=kind, content=content, embedding=fake_vector(content)
                )
            )
        brand_id = brand.id
        account_id = acct.id

    yield brand_id

    with session_scope() as db:
        db.query(BrandMemory).filter(BrandMemory.brand_id == brand_id).delete()
        db.query(Brand).filter(Brand.id == brand_id).delete()
        db.query(Account).filter(Account.id == account_id).delete()


def test_catalog_lane_returns_only_products(seeded_brand):
    from app.db.session import session_scope

    with session_scope() as db:
        g = G.ground(db, brand_id=seeded_brand, query="coconut oil 500ml Rs 249")
    kinds = {m.kind for m, _ in g.hits["catalog"]}
    assert kinds <= {"product"}
    assert any("coconut" in m.content.lower() for m, _ in g.hits["catalog"])


def test_rejections_surface_on_their_own_lane(seeded_brand):
    from app.db.session import session_scope

    with session_scope() as db:
        g = G.ground(db, brand_id=seeded_brand, query="do not use the word cheap")
    assert {m.kind for m, _ in g.hits["rejection"]} <= {"rejection"}
    assert any("cheap" in m.content.lower() for m, _ in g.hits["rejection"])


def test_a_note_never_leaks_into_a_lane(seeded_brand):
    """'note' belongs to no lane. If it appears, the kind filter is broken."""
    from app.db.session import session_scope

    with session_scope() as db:
        g = G.ground(db, brand_id=seeded_brand, query="shop closed on Tuesdays")
    every = [m.kind for rows in g.hits.values() for m, _ in rows]
    assert "note" not in every


def test_grounding_is_scoped_to_one_brand(seeded_brand):
    from app.db.session import session_scope

    with session_scope() as db:
        g = G.ground(db, brand_id=uuid.uuid4(), query="coconut oil 500ml")
    assert g.is_empty()


@pytest.mark.asyncio
async def test_brief_grounding_is_stamped_server_side(monkeypatch):
    """The model does not get to author the grounding list.

    It records what retrieval handed over. If the model wrote it, a bad
    creative would tell you what the model believed rather than what it was
    given -- which is the opposite of useful when you are debugging.
    """
    import json as _json

    from app.agent import tools as T
    from app.agent.context import ToolContext
    from app.creative.brief import EXAMPLE
    from app.telemetry.stages import Trace

    real = _mem("product", "Coconut oil 500ml Rs 249")
    grounded = G.Grounded(hits={"catalog": [(real, 0.9)]})

    captured = {}

    async def fake_generate(ctx, brief):
        captured["brief"] = brief
        return {"ok": True}

    monkeypatch.setattr(T.pipeline, "generate", fake_generate)

    ctx = ToolContext(
        account_id=uuid.uuid4(),
        brand_id=uuid.uuid4(),
        session_id=None,
        wa_id="91999",
        trace=Trace(trace_id="t"),
        grounding=grounded,
    )

    # The model invents ids that were never retrieved.
    payload = _json.loads(_json.dumps(EXAMPLE))
    payload["grounding"] = {
        "catalog_item_ids": ["totally-made-up"],
        "style_anchor_ids": ["also-invented"],
        "rejection_ids": [],
    }

    await T._create_creative(ctx, {"brief": payload})

    stamped = captured["brief"].grounding
    assert stamped.catalog_item_ids == [str(real.id)]
    assert stamped.style_anchor_ids == []
    assert "totally-made-up" not in stamped.catalog_item_ids
