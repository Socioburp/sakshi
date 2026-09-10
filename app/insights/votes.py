"""A tap is a vote. Record it, and teach the memory lanes what it meant.

"Post it" is the strongest signal the product ever gets about a picture: the
owner is putting their name on it. It becomes a `style_anchor` memory, which
grounding already retrieves for the next brief. "Change the picture" becomes
a `rejection` memory -- the lane with the loosest retrieval bar, because a
"no" is the most expensive thing to get wrong twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Brief
from app.insights.events import facts_of, record
from app.logging import get_logger

log = get_logger(__name__)


def _brief(db: Session, brief_id: str, account_id: uuid.UUID | None = None) -> Brief | None:
    try:
        b = db.get(Brief, uuid.UUID(str(brief_id)))
    except ValueError:
        return None
    if b is None or (account_id is not None and b.account_id != account_id):
        return None
    return b


def _remember(db: Session, brief: Brief, kind: str, content: str) -> None:
    try:
        from app.memory import embed

        embed.remember(
            db, brand_id=brief.brand_id, kind=kind, content=content, source_ref=f"brief:{brief.id}"
        )
    except Exception:  # noqa: BLE001 - memory is an enhancement, never a gate
        log.warning("vote_memory_failed", kind=kind, brief_id=str(brief.id))


def _describe(brief: Brief) -> str:
    p = brief.payload or {}
    vd = p.get("visual_direction") or {}
    return (
        f"headline '{p.get('headline', '')}'; picture: {vd.get('prompt', '')[:160]}; "
        f"mood: {vd.get('mood') or '-'}; layout: {p.get('template_id') or 'centered_overlay'}"
    )


def approve(db: Session, brief_id: str, *, remember: bool = True) -> None:
    """Record the vote; write the memory too unless the caller defers it.

    The webhook passes `remember=False`: the embedding is a network call to
    another vendor, and the webhook's transaction must not wait on it. The
    worker then calls `remember_tap` before the turn runs.
    """
    b = _brief(db, brief_id)
    if b is None:
        return
    record(
        db,
        kind="approve",
        account_id=b.account_id,
        brand_id=b.brand_id,
        brief_id=b.id,
        meta=facts_of(b.payload or {}),
    )
    if remember:
        _remember(db, b, "style_anchor", _approval_text(b))


def _approval_text(b: Brief) -> str:
    return f"Owner approved this creative: {_describe(b)}"


def change_words(db: Session, brief_id: str, account_id: uuid.UUID) -> None:
    b = _brief(db, brief_id, account_id)
    if b is None:
        return
    record(
        db,
        kind="change_words",
        account_id=b.account_id,
        brand_id=b.brand_id,
        brief_id=b.id,
        meta=facts_of(b.payload or {}),
    )


def change_picture(
    db: Session, brief_id: str, account_id: uuid.UUID, *, remember: bool = True
) -> None:
    b = _brief(db, brief_id, account_id)
    if b is None:
        return
    record(
        db,
        kind="change_picture",
        account_id=b.account_id,
        brand_id=b.brand_id,
        brief_id=b.id,
        meta=facts_of(b.payload or {}),
    )
    if remember:
        _remember(db, b, "rejection", _rejection_text(b))


def _rejection_text(b: Brief) -> str:
    vd = (b.payload or {}).get("visual_direction") or {}
    return (
        f"Owner rejected the picture (asked for a different one): {vd.get('prompt', '')[:200]}"
        + (f"; mood {vd.get('mood')}" if vd.get("mood") else "")
    )


def remember_tap(db: Session, interactive_id: str, account_id: uuid.UUID) -> None:
    """The memory half of a tap, run by the worker.

    approve -> style anchor, redo -> rejection. The vote itself was recorded
    by the webhook; only the embedding (a call to another vendor) is deferred
    here so the webhook never waits on it.
    """
    if interactive_id.startswith("approve:"):
        b = _brief(db, interactive_id[len("approve:") :], account_id)
        if b is not None:
            _remember(db, b, "style_anchor", _approval_text(b))
    elif interactive_id.startswith("redo:"):
        b = _brief(db, interactive_id[len("redo:") :], account_id)
        if b is not None:
            _remember(db, b, "rejection", _rejection_text(b))


def snooze_nudge(db: Session, brand: Any, days: int = 3) -> None:
    """ "Not today" is not "never": no daily idea for a few days, then it resumes."""
    until = (datetime.now(UTC) + timedelta(days=days)).isoformat()
    brand.template_prefs = {**(brand.template_prefs or {}), "nudge_snooze_until": until}
    db.flush()


def nudge_snoozed(brand: Any, now: datetime | None = None) -> bool:
    raw = (getattr(brand, "template_prefs", None) or {}).get("nudge_snooze_until")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > (now or datetime.now(UTC))
