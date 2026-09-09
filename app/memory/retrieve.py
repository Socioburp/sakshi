"""Retrieval over brand_memory. Cosine distance, pgvector.

`search` takes an optional precomputed vector so a caller running several
lanes over the same question embeds it once instead of once per lane.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import BrandMemory
from app.memory.embed import embed_one

# Below this similarity a hit is noise, and noise in the prompt is worse than
# an empty section: the model treats whatever it is given as relevant.
MIN_SIMILARITY = 0.35


def search(
    db: Session,
    *,
    brand_id: uuid.UUID,
    query: str,
    k: int = 5,
    kinds: tuple[str, ...] | list[str] | None = None,
    min_similarity: float = MIN_SIMILARITY,
    vector: list[float] | None = None,
) -> list[tuple[BrandMemory, float]]:
    vec = vector if vector is not None else embed_one(query, input_type="query")
    distance = BrandMemory.embedding.cosine_distance(vec).label("distance")
    stmt = (
        select(BrandMemory, distance)
        .where(BrandMemory.brand_id == brand_id)
        .where(BrandMemory.embedding.is_not(None))
    )
    if kinds:
        stmt = stmt.where(BrandMemory.kind.in_(list(kinds)))
    rows = db.execute(stmt.order_by(distance).limit(k)).all()
    hits = [(row[0], 1.0 - float(row[1])) for row in rows]
    return [(m, s) for m, s in hits if s >= min_similarity]


def as_prompt_block(hits: list[tuple[BrandMemory, float]]) -> str:
    if not hits:
        return ""
    lines = [f"- ({m.kind}) {m.content}" for m, _ in hits]
    return "Possibly relevant past context (may be stale, use judgement):\n" + "\n".join(lines)
