"""Upstash-backed job queue.

Redis holds the work list; Postgres holds the truth. The `jobs` table gives
dedupe (a provider webhook retry must not produce a second creative), retry
counts, and an audit trail you can query when a customer says "it never sent".
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import redis

from app.config import settings
from app.db.models import Job
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger(__name__)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=10
        )
    return _client


def enqueue(
    *,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    scheduled_for: datetime | None = None,
) -> str | None:
    """Returns the job id, or None when deduped away."""
    job_id = str(uuid.uuid4())
    with session_scope() as db:
        if dedupe_key:
            existing = db.query(Job).filter(Job.dedupe_key == dedupe_key).one_or_none()
            if existing is not None:
                log.info("job_deduped", kind=kind, dedupe_key=dedupe_key)
                return None
        db.add(
            Job(
                id=uuid.UUID(job_id),
                kind=kind,
                dedupe_key=dedupe_key,
                payload=payload,
                scheduled_for=scheduled_for,
                status="queued",
            )
        )

    envelope = json.dumps({"job_id": job_id, "kind": kind, "payload": payload})
    try:
        r = get_redis()
        if scheduled_for and scheduled_for > datetime.now(UTC):
            r.zadd(f"{settings.queue_name}:delayed", {envelope: scheduled_for.timestamp()})
        else:
            r.lpush(settings.queue_name, envelope)
    except redis.RedisError as exc:
        # The row is already in Postgres; the reaper below will pick it up.
        log.error("enqueue_redis_failed", error=str(exc), job_id=job_id)
    return job_id


def promote_due_jobs(limit: int = 100) -> int:
    """Move delayed jobs whose time has come onto the main list."""
    r = get_redis()
    now = datetime.now(UTC).timestamp()
    due = r.zrangebyscore(f"{settings.queue_name}:delayed", 0, now, start=0, num=limit)
    moved = 0
    for envelope in due:
        if r.zrem(f"{settings.queue_name}:delayed", envelope):
            r.lpush(settings.queue_name, envelope)
            moved += 1
    return moved


def dequeue(timeout: int = 5) -> dict[str, Any] | None:
    r = get_redis()
    item = r.brpop(settings.queue_name, timeout=timeout)
    if item is None:
        return None
    _, envelope = item
    try:
        return json.loads(envelope)
    except json.JSONDecodeError:
        log.error("job_bad_envelope", envelope=envelope[:200])
        return None
