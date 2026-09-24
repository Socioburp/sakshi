"""Instagram webhook endpoint: comments and DMs in, one job each.

Thin, like the WhatsApp webhook: verify, persist, enqueue, return 200. Meta
retries on anything but a fast 200, so parsing failures and unknown shapes are
swallowed after logging rather than surfaced as errors.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.db.session import session_scope
from app.inbox import service
from app.integrations.instagram import webhook
from app.logging import get_logger
from app.queue.client import add_job, push_job

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks/instagram", tags=["instagram"])


@router.get("")
async def verify(request: Request) -> Response:
    challenge = webhook.verify_challenge(dict(request.query_params))
    if challenge is None:
        log.warning("ig_verify_rejected", params=dict(request.query_params))
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    return Response(content=challenge, media_type="text/plain")


@router.post("")
async def receive(request: Request) -> Response:
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not webhook.verify_signature(raw, headers):
        log.warning("ig_bad_signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        body = await request.json()
    except ValueError:
        log.warning("ig_bad_body", size=len(raw))
        return Response(status_code=status.HTTP_200_OK)
    try:
        events = webhook.parse(body)
    except Exception:  # noqa: BLE001 - a parse bug must not make Meta retry forever
        log.exception("ig_parse_failed")
        return Response(status_code=status.HTTP_200_OK)

    for inbound in events:
        # Persist the event and enqueue its notify job in one transaction, so a
        # row without a job (unprocessable forever) can never exist.
        try:
            with session_scope() as db:
                event_id = service.record(db, inbound)
                if event_id is None:
                    continue
                payload = {"event_id": str(event_id)}
                job = add_job(
                    db,
                    kind="ig_event_notify",
                    payload=payload,
                    dedupe_key=f"ig_notify:{event_id}",
                )
                job_id = job.id if job else None
            if job_id is not None:
                push_job(job_id, "ig_event_notify", payload)
        except Exception:  # noqa: BLE001 - one bad event must not drop the batch
            log.exception("ig_event_record_failed", kind=inbound.kind, obj=inbound.ig_object_id)

    return Response(status_code=status.HTTP_200_OK)
