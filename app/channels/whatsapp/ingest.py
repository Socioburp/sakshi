"""Inbound message ingestion.

Deliberately does almost nothing: persist, extend the window, enqueue. The
webhook must return 200 fast or the provider retries and you get duplicate
creatives. All real work happens in the worker.
"""

from __future__ import annotations

import uuid

from app.channels.base import InboundMessage
from app.db import repo
from app.db.session import session_scope
from app.logging import get_logger
from app.queue.client import enqueue

log = get_logger(__name__)

# Audio needs STT before the agent can see it; an image needs looking at.
AUDIO_KINDS = {"audio"}
IMAGE_KINDS = {"image"}

APPROVE_PREFIX = "approve:"


def ingest(msg: InboundMessage) -> uuid.UUID | None:
    """Persist one inbound message. Returns the message id, or None if duplicate."""
    with session_scope() as db:
        account = repo.get_or_create_account(db, msg.wa_id, msg.profile_name)
        sess = repo.touch_session(db, account, msg.wa_id, inbound=True)

        row = repo.record_message(
            db,
            account_id=account.id,
            session_id=sess.id,
            channel="whatsapp",
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            direction="in",
            kind=msg.kind,
            text=msg.text,
            media_url=(msg.media.url if msg.media else None),
            media_mime=(msg.media.mime if msg.media else None),
            media_duration_ms=msg.duration_ms,
            raw=_jsonable(msg.raw),
        )
        if row is None:
            log.info("wa_duplicate_ignored", provider_message_id=msg.provider_message_id)
            return None

        message_id = row.id
        account_id = account.id
        brand = repo.default_brand(db, account.id)

        # Approval is recorded here, from the client's own tap, before the
        # agent runs. The publish tool reads the column; it never takes the
        # agent's word for whether the client agreed.
        if msg.interactive_id and msg.interactive_id.startswith(APPROVE_PREFIX):
            brief_id = msg.interactive_id[len(APPROVE_PREFIX) :]
            approved = repo.mark_approved(db, brief_id=brief_id, via="button")
            log.info("approval_recorded", brief_id=brief_id, slides=approved)

        if msg.kind in AUDIO_KINDS:
            kind = "transcribe_and_handle"
        elif msg.kind in IMAGE_KINDS:
            kind = "handle_image"
        else:
            kind = "handle_message"
        is_logo_candidate = msg.kind in IMAGE_KINDS and not (brand and brand.logo_url)

    # Enqueue outside the transaction so the worker can never see a row that
    # has not committed yet.
    enqueue(
        kind=kind,
        payload={
            "message_id": str(message_id),
            "account_id": str(account_id),
            "media_id": msg.media.id if msg.media else None,
            "media_url": msg.media.url if msg.media else None,
            "media_mime": msg.media.mime if msg.media else None,
            "provider": msg.provider,
            "is_logo_candidate": is_logo_candidate,
        },
        dedupe_key=f"{msg.provider}:{msg.provider_message_id}",
    )
    log.info("wa_ingested", message_id=str(message_id), kind=msg.kind, job=kind)
    return message_id


def record_outbound(
    *,
    account_id: uuid.UUID,
    session_id: uuid.UUID | None,
    provider: str,
    provider_message_id: str | None,
    kind: str,
    text: str | None = None,
    media_url: str | None = None,
) -> None:
    """Mirror an outbound message into the log.

    Idempotent, and never raises: by the time this runs the message is already
    on the owner's phone. Losing the mirror row is a reporting problem; raising
    here would turn it into a failed turn and a retry that sends twice.
    """
    try:
        with session_scope() as db:
            repo.record_message(
                db,
                account_id=account_id,
                session_id=session_id,
                channel="whatsapp",
                provider=provider,
                provider_message_id=provider_message_id,
                direction="out",
                kind=kind,
                text=text,
                media_url=media_url,
                raw={},
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "outbound_mirror_failed",
            provider_message_id=provider_message_id,
            note="message was delivered; only the log row is missing",
        )


def _jsonable(raw) -> dict:
    try:
        import json

        json.dumps(raw)
        return raw if isinstance(raw, dict) else {"value": raw}
    except (TypeError, ValueError):
        return {"unserialisable": str(raw)[:2000]}
