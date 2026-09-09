"""Grounding: the three retrieval lanes the brief contract already names.

`docs/brief_schema.json` defines a `grounding` object with catalog_item_ids,
style_anchor_ids and rejection_ids, "logged for debugging retrieval quality".
This module is what fills them.

Three lanes rather than one search, because they are not the same question
and should not share a threshold:

  catalogue   what this brand actually sells. Precision matters -- naming a
              product they do not stock is worse than naming none, so the bar
              is high and the list is short.
  style       creatives that worked, and feedback about them. Middling bar;
              these shape tone, and a near-miss is still useful direction.
  rejections  what the client turned down. The bar is LOW on purpose. Missing
              a "never show bare feet" costs a rejected creative and some
              trust; surfacing an irrelevant one costs a few tokens. Recall
              beats precision when the downside is asymmetric.

The question is embedded once and reused across all three lanes -- three
Voyage calls per turn would be three times the cost and latency for the same
vector.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.db.models import BrandMemory
from app.logging import get_logger
from app.memory.embed import embed_one
from app.memory.retrieve import search

log = get_logger(__name__)


@dataclass(frozen=True)
class Lane:
    key: str
    kinds: tuple[str, ...]
    k: int
    min_similarity: float
    heading: str


LANES: tuple[Lane, ...] = (
    Lane(
        key="catalog",
        kinds=("product",),
        k=4,
        min_similarity=0.45,
        heading="Products this brand actually sells (use these exact names and prices)",
    ),
    Lane(
        key="style",
        kinds=("style_anchor", "past_creative", "feedback"),
        k=3,
        min_similarity=0.38,
        heading="What has worked for this brand before",
    ),
    Lane(
        key="rejection",
        kinds=("rejection",),
        k=3,
        min_similarity=0.25,
        heading="Things this client has already turned down -- do not repeat them",
    ),
)

LANE_BY_KEY = {lane.key: lane for lane in LANES}


@dataclass
class Grounded:
    """What retrieval returned, per lane, plus the ids to log on the brief."""

    hits: dict[str, list[tuple[BrandMemory, float]]] = field(default_factory=dict)

    def ids(self, key: str) -> list[str]:
        return [str(m.id) for m, _ in self.hits.get(key, [])]

    def as_brief_grounding(self) -> dict[str, list[str]]:
        return {
            "catalog_item_ids": self.ids("catalog"),
            "style_anchor_ids": self.ids("style"),
            "rejection_ids": self.ids("rejection"),
        }

    def counts(self) -> dict[str, int]:
        return {key: len(self.hits.get(key, [])) for key in LANE_BY_KEY}

    def is_empty(self) -> bool:
        return not any(self.hits.values())

    def as_prompt_block(self) -> str:
        """Each lane gets its own heading. One merged list would let a
        rejection read as a suggestion."""
        sections: list[str] = []
        for lane in LANES:
            rows = self.hits.get(lane.key) or []
            if not rows:
                continue
            lines = [f"- {m.content}" for m, _ in rows]
            sections.append(f"{lane.heading}:\n" + "\n".join(lines))
        if not sections:
            return ""
        return (
            "## What we know about this brand\n"
            "Retrieved for this message. It may be stale or wrong -- if it "
            "contradicts what the owner just said, they are right.\n\n"
            + "\n\n".join(sections)
        )


def ground(db: Session, *, brand_id: uuid.UUID, query: str) -> Grounded:
    if not query or not query.strip():
        return Grounded()
    try:
        vector = embed_one(query, input_type="query")
    except Exception as exc:  # noqa: BLE001 - grounding is an enhancement, not a gate
        log.error("grounding_embed_failed", brand_id=str(brand_id), error=str(exc)[:200])
        return Grounded()

    result = Grounded()
    for lane in LANES:
        try:
            result.hits[lane.key] = search(
                db,
                brand_id=brand_id,
                query=query,
                k=lane.k,
                kinds=lane.kinds,
                min_similarity=lane.min_similarity,
                vector=vector,
            )
        except Exception as exc:  # noqa: BLE001 - one bad lane must not lose the others
            log.error("grounding_lane_failed", lane=lane.key, error=str(exc)[:200])
            result.hits[lane.key] = []

    log.info("grounded", brand_id=str(brand_id), counts=result.counts())
    return result
