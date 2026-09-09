"""Thin data-access helpers. Keeps SQL out of the agent and channel layers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Account, Brand, Brief, Creative, Message, WaSession

WA_WINDOW = timedelta(hours=24)


def now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
def get_or_create_account(db: Session, wa_phone: str, display_name: str | None = None) -> Account:
    acct = db.scalar(select(Account).where(Account.wa_phone == wa_phone))
    if acct:
        if display_name and not acct.display_name:
            acct.display_name = display_name
            db.flush()
        return acct
    acct = Account(
        wa_phone=wa_phone,
        display_name=display_name,
        credits_balance=settings.free_trial_credits,
    )
    db.add(acct)
    db.flush()
    db.add(
        Brand(
            account_id=acct.id,
            name=display_name or "My brand",
            languages=["en", "hi"],
            palette={},
            fonts={},
        )
    )
    db.flush()
    return acct


def default_brand(db: Session, account_id: uuid.UUID) -> Brand | None:
    return db.scalar(
        select(Brand)
        .where(Brand.account_id == account_id)
        .order_by(Brand.is_default.desc(), Brand.created_at.asc())
        .limit(1)
    )


# --------------------------------------------------------------------------- #
def touch_session(db: Session, account: Account, wa_id: str, inbound: bool) -> WaSession:
    """Open or extend the 24h window. One live session per (account, wa_id)."""
    sess = db.scalar(
        select(WaSession)
        .where(WaSession.account_id == account.id, WaSession.wa_id == wa_id)
        .where(WaSession.closed_at.is_(None))
        .order_by(WaSession.created_at.desc())
        .limit(1)
    )
    ts = now()
    if sess and sess.window_expires_at and sess.window_expires_at < ts:
        sess.closed_at = ts
        db.flush()
        sess = None
    if sess is None:
        sess = WaSession(account_id=account.id, wa_id=wa_id, state={})
        db.add(sess)
    if inbound:
        sess.last_inbound_at = ts
        sess.window_expires_at = ts + WA_WINDOW
    else:
        sess.last_outbound_at = ts
    db.flush()
    return sess


def latest_session(
    db: Session, wa_id: str, account_id: uuid.UUID | None = None
) -> WaSession | None:
    """The most recent live session for a number, scoped to the account when known."""
    stmt = select(WaSession).where(WaSession.wa_id == wa_id, WaSession.closed_at.is_(None))
    if account_id is not None:
        stmt = stmt.where(WaSession.account_id == account_id)
    return db.scalar(stmt.order_by(WaSession.created_at.desc()).limit(1))


def window_is_open(sess: WaSession) -> bool:
    return bool(sess.window_expires_at and sess.window_expires_at > now())


# --------------------------------------------------------------------------- #
def record_message(db: Session, **kwargs) -> Message | None:
    """Idempotent insert keyed on (provider, provider_message_id).

    Returns None when the row already existed -- providers retry webhooks
    aggressively and a duplicate must not trigger a second creative.
    """
    provider_message_id = kwargs.get("provider_message_id")
    if provider_message_id:
        stmt = (
            insert(Message)
            .values(id=uuid.uuid4(), **kwargs)
            .on_conflict_do_nothing(index_elements=["provider", "provider_message_id"])
            .returning(Message.id)
        )
        new_id = db.execute(stmt).scalar()
        if new_id is None:
            return None
        db.flush()
        return db.get(Message, new_id)
    msg = Message(**kwargs)
    db.add(msg)
    db.flush()
    return msg


def recent_messages(db: Session, session_id: uuid.UUID, limit: int = 20) -> list[Message]:
    rows = db.scalars(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


# --------------------------------------------------------------------------- #
def save_brief(
    db: Session,
    *,
    account_id: uuid.UUID,
    brand_id: uuid.UUID,
    payload: dict,
    source_message_id: uuid.UUID | None = None,
    parent: Brief | None = None,
) -> Brief:
    if parent is not None:
        parent.status = "superseded"
    brief = Brief(
        account_id=account_id,
        brand_id=brand_id,
        payload=payload,
        source_message_id=source_message_id,
        parent_brief_id=parent.id if parent else None,
        version=(parent.version + 1) if parent else 1,
    )
    db.add(brief)
    db.flush()
    return brief


def latest_creative_for_brief(db: Session, brief_id: uuid.UUID) -> Creative | None:
    return db.scalar(
        select(Creative)
        .where(Creative.brief_id == brief_id)
        .order_by(Creative.created_at.desc())
        .limit(1)
    )


def mark_approved(db: Session, *, brief_id: str, via: str) -> int:
    """Stamp every slide of a brief as approved. Returns how many were stamped."""
    try:
        bid = uuid.UUID(brief_id)
    except (ValueError, AttributeError):
        return 0
    rows = creatives_for_brief(db, bid)
    stamped = 0
    for row in rows:
        if row.approved_at is None and row.status in ("ready", "approved"):
            row.approved_at = now()
            row.approved_via = via
            row.status = "approved"
            stamped += 1
    db.flush()
    return stamped


def approval_state(db: Session, brief_id: uuid.UUID) -> tuple[int, int]:
    """(approved, total) for a brief's slides."""
    rows = creatives_for_brief(db, brief_id)
    return sum(1 for r in rows if r.approved_at is not None), len(rows)


def creatives_for_brief(db: Session, brief_id: uuid.UUID) -> list[Creative]:
    """Every slide of a brief, in slide order. A single post is a list of one."""
    return list(
        db.scalars(
            select(Creative)
            .where(Creative.brief_id == brief_id)
            .order_by(Creative.slide_position.asc())
        ).all()
    )
