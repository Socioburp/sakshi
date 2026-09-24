"""Voyage embeddings for brand_memory -- and nothing else.

Brand identity is deliberately NOT embedded. It lives in plain columns on
`brands` and goes into every prompt whole, so a `never_say` rule can never be
missed because it ranked below a similarity threshold. What gets embedded here
is the long tail: past creatives, feedback the owner gave, product notes.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import BrandMemory
from app.logging import get_logger

log = get_logger(__name__)

_client = None


def _voyage():
    global _client
    if _client is None:
        import voyageai

        _client = voyageai.Client(api_key=settings.voyage_api_key)
    return _client


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    if not texts:
        return []
    if not settings.voyage_api_key:
        if input_type == "document":
            # A zero vector has no direction: cosine distance to it is NaN, so
            # a row WRITTEN this way can never be retrieved, even after the key
            # is fixed. This used to be guarded by `settings.is_prod`, which is
            # exactly backwards: staging and any deploy that has not had its
            # key set yet are where this actually happens, and they are where
            # it does the damage. Every approval, rejection and product note
            # written without a key is a row that looks saved, reports
            # "brand_memory_written", and can never be read back -- which is
            # what "the bot does not remember my brand" looks like from the
            # outside. Reads still degrade quietly: a zero query matches
            # nothing, which is an empty prompt section, not a poisoned one.
            raise RuntimeError("VOYAGE_API_KEY is unset; refusing to write unretrievable memory")
        # Deterministic stand-in for READS so local runs and tests need no key.
        log.warning("voyage_key_missing", note="query embedded as zeros; retrieval will be empty")
        return [[0.0] * settings.embed_dim for _ in texts]
    res = _voyage().embed(texts, model=settings.voyage_model, input_type=input_type)
    return res.embeddings


def embed_one(text: str, input_type: str = "query") -> list[float]:
    return embed_texts([text], input_type=input_type)[0]


def remember(
    db: Session,
    *,
    brand_id: uuid.UUID,
    kind: str,
    content: str,
    meta: dict | None = None,
    source_ref: str | None = None,
) -> BrandMemory:
    row = BrandMemory(
        brand_id=brand_id,
        kind=kind,
        content=content.strip(),
        embedding=embed_one(content, input_type="document"),
        meta=meta or {},
        source_ref=source_ref,
    )
    db.add(row)
    db.flush()
    log.info("brand_memory_written", brand_id=str(brand_id), kind=kind)
    return row
