"""Thin data-access helpers. Keeps SQL out of the agent and channel layers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Account, Brand, Brief, Creative, Message, WaSession

WA_WINDOW = timedelta(hours=24)


def now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
def get_or_create_account(db: Session, wa_phone: str, display_name: str | None = None) -> Account:
    """Find or create, safely under concurrent first contact.

    A new owner's "hi" and their voice note arrive in the same second and are
    ingested concurrently. Check-then-insert raised IntegrityError for the
    second one and silently dropped their message -- on the very first
    contact. The insert now runs in a savepoint; the loser re-reads.
    """
    acct = db.scalar(select(Account).where(Account.wa_phone == wa_phone))
    if acct:
        if display_name and not acct.display_name:
            acct.display_name = display_name
            db.flush()
        return acct
    try:
        with db.begin_nested():
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
    except IntegrityError:
        db.expire_all()
        acct = db.scalar(select(Account).where(Account.wa_phone == wa_phone))
        if acct is None:  # pragma: no cover - the unique index guarantees a winner
            raise
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
    carried: dict = {}
    carried_brief: uuid.UUID | None = None
    if sess and sess.window_expires_at and sess.window_expires_at < ts:
        carried = _carry_over(sess.state or {}, ts)
        carried_brief = _carry_brief(db, sess.active_brief_id, ts)
        sess.closed_at = ts
        db.flush()
        sess = None
    if sess is None:
        # Same race as the account: two first messages, one live session.
        try:
            with db.begin_nested():
                sess = WaSession(
                    account_id=account.id,
                    wa_id=wa_id,
                    active_brief_id=carried_brief,
                    state=dict(carried),
                )
                db.add(sess)
                db.flush()
        except IntegrityError:
            db.expire_all()
            sess = db.scalar(
                select(WaSession)
                .where(WaSession.account_id == account.id, WaSession.wa_id == wa_id)
                .where(WaSession.closed_at.is_(None))
                .order_by(WaSession.created_at.desc())
                .limit(1)
            )
    if inbound:
        sess.last_inbound_at = ts
        sess.window_expires_at = ts + WA_WINDOW
    else:
        sess.last_outbound_at = ts
    db.flush()
    return sess


# An idea pushed OUTSIDE the window (a festival template) is answered by a tap
# that opens a brand-new session. Without this the tap's "Make it" would point
# at nothing: the ideas lived on the session that just closed.
PUSHED_IDEA_TTL = timedelta(hours=72)


def _carry_over(state: dict, ts: datetime) -> dict:
    ideas, at = state.get("suggestions"), state.get("suggestions_at")
    if not ideas or not at:
        return {}
    try:
        fresh = ts - datetime.fromisoformat(at) <= PUSHED_IDEA_TTL
    except (TypeError, ValueError):
        return {}
    return {"suggestions": ideas, "suggestions_at": at} if fresh else {}


def _carry_brief(db: Session, brief_id: uuid.UUID | None, ts: datetime) -> uuid.UUID | None:
    """The creative they were shown last night is the one "change the headline"
    means this morning. The window closing used to drop active_brief_id, so
    the request targeted nothing and the agent made (and charged) a new one.
    Not carried once it is approved (nothing left to change) or its drafts
    have expired (nothing left to change it on)."""
    if not brief_id:
        return None
    rows = creatives_for_brief(db, brief_id)
    if not rows or all(r.approved_at for r in rows):
        return None
    if any(r.expires_at and r.expires_at <= ts for r in rows):
        return None
    return brief_id


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
    # The id is minted here so a root can name itself before the INSERT: the
    # lineage is complete on the row the moment it exists, never "fixed later".
    brief_id = uuid.uuid4()
    brief = Brief(
        id=brief_id,
        account_id=account_id,
        brand_id=brand_id,
        payload=payload,
        source_message_id=source_message_id,
        parent_brief_id=parent.id if parent else None,
        root_brief_id=(parent.root_brief_id or parent.id) if parent else brief_id,
        version=(parent.version + 1) if parent else 1,
    )
    db.add(brief)
    db.flush()
    return brief


def lineage(db: Session, brief_id: uuid.UUID) -> list[Brief]:
    """Every version of a creative, root first, ending at `brief_id`.

    Walks parent_brief_id rather than reading root_brief_id: a chain written
    before the root column existed is still a chain, and a cycle (impossible
    in practice, fatal in a loop) is cut by the seen-set.
    """
    chain: list[Brief] = []
    seen: set[uuid.UUID] = set()
    cur = db.get(Brief, brief_id)
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        chain.append(cur)
        cur = db.get(Brief, cur.parent_brief_id) if cur.parent_brief_id else None
    chain.reverse()
    return chain


def revision_no(db: Session, brief_id: uuid.UUID) -> int:
    """How many change requests this creative has had so far: 0 for a first
    version, 1 once the owner has asked for one change, and so on. It is the
    number the ladder rules are keyed on, so it comes from the database every
    turn -- session state is lost at the 24h window."""
    return max(0, len(lineage(db, brief_id)) - 1)


def request_text(db: Session, message_id: uuid.UUID | None, limit: int = 500) -> str | None:
    """What the owner actually said in the message that asked for a change:
    the typed text, or the transcript of their voice note."""
    if message_id is None:
        return None
    msg = db.get(Message, message_id)
    if msg is None:
        return None
    text = msg.text or msg.transcript
    return text[:limit] if text else None


def latest_creative_for_brief(db: Session, brief_id: uuid.UUID) -> Creative | None:
    return db.scalar(
        select(Creative)
        .where(Creative.brief_id == brief_id)
        .order_by(Creative.created_at.desc())
        .limit(1)
    )


def mark_approved(
    db: Session, *, brief_id: str, via: str, account_id: uuid.UUID | None = None
) -> int:
    """Stamp every slide of a brief as approved. Returns how many were stamped.

    Scoped to the account that tapped: a button id is ours, but Twilio's and
    Gupshup's payloads arrive from the provider verbatim, and an approval must
    never land on another account's creative.
    """
    try:
        bid = uuid.UUID(brief_id)
    except (ValueError, AttributeError):
        return 0
    if account_id is not None:
        brief = db.get(Brief, bid)
        if brief is None or brief.account_id != account_id:
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
