"""Outbound helpers. Every send goes through here so the 24h window is
checked once, in one place, and every send is mirrored into `messages`."""

from __future__ import annotations

import uuid

from app.channels.base import Button, OutboundMessage
from app.channels.whatsapp.adapters import get_adapter
from app.channels.whatsapp.ingest import record_outbound
from app.channels.whatsapp.session_window import is_open
from app.db.models import WaSession
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger(__name__)


def _window_open(session_id: uuid.UUID | None) -> bool:
    if session_id is None:
        return True
    with session_scope() as db:
        return is_open(db.get(WaSession, session_id))


async def send_text(
    *, account_id: uuid.UUID, session_id: uuid.UUID | None, wa_id: str, text: str,
    buttons: list[Button] | None = None,
) -> bool:
    if not _window_open(session_id):
        log.warning("send_suppressed_window_closed", wa_id=wa_id)
        return False
    adapter = get_adapter()
    msg = OutboundMessage(
        to=wa_id,
        kind="buttons" if buttons else "text",
        text=text,
        buttons=buttons or [],
    )
    res = await adapter.send(msg)
    record_outbound(
        account_id=account_id,
        session_id=session_id,
        provider=adapter.name,
        provider_message_id=res.provider_message_id,
        kind="interactive" if buttons else "text",
        text=text,
    )
    return res.ok


async def send_image(
    *, account_id: uuid.UUID, session_id: uuid.UUID | None, wa_id: str,
    image_url: str, caption: str = "",
) -> bool:
    if not _window_open(session_id):
        log.warning("send_suppressed_window_closed", wa_id=wa_id)
        return False
    adapter = get_adapter()
    res = await adapter.send(
        OutboundMessage(to=wa_id, kind="image", image_url=image_url, caption=caption)
    )
    record_outbound(
        account_id=account_id,
        session_id=session_id,
        provider=adapter.name,
        provider_message_id=res.provider_message_id,
        kind="image",
        text=caption,
        media_url=image_url,
    )
    return res.ok
