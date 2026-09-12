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
from app.insights import votes
from app.logging import get_logger
from app.queue.client import add_job, push_job

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
            # "" is what adapters send when the provider gave no id; the unique
            # index would make the second such message collide. NULLs do not.
            provider_message_id=msg.provider_message_id or None,
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
            approved = repo.mark_approved(
                db, brief_id=brief_id, via="button", account_id=account.id
            )
            log.info("approval_recorded", brief_id=brief_id, slides=approved)
            if approved:
                votes.approve(db, brief_id, remember=False)
        elif msg.interactive_id and msg.interactive_id.startswith("revise:"):
            votes.change_words(db, msg.interactive_id[len("revise:") :], account.id)
        elif msg.interactive_id and msg.interactive_id.startswith("redo:"):
            votes.change_picture(db, msg.interactive_id[len("redo:") :], account.id, remember=False)
        elif msg.interactive_id == "skip" and brand is not None:
            # "Not today" is not "never": three quiet days, then ideas resume.
            votes.snooze_nudge(db, brand)

        # An Instagram-reply tap (Send/Edit/Skip), or the reply the owner typed
        # after tapping Edit, is a bounded action: post it, don't run the agent.
        ig_action = _ig_action(msg, sess)
        if ig_action is not None:
            kind = "ig_action"
            payload = {
                "message_id": str(message_id),
                "account_id": str(account_id),
                **ig_action,
            }
        else:
            if msg.kind in AUDIO_KINDS:
                kind = "transcribe_and_handle"
            elif msg.kind in IMAGE_KINDS:
                kind = "handle_image"
            else:
                kind = "handle_message"
            # An owner who said "no logo" sends product photos, not a logo.
            no_logo = bool(brand and (brand.template_prefs or {}).get("no_logo"))
            is_logo_candidate = (
                msg.kind in IMAGE_KINDS and not (brand and brand.logo_url) and not no_logo
            )
            payload = {
                "message_id": str(message_id),
                "account_id": str(account_id),
                "media_id": msg.media.id if msg.media else None,
                "media_url": msg.media.url if msg.media else None,
                "media_mime": msg.media.mime if msg.media else None,
                "provider": msg.provider,
                "is_logo_candidate": is_logo_candidate,
                # The worker writes the memory for a tap (a network call to the
                # embedding vendor) so the webhook never waits on it.
                "interactive_id": msg.interactive_id,
            }
        # The job row commits WITH the message row. A message that exists with
        # no job is unprocessable forever: the provider's retry is (correctly)
        # deduped as a duplicate message, so nothing ever comes back for it.
        job = add_job(
            db,
            kind=kind,
            payload=payload,
            dedupe_key=(
                f"{msg.provider}:{msg.provider_message_id}" if msg.provider_message_id else None
            ),
        )
        job_id = job.id if job else None

    # Push after the commit so the worker can never see an uncommitted row. If
    # the push itself fails, the row is "queued" and the reaper re-pushes it.
    if job_id is not None:
        push_job(job_id, kind, payload)
    log.info("wa_ingested", message_id=str(message_id), kind=msg.kind, job=kind)
    return message_id


# Instagram-reply tap ids carry the ig_event id: igok=send, iged=edit, igno=skip.
_IG_TAP_ACTIONS = {"igok:": "send", "iged:": "edit_prompt", "igno:": "skip"}


def _ig_action(msg: InboundMessage, sess) -> dict | None:
    """The ig_action payload fields for a reply tap or a typed edit, else None.

    Reads and writes `sess.state["ig_edit"]`: tapping Edit arms it with the
    event id, and the next typed message is consumed as that reply and disarms
    it. A fresh tap always clears any half-finished edit.
    """
    state = dict(sess.state or {})
    iid = msg.interactive_id or ""
    for prefix, action in _IG_TAP_ACTIONS.items():
        if iid.startswith(prefix):
            event_id = iid[len(prefix) :]
            if action == "edit_prompt":
                state["ig_edit"] = event_id
            else:
                state.pop("ig_edit", None)
            sess.state = state
            return {"event_id": event_id, "action": action}
    # A plain typed message right after an Edit tap is the reply to post.
    pending = state.get("ig_edit")
    if pending and not msg.interactive_id and msg.kind == "text" and (msg.text or "").strip():
        state.pop("ig_edit", None)
        sess.state = state
        return {"event_id": pending, "action": "custom", "custom_text": msg.text.strip()}
    # Any other message clears a stale edit so it can't hijack a later reply.
    if pending:
        state.pop("ig_edit", None)
        sess.state = state
    return None


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
