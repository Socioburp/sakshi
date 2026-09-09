"""WhatsApp webhook. Verification handshake + inbound fan-in."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Request, Response, status

from app.channels.whatsapp.adapters import get_adapter
from app.channels.whatsapp.ingest import ingest
from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])


@router.get("")
async def verify(request: Request) -> Response:
    """Meta's subscription handshake: echo hub.challenge as plain text."""
    adapter = get_adapter()
    challenge = adapter.verify_webhook(dict(request.query_params))
    if challenge is None:
        log.warning("wa_verify_rejected", params=dict(request.query_params))
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    return Response(content=challenge, media_type="text/plain")


@router.post("")
async def receive(request: Request, background: BackgroundTasks) -> Response:
    adapter = get_adapter()
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    content_type = headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        body = {k: v for k, v in form.items()}
        # Twilio signs url + sorted params; hand the adapter the signing base.
        headers["x-sakshi-signing-base"] = str(request.url) + "".join(
            f"{k}{body[k]}" for k in sorted(body)
        )
    else:
        try:
            body = await request.json()
        except ValueError:
            log.warning("wa_bad_body", size=len(raw))
            return Response(status_code=status.HTTP_200_OK)

    if not adapter.verify_signature(raw, headers):
        log.warning("wa_bad_signature", provider=adapter.name)
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        messages = adapter.parse(body, headers)
    except Exception:
        log.exception("wa_parse_failed")
        # 200 anyway: a parse bug must not make the provider retry forever.
        return Response(status_code=status.HTTP_200_OK)

    for msg in messages:
        background.add_task(ingest, msg)

    return Response(status_code=status.HTTP_200_OK)


@router.get("/debug/verify-url", include_in_schema=False)
async def debug_verify_url() -> dict:
    """Convenience: the exact callback URL and token to paste into the provider."""
    if settings.is_prod:
        return {}
    return {
        "callback_url": f"{settings.public_base_url}/webhooks/whatsapp",
        "verify_token": settings.wa_verify_token,
        "self_test": (
            f"{settings.public_base_url}/webhooks/whatsapp"
            f"?hub.mode=subscribe&hub.verify_token={quote(settings.wa_verify_token)}"
            f"&hub.challenge=ping"
        ),
    }
