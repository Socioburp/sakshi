"""Worker loop. Run as a separate Render service:  python -m app.queue.worker"""

from __future__ import annotations

import asyncio
import signal
import uuid
from datetime import UTC, datetime

from app.db.models import Job
from app.db.session import session_scope
from app.logging import get_logger
from app.queue.client import dequeue, promote_due_jobs
from app.queue.handlers import HANDLERS

log = get_logger(__name__)

MAX_ATTEMPTS = 3
_stop = asyncio.Event()


def _mark(job_id: str, status: str, error: str | None = None) -> int:
    with session_scope() as db:
        job = db.get(Job, uuid.UUID(job_id))
        if job is None:
            return 0
        job.status = status
        if status == "running":
            job.attempts += 1
            job.started_at = datetime.now(UTC)
        else:
            job.finished_at = datetime.now(UTC)
        if error:
            job.last_error = error[:4000]
        return job.attempts


async def run_once() -> bool:
    envelope = dequeue(timeout=5)
    if envelope is None:
        promote_due_jobs()
        return False

    job_id, kind, payload = envelope["job_id"], envelope["kind"], envelope["payload"]
    handler = HANDLERS.get(kind)
    if handler is None:
        log.error("job_unknown_kind", kind=kind, job_id=job_id)
        _mark(job_id, "dead", f"no handler for {kind}")
        return True

    attempts = _mark(job_id, "running")
    log.info("job_start", kind=kind, job_id=job_id, attempt=attempts)
    try:
        await handler(payload)
    except Exception as exc:  # noqa: BLE001 - the worker must not die
        log.exception("job_failed", kind=kind, job_id=job_id)
        _mark(job_id, "failed" if attempts < MAX_ATTEMPTS else "dead", str(exc))
    else:
        _mark(job_id, "done")
        log.info("job_done", kind=kind, job_id=job_id)
    return True


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop.set)
    log.info("worker_started", handlers=sorted(HANDLERS))
    while not _stop.is_set():
        did_work = await run_once()
        if not did_work:
            await asyncio.sleep(0.2)
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
