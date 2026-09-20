#!/usr/bin/env python3
"""Repair brand_memory rows that were written without a Voyage key.

Why these rows exist
--------------------
`app/memory/embed.py` used to fall back to a zero vector whenever
VOYAGE_API_KEY was unset, guarded only by `settings.is_prod`. So on staging,
and on any deploy whose key had not been set yet, every approval, rejection
and product note was stored with an all-zeros embedding and logged as
`brand_memory_written`.

Those rows are not merely low quality, they are unreachable. pgvector's
cosine distance to a zero vector is undefined (NaN), so the row can never
rank, can never be filtered in, and can never be read back -- no matter how
well the key is configured afterwards. The content is intact; only the
vector is dead. That is why the bot appeared to forget a brand it had been
told about repeatedly.

The write path is fixed (a keyless write now raises in every environment),
but the rows already in the database stay dead until something re-embeds
them. That is this script.

Usage
-----
    VOYAGE_API_KEY=... python scripts/backfill_embeddings.py --dry-run
    VOYAGE_API_KEY=... python scripts/backfill_embeddings.py
    VOYAGE_API_KEY=... python scripts/backfill_embeddings.py --brand <uuid>

Safe to run repeatedly: it only touches rows whose vector is missing or
all-zeros, so a second run after a partial failure resumes where it stopped.
Content is never modified, and nothing is deleted.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import select

from app.config import settings
from app.db.models import BrandMemory
from app.db.session import session_scope
from app.memory.embed import embed_texts

# Voyage takes a list; batching keeps the round trips down without building a
# request big enough to time out. 64 short notes is comfortably inside their
# per-request token ceiling.
BATCH = 64


def is_dead(vector) -> bool:
    """A vector that can never be retrieved: absent, wrong width, or no direction."""
    if vector is None:
        return True
    vals = list(vector)
    if len(vals) != settings.embed_dim:
        return True
    return not any(vals)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--brand", help="limit to one brand id")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    args = ap.parse_args()

    if not settings.voyage_api_key and not args.dry_run:
        print("VOYAGE_API_KEY is unset. Set it, or pass --dry-run.", file=sys.stderr)
        return 2

    with session_scope() as db:
        stmt = select(BrandMemory).order_by(BrandMemory.created_at)
        if args.brand:
            stmt = stmt.where(BrandMemory.brand_id == uuid.UUID(args.brand))
        rows = list(db.execute(stmt).scalars())

    dead = [r for r in rows if is_dead(r.embedding)]
    if args.limit:
        dead = dead[: args.limit]

    by_kind: dict[str, int] = {}
    brands: set[str] = set()
    for r in dead:
        by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        brands.add(str(r.brand_id))

    print(f"{len(rows)} rows scanned, {len(dead)} unretrievable across {len(brands)} brand(s)")
    for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<14} {n}")
    if not dead:
        return 0
    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    repaired = failed = 0
    for i in range(0, len(dead), BATCH):
        chunk = dead[i : i + BATCH]
        try:
            # input_type="document" -- these are stored notes being indexed,
            # not questions. Embedding them as queries would put them in a
            # different part of the space from every future write.
            vectors = embed_texts([r.content for r in chunk], input_type="document")
        except Exception as exc:  # noqa: BLE001
            print(f"  batch at {i} failed: {exc!r}", file=sys.stderr)
            failed += len(chunk)
            continue
        # Re-fetched and committed per batch so an interrupted run keeps the
        # work it already paid Voyage for.
        with session_scope() as db:
            for row, vec in zip(chunk, vectors, strict=True):
                if not any(vec):
                    failed += 1
                    continue
                fresh = db.get(BrandMemory, row.id)
                if fresh is None:
                    continue
                fresh.embedding = vec
                repaired += 1
        print(f"  {min(i + BATCH, len(dead))}/{len(dead)}")

    print(f"\nrepaired {repaired}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
