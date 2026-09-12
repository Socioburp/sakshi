"""Upstash-backed job queue.

Redis holds the work list; Postgres holds the truth. The `jobs` table gives
dedupe (a provider webhook retry must not produce a second creative), retry
counts, and an audit trail you can query when a customer says "it never sent".

Delivery is at-least-once, made safe by the row: the worker checks the job's
status before running it, so a job that is pushed twice (a Redis error after a
successful push, a reaper re-push racing a live worker) runs once. The old
design was at-most-once -- BRPOP removed the envelope and a failing handler
simply lost the message, with the owner's blue tick as the only trace.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Job
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger(__name__)

_client: redis.Redis | None = None

# Retry backoff before attempt 2 and attempt 3 (seconds). The first retry is
# quick because most failures are a blip; the second waits for a vendor to
# recover. MAX_ATTEMPTS bounds the table: raise both together.
BACKOFF = (10, 60)
MAX_ATTEMPTS = 3

# A job "running" longer than this has lost its worker (SIGKILL, OOM, redeploy).
STALE_RUNNING = timedelta(minutes=10)
# A job still "queued" this long after creation was never pushed, or its push
# was lost. Longer than any legitimate delay before a worker picks it up.
STALE_QUEUED = timedelta(seconds=90)


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=10)
    return _client


def _envelope(job_id: str, kind: str, payload: dict[str, Any]) -> str:
    return json.dumps({"job_id": job_id, "kind": kind, "payload": payload})


def _push(envelope: str, at: datetime | None = None) -> bool:
    """Push to the main list, or to the delayed set when `at` is in the future."""
    try:
        r = get_redis()
        if at and at > datetime.now(UTC):
            r.zadd(f"{settings.queue_name}:delayed", {envelope: at.timestamp()})
        else:
            r.lpush(settings.queue_name, envelope)
        return True
    except redis.RedisError as exc:
        # The row is in Postgres as "queued"; reap() re-pushes it.
        log.error("enqueue_redis_failed", error=str(exc))
        return False


def add_job(
    db: Session,
    *,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    scheduled_for: datetime | None = None,
) -> Job | None:
    """Insert the job row inside the CALLER's transaction. Returns None if deduped.

    Used by ingest so the message row and its job commit together: a message
    that exists with no job is unprocessable forever, because the provider's
    retry is (correctly) deduped as a duplicate message.
    """
    if dedupe_key:
        existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is not None:
            log.info("job_deduped", kind=kind, dedupe_key=dedupe_key)
            return None
    job = Job(
        id=uuid.uuid4(),
        kind=kind,
        dedupe_key=dedupe_key,
        payload=payload,
        scheduled_for=scheduled_for,
        status="queued",
    )
    db.add(job)
    db.flush()
    return job


def push_job(
    job_id: uuid.UUID | str,
    kind: str,
    payload: dict[str, Any],
    scheduled_for: datetime | None = None,
) -> bool:
    """Push an already-committed job row. Call AFTER the transaction commits."""
    return _push(_envelope(str(job_id), kind, payload), scheduled_for)


def enqueue(
    *,
    kind: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
    scheduled_for: datetime | None = None,
) -> str | None:
    """Row + push, for callers that do not have a transaction open. Returns the job id."""
    with session_scope() as db:
        job = add_job(
            db, kind=kind, payload=payload, dedupe_key=dedupe_key, scheduled_for=scheduled_for
        )
        if job is None:
            return None
        job_id = str(job.id)
    push_job(job_id, kind, payload, scheduled_for)
    return job_id


def retry_later(job_id: str, kind: str, payload: dict[str, Any], attempt: int) -> datetime:
    """Schedule the next attempt with backoff. Returns when it will run."""
    delay = BACKOFF[min(attempt, len(BACKOFF)) - 1]
    at = datetime.now(UTC) + timedelta(seconds=delay)
    _push(_envelope(job_id, kind, payload), at)
    return at


# ZRANGEBYSCORE + ZREM + LPUSH in one server-side step. The three-call version
# could lose a scheduled post to a crash between the remove and the push.
_PROMOTE = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1], 'LIMIT', 0, ARGV[2])
for _, e in ipairs(due) do
  if redis.call('ZREM', KEYS[1], e) == 1 then
    redis.call('LPUSH', KEYS[2], e)
  end
end
return #due
"""


def promote_due_jobs(limit: int = 100) -> int:
    """Move delayed jobs whose time has come onto the main list, atomically."""
    r = get_redis()
    now = datetime.now(UTC).timestamp()
    try:
        return int(
            r.eval(_PROMOTE, 2, f"{settings.queue_name}:delayed", settings.queue_name, now, limit)
        )
    except redis.RedisError as exc:
        log.error("promote_failed", error=str(exc))
        return 0


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


def reap(now: datetime | None = None) -> dict[str, int]:
    """Recover what a crash or a Redis blip left behind. Runs every ~30s on the worker.

    * queued too long  -> re-push (the push never happened, or was lost)
    * running too long -> the worker died mid-job: retry if attempts remain, else dead
    """
    now = now or datetime.now(UTC)
    counts = {"requeued": 0, "retried": 0, "dead": 0}
    with session_scope() as db:
        stale_queued = db.scalars(
            select(Job)
            .where(
                Job.status == "queued",
                Job.created_at < now - STALE_QUEUED,
                (Job.scheduled_for.is_(None)) | (Job.scheduled_for < now),
            )
            .limit(200)
        ).all()
        for job in stale_queued:
            # Re-pushing is safe: the worker checks the row before running.
            if _push(_envelope(str(job.id), job.kind, job.payload)):
                counts["requeued"] += 1

        stale_running = db.scalars(
            select(Job)
            .where(Job.status == "running", Job.started_at < now - STALE_RUNNING)
            .limit(200)
        ).all()
        for job in stale_running:
            if job.attempts < MAX_ATTEMPTS:
                job.status = "queued"
                job.last_error = "worker lost mid-job"
                if _push(_envelope(str(job.id), job.kind, job.payload)):
                    counts["retried"] += 1
            else:
                job.status, job.finished_at = "dead", now
                job.last_error = "worker lost mid-job; attempts exhausted"
                counts["dead"] += 1
    if any(counts.values()):
        log.warning("jobs_reaped", **counts)
    return counts
