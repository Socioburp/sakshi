"""The engagement inbox: a comment or DM comes in, a reply goes out with
the owner's tap.

The owner never has to open Instagram to keep up with their comments. Sakshi
drafts a reply in their brand's voice, sends it to them on WhatsApp with three
buttons -- Send, Edit, Skip -- and posts it only when they tap Send (or the
edit they typed). Nothing is auto-posted; the owner's tap is the gate, exactly
like publishing a creative.

This module is the DB-and-orchestration half. The webhook parser
(`integrations/instagram/webhook.py`) turns Meta's payload into an `IgInbound`;
the queue handlers call `draft` and the posting helpers.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Brand, IgAccount, IgEvent
from app.integrations.instagram.webhook import IgInbound
from app.logging import get_logger

log = get_logger(__name__)

# A DM thread is a conversation; a comment is a one-off. We reply to both, but
# only nudge on the first message of a quiet thread is a later refinement --
# for now every comment and every non-echo message becomes one event.


def route(db: Session, recipient_ig_id: str) -> IgAccount | None:
    """Which connected account (and brand) does this event belong to."""
    if not recipient_ig_id:
        return None
    return db.scalar(
        select(IgAccount)
        .where(IgAccount.ig_user_id == recipient_ig_id, IgAccount.status == "connected")
        .limit(1)
    )


def record(db: Session, inbound: IgInbound) -> uuid.UUID | None:
    """Persist one inbound event as 'new'. Returns its id, or None when skipped.

    Skipped: an echo of the account's own message, the account commenting on
    its own post, a body with no text, or a duplicate (the unique key catches
    a webhook retry). None means "do not notify".
    """
    if inbound.is_echo:
        return None
    ig = route(db, inbound.recipient_ig_id)
    if ig is None:
        log.info("ig_event_unrouted", recipient=inbound.recipient_ig_id, kind=inbound.kind)
        return None
    if inbound.from_id and inbound.from_id == ig.ig_user_id:
        return None  # our own reply coming back as a comment event
    if not (inbound.text or "").strip():
        return None  # a sticker/like with no text: nothing to reply to
    existing = db.scalar(
        select(IgEvent).where(
            IgEvent.brand_id == ig.brand_id,
            IgEvent.kind == inbound.kind,
            IgEvent.ig_object_id == inbound.ig_object_id,
        )
    )
    if existing is not None:
        return None
    row = IgEvent(
        brand_id=ig.brand_id,
        ig_account_id=ig.id,
        kind=inbound.kind,
        ig_object_id=inbound.ig_object_id,
        parent_id=inbound.parent_id,
        media_id=inbound.media_id,
        from_id=inbound.from_id,
        from_username=inbound.from_username,
        text=(inbound.text or "")[:2000],
        status="new",
        meta={"raw_kind": inbound.kind},
    )
    db.add(row)
    db.flush()
    return row.id


# --------------------------------------------------------------------------- #
# drafting
# --------------------------------------------------------------------------- #
_FALLBACK = {
    "comment": {
        "en": "Thank you! 🙏 Please DM us or WhatsApp for details.",
        "hi": "Dhanyavaad! 🙏 Details ke liye DM ya WhatsApp karein.",
    },
    "message": {
        "en": "Thanks for reaching out! How can we help?",
        "hi": "Sampark karne ke liye dhanyavaad! Hum kaise madad karein?",
    },
}


def _fallback(kind: str, lang: str) -> str:
    table = _FALLBACK.get("comment" if kind in ("comment", "mention") else "message")
    return table.get(lang, table["en"])


async def draft(*, kind: str, text: str, brand: Brand, facts: str, lang: str) -> str:
    """A short reply in the brand's voice. The model when configured, a plain
    line otherwise. Never longer than a couple of sentences -- a reply is not
    a pitch, and the owner approves it before it goes out."""
    if not settings.anthropic_api_key or not settings.anthropic_model:
        return _fallback(kind, lang)
    surface = "a public comment" if kind in ("comment", "mention") else "a direct message"
    tone = (brand.tone or "warm, friendly").strip()
    known = f"\nWhat we know (use verbatim, never invent): {facts}" if facts else ""
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=160,
            system=(
                f"You are the owner of {brand.name}, replying to {surface} on Instagram. "
                f"Write ONE short reply (max 30 words) in this tone: {tone}. Language of this "
                f"code, Latin script for hi/mr/kn/ta/te/ml (Hinglish-style): {lang}. Be warm and "
                "helpful. Do not invent prices, offers, stock, delivery or claims -- if you do "
                "not know, invite them to DM or WhatsApp. No hashtags, no greeting boilerplate, "
                "no quotes around the reply." + known
            ),
            messages=[{"role": "user", "content": f"They wrote: {text}"}],
        )
        out = "".join(b.text for b in resp.content if b.type == "text").strip()
        out = " ".join(out.split())
        return out if 2 <= len(out) <= 280 else _fallback(kind, lang)
    except Exception:  # noqa: BLE001 - a plain reply is a fine reply
        log.warning("ig_reply_draft_failed", brand_id=str(brand.id))
        return _fallback(kind, lang)


def facts_for(db: Session, brand_id: uuid.UUID, query: str) -> str:
    """A line of remembered product facts to ground the reply (prices, names)."""
    try:
        from app.memory import retrieve

        hits = retrieve.search(db, brand_id=brand_id, query=query, kinds=("fact", "product"), k=3)
        joined = "; ".join(m.content.strip() for m, _ in hits if m.content)
        return joined[:400]
    except Exception:  # noqa: BLE001 - grounding is a bonus, never a gate
        return ""


# --------------------------------------------------------------------------- #
# the owner-facing message
# --------------------------------------------------------------------------- #
_HEADER = {
    "comment": {"en": "💬 New comment", "hi": "💬 Naya comment"},
    "mention": {"en": "💬 You were mentioned", "hi": "💬 Aapko mention kiya"},
    "message": {"en": "✉️ New message", "hi": "✉️ Naya message"},
}
_SUGGEST = {"en": "Suggested reply", "hi": "Suggested reply"}


def owner_message(
    *, kind: str, from_username: str | None, text: str, draft_text: str, lang: str
) -> str:
    header = _HEADER.get(kind, _HEADER["comment"])
    who = f" from @{from_username}" if from_username else ""
    head = header.get(lang, header["en"])
    suggest = _SUGGEST.get(lang, _SUGGEST["en"])
    snippet = (text or "").strip()
    if len(snippet) > 240:
        snippet = snippet[:237] + "..."
    return f'{head}{who}:\n"{snippet}"\n\n{suggest}:\n"{draft_text}"'


def as_public_dict(row: IgEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "from": row.from_username or row.from_id,
        "text": row.text,
        "draft": row.draft_reply,
        "status": row.status,
    }


__all__ = ["as_public_dict", "draft", "facts_for", "owner_message", "record", "route"]
