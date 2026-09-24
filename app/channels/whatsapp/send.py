"""Outbound helpers. Every send goes through here so the 24h window is
checked once, in one place, and every send is mirrored into `messages`."""

from __future__ import annotations

import uuid

from app.channels.base import Button, OutboundMessage
from app.channels.whatsapp.adapters import get_adapter
from app.channels.whatsapp.ingest import record_outbound
from app.channels.whatsapp.session_window import is_open
from app.db import repo
from app.db.models import WaSession
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger(__name__)


def _window_open(
    session_id: uuid.UUID | None, wa_id: str = "", account_id: uuid.UUID | None = None
) -> bool:
    """Meta's 24h rule, decided from the session -- never assumed open.

    A caller with no session id (a scheduled publish hours later, a message
    whose session was deleted) used to be waved through, which is precisely
    the case most likely to be outside the window. Look up the live session
    for the number instead, and treat "no session" as closed. A session id
    that points at a closed window also falls back to the number's newest
    session: the owner may have written again since.
    """
    with session_scope() as db:
        sess = db.get(WaSession, session_id) if session_id else None
        if (sess is None or not is_open(sess)) and wa_id:
            newer = repo.latest_session(db, wa_id, account_id=account_id)
            sess = newer or sess
        return is_open(sess) if sess is not None else False


async def send_text(
    *,
    account_id: uuid.UUID,
    session_id: uuid.UUID | None,
    wa_id: str,
    text: str,
    buttons: list[Button] | None = None,
) -> bool:
    if not _window_open(session_id, wa_id, account_id):
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


async def send_template(
    *,
    account_id: uuid.UUID,
    wa_id: str,
    name: str,
    lang: str,
    params: list[str],
    button_ids: list[str],
    rendered: str,
) -> bool:
    """A pre-approved template: the ONE send that does not need the 24h window.

    It is also the one send that costs money (a marketing conversation), so it
    is never used when a free in-window message would do -- callers check the
    window first. An adapter that cannot send templates answers False; nothing
    is ever downgraded to a free-form message outside the window, because Meta
    would reject it and count it against the number's quality rating.
    """
    adapter = get_adapter()
    msg = OutboundMessage(
        to=wa_id,
        kind="template",
        text=rendered,
        template_name=name,
        template_lang=lang,
        template_params=list(params),
        buttons=[Button(id=b, title="") for b in button_ids],
    )
    try:
        res = await adapter.send(msg)
    except ValueError:
        log.warning("template_unsupported_by_adapter", provider=adapter.name)
        return False
    record_outbound(
        account_id=account_id,
        session_id=None,
        provider=adapter.name,
        provider_message_id=res.provider_message_id,
        kind="interactive",
        text=rendered,
    )
    return res.ok


async def send_video(
    *,
    account_id: uuid.UUID,
    session_id: uuid.UUID | None,
    wa_id: str,
    video_url: str,
    caption: str = "",
) -> bool:
    """A reel, as a WhatsApp video: playable in the chat, forwardable to Status."""
    if not _window_open(session_id, wa_id, account_id):
        log.warning("send_suppressed_window_closed", wa_id=wa_id)
        return False
    adapter = get_adapter()
    res = await adapter.send(
        OutboundMessage(to=wa_id, kind="video", video_url=video_url, caption=caption)
    )
    record_outbound(
        account_id=account_id,
        session_id=session_id,
        provider=adapter.name,
        provider_message_id=res.provider_message_id,
        kind="video",
        text=caption,
        media_url=video_url,
    )
    return res.ok


async def send_image(
    *,
    account_id: uuid.UUID,
    session_id: uuid.UUID | None,
    wa_id: str,
    image_url: str,
    caption: str = "",
) -> bool:
    if not _window_open(session_id, wa_id, account_id):
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
