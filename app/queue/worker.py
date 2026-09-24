"""Worker loop. Run as a separate Render service:  python -m app.queue.worker

At-least-once, deduped by the job row: a job runs when its row says "queued".
A failing handler is retried with backoff up to MAX_ATTEMPTS, then marked dead.
Every ~30s the worker also reaps: re-pushes jobs whose push was lost, retries
jobs whose worker died, and fails creatives stuck mid-generation -- refunding
exactly the slides that were charged. Once an hour it queues an Instagram
Insights read for every connected account that has not been read today.
"""

from __future__ import annotations

import asyncio
import signal
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, update

from app.db.models import Job
from app.db.session import session_scope
from app.logging import get_logger
from app.queue.client import (
    DEQUEUE_BLOCK,
    MAX_ATTEMPTS,
    dequeue,
    promote_due_jobs,
    reap,
    retry_later,
)
from app.queue.handlers import HANDLERS

log = get_logger(__name__)

_stop = asyncio.Event()
# Both of these are Redis REQUESTS on an idle queue, and a managed Redis bills
# by the request: promoting once a second is 86,400 calls a day and a five-second
# BRPOP is another 17,280, so an empty queue spent the whole 500,000-request
# allowance in five days and every client's messages stopped being answered.
# Neither number was buying anything. A delayed job is a scheduled post, which
# nobody times to the second, and BRPOP is a BLOCK, not a poll -- it returns the
# instant a job is pushed, so a longer timeout costs a live owner nothing and
# only makes the idle loop cheaper.
PROMOTE_EVERY = 20.0  # seconds; a scheduled post is not a stopwatch
REAP_EVERY = 30.0
SWEEP_EVERY = 3600.0  # Insights: once a day per brand, checked hourly


def _claim(job_id: str) -> int | None:
    """Flip queued -> running and return the attempt number, or None to skip.

    One statement, conditional on the current status: two workers holding
    the same envelope (a reaper re-push racing a live delivery) both try, the
    database lets exactly one through. A read-then-write here let both run
    the job and lost an attempt count.
    """
    with session_scope() as db:
        now = datetime.now(UTC)
        attempts = db.execute(
            update(Job)
            .where(
                Job.id == uuid.UUID(job_id),
                Job.status == "queued",
                # A duplicate envelope (reaper re-push) must not run a job that
                # is waiting on backoff; the delayed copy arrives on time.
                or_(Job.scheduled_for.is_(None), Job.scheduled_for <= now),
            )
            .values(status="running", attempts=Job.attempts + 1, started_at=now)
            .returning(Job.attempts)
        ).scalar_one_or_none()
    if attempts is None:
        log.info("job_skipped", job_id=job_id)
    return attempts


def _finish(
    job_id: str, status: str, error: str | None = None, scheduled_for: datetime | None = None
) -> None:
    with session_scope() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return
        job.status = status
        if status == "queued":
            # Waiting on backoff: the reaper must not treat it as a lost push
            # until this time has passed.
            job.scheduled_for = scheduled_for
        else:
            job.finished_at = datetime.now(UTC)
        if error:
            job.last_error = error[:4000]


# A Redis that is refusing every call does not recover because we ask faster.
# When the request allowance ran out, this loop retried every two seconds --
# tens of thousands of failing calls a day, holding the account at its limit and
# filling the log so the real cause was hard to see. Back off instead, and give
# up the whole worker once it is clearly not a blip, so the platform restarts it
# rather than leaving it spinning.
RETRY_BASE_S = 2
RETRY_MAX_S = 60
_dequeue_failures = 0


async def run_once() -> bool:
    global _dequeue_failures
    try:
        envelope = dequeue(timeout=DEQUEUE_BLOCK)
    except Exception:  # noqa: BLE001 - a Redis blip must not kill the worker
        _dequeue_failures += 1
        log.exception("dequeue_failed", consecutive=_dequeue_failures)
        await asyncio.sleep(min(RETRY_BASE_S * 2 ** (_dequeue_failures - 1), RETRY_MAX_S))
        return False
    _dequeue_failures = 0
    if envelope is None:
        return False

    try:
        job_id, kind, payload = envelope["job_id"], envelope["kind"], envelope["payload"]
    except (KeyError, TypeError):
        log.error("job_bad_envelope", envelope=str(envelope)[:200])
        return True
    handler = HANDLERS.get(kind)
    if handler is None:
        log.error("job_unknown_kind", kind=kind, job_id=job_id)
        try:
            _finish(job_id, "dead", f"no handler for {kind}")
        except Exception:  # noqa: BLE001
            log.exception("job_finish_failed", job_id=job_id)
        return True

    try:
        attempts = _claim(job_id)
    except Exception:  # noqa: BLE001 - a DB blip here must not kill the worker
        log.exception("job_claim_failed", job_id=job_id)
        return True
    if attempts is None:
        return True

    log.info("job_start", kind=kind, job_id=job_id, attempt=attempts)
    try:
        await handler(payload)
    except Exception as exc:  # noqa: BLE001 - the worker must not die
        log.exception("job_failed", kind=kind, job_id=job_id, attempt=attempts)
        try:
            if attempts < MAX_ATTEMPTS:
                at = retry_later(job_id, kind, payload, attempts)
                _finish(job_id, "queued", str(exc), scheduled_for=at)
                log.warning("job_retry_scheduled", job_id=job_id, at=at.isoformat())
            else:
                _finish(job_id, "dead", str(exc))
        except Exception:  # noqa: BLE001 - the row stays "running"; the reaper retries it
            log.exception("job_finish_failed", job_id=job_id)
    else:
        try:
            _finish(job_id, "done")
        except Exception:  # noqa: BLE001
            # The work is done; if this write fails the reaper will re-run
            # the job after STALE_RUNNING. Re-runs are cheap by design: the
            # runner skips an answered message, the STT handler keeps an
            # existing transcript, the image handler keeps an existing asset.
            log.exception("job_finish_failed", job_id=job_id)
        log.info("job_done", kind=kind, job_id=job_id)
    return True


def _reap_creatives() -> int:
    """Creatives stuck mid-generation after a crash: fail them, refund what was billed."""
    from app.creative.pipeline import reap_stuck_creatives

    return reap_stuck_creatives()


def _sweep_insights() -> int:
    from app.insights.performance import sweep

    return sweep()


async def housekeeping(
    last_promote: float, last_reap: float, last_sweep: float = 0.0
) -> tuple[float, float, float]:
    now = time.monotonic()
    if now - last_promote >= PROMOTE_EVERY:
        promote_due_jobs()
        last_promote = now
    if now - last_reap >= REAP_EVERY:
        try:
            reap()
            _reap_creatives()
        except Exception:  # noqa: BLE001
            log.exception("reap_failed")
        last_reap = now
    if now - last_sweep >= SWEEP_EVERY:
        try:
            _sweep_insights()
        except Exception:  # noqa: BLE001
            log.exception("insights_sweep_failed")
        try:
            from app.insights import festival_push

            festival_push.sweep()
        except Exception:  # noqa: BLE001
            log.exception("festival_sweep_failed")
        last_sweep = now
    return last_promote, last_reap, last_sweep


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop.set)
    log.info("worker_started", handlers=sorted(HANDLERS))
    last_promote = last_reap = last_sweep = 0.0
    while not _stop.is_set():
        last_promote, last_reap, last_sweep = await housekeeping(
            last_promote, last_reap, last_sweep
        )
        did_work = await run_once()
        if not did_work:
            await asyncio.sleep(0.2)
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
