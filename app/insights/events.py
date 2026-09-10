"""Record what the owner did with a creative. One row per vote."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Brief, CreativeEvent
from app.logging import get_logger

log = get_logger(__name__)

# What the events mean, so the rest of the package does not re-derive it. A
# tap and the tool call that follows it ("change the picture" -> regenerate)
# are ONE opinion; profile.taste() counts at most one of each per brief.
LIKES_PICTURE = {"approve", "publish", "change_words"}  # words changed = picture kept
DISLIKES_PICTURE = {"change_picture", "regenerate"}
LIKES_WORDS = {"approve", "publish", "change_picture"}
DISLIKES_WORDS = {"change_words", "revise"}


def record(
    db: Session,
    *,
    kind: str,
    account_id: uuid.UUID,
    brand_id: uuid.UUID,
    brief_id: uuid.UUID | str | None = None,
    creative_id: uuid.UUID | str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Never raises, and never poisons the caller's transaction.

    A failed INSERT aborts a Postgres transaction; swallowing the error alone
    would leave the caller's next flush raising PendingRollbackError and lose
    the creative it was recording a vote about. The savepoint contains it.
    """
    try:
        with db.begin_nested():
            db.add(
                CreativeEvent(
                    account_id=account_id,
                    brand_id=brand_id,
                    brief_id=uuid.UUID(str(brief_id)) if brief_id else None,
                    creative_id=uuid.UUID(str(creative_id)) if creative_id else None,
                    kind=kind,
                    meta=meta or {},
                )
            )
            db.flush()
    except Exception:  # noqa: BLE001
        log.exception("creative_event_failed", kind=kind)


def facts_of(brief_payload: dict[str, Any]) -> dict[str, Any]:
    """The properties of a brief worth remembering as context for a vote."""
    fmt = brief_payload.get("format") or {}
    vd = brief_payload.get("visual_direction") or {}
    slides = brief_payload.get("slides") or []
    return {
        "template": brief_payload.get("template_id") or "centered_overlay",
        "aspect": fmt.get("aspect_ratio", "1:1"),
        "format": fmt.get("type", "single"),
        "slides": len(slides) or 1,
        "intent": brief_payload.get("intent"),
        "mood": (vd.get("mood") or "")[:80],
        "headline_len": len(brief_payload.get("headline") or ""),
        "has_photo": bool(vd.get("reference_asset_id"))
        or any((s.get("visual_direction") or {}).get("reference_asset_id") for s in slides),
    }


def record_for_brief(
    db: Session, *, kind: str, brief_id: uuid.UUID | str, meta: dict[str, Any] | None = None
) -> None:
    """Record a vote when only the brief id is known (a button tap)."""
    brief = db.get(Brief, uuid.UUID(str(brief_id)))
    if brief is None:
        return
    record(
        db,
        kind=kind,
        account_id=brief.account_id,
        brand_id=brief.brand_id,
        brief_id=brief.id,
        meta={**facts_of(brief.payload or {}), **(meta or {})},
    )
