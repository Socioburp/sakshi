"""Meta's 24h customer-service window.

Outside the window you may only send an approved template, not free-form text.
Every outbound path checks this first -- the alternative is silent delivery
failures that look like the bot ignoring the user.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import WaSession
from app.logging import get_logger

log = get_logger(__name__)


def is_open(sess: WaSession | None) -> bool:
    return bool(sess and sess.window_expires_at and sess.window_expires_at > datetime.now(UTC))


def seconds_left(sess: WaSession | None) -> int:
    if not is_open(sess):
        return 0
    return int((sess.window_expires_at - datetime.now(UTC)).total_seconds())


def guard(db: Session, sess: WaSession | None) -> bool:
    """Return True if free-form send is allowed right now."""
    if is_open(sess):
        return True
    log.warning(
        "wa_window_closed",
        session_id=str(sess.id) if sess else None,
        note="free-form send suppressed; use an approved template to re-open",
    )
    return False
