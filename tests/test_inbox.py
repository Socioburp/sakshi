"""The engagement inbox: webhook parsing, the reply loop, and the tap flow.

Pure parser/signature tests need nothing. The service and handler tests need a
database and skip cleanly without one. No model is configured in tests, so the
drafted reply is the deterministic fallback line.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

import pytest

from app.integrations.instagram import webhook

# --------------------------------------------------------------------------- #
# webhook: verification and parsing (pure)
# --------------------------------------------------------------------------- #
COMMENT_BODY = {
    "object": "instagram",
    "entry": [
        {
            "id": "17841400000000000",
            "time": 1,
            "changes": [
                {
                    "field": "comments",
                    "value": {
                        "id": "COMMENT_1",
                        "text": "How much for 1L?",
                        "from": {"id": "user_9", "username": "asha_kitchen"},
                        "media": {"id": "MEDIA_1", "media_product_type": "FEED"},
                    },
                }
            ],
        }
    ],
}

MESSAGE_BODY = {
    "object": "instagram",
    "entry": [
        {
            "id": "17841400000000000",
            "messaging": [
                {
                    "sender": {"id": "user_5"},
                    "recipient": {"id": "17841400000000000"},
                    "message": {"mid": "MID_1", "text": "Do you deliver to Jayanagar?"},
                }
            ],
        }
    ],
}


def test_challenge_handshake(monkeypatch):
    monkeypatch.setattr(webhook.settings, "ig_verify_token", "tok")
    assert (
        webhook.verify_challenge(
            {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "99"}
        )
        == "99"
    )
    assert webhook.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "no"}) is None


def test_signature_verification(monkeypatch):
    monkeypatch.setattr(webhook.settings, "ig_app_secret", "s3cret")
    monkeypatch.setattr(webhook.settings, "env", "prod")
    raw = b'{"hello":"world"}'
    good = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    assert webhook.verify_signature(raw, {"x-hub-signature-256": good})
    assert not webhook.verify_signature(raw, {"x-hub-signature-256": "sha256=deadbeef"})
    assert not webhook.verify_signature(raw, {}), "prod rejects an unsigned body"
    # md5-style prefix or no prefix is refused
    assert not webhook.verify_signature(raw, {"x-hub-signature-256": "md5=abc"})


def test_signature_is_lenient_in_dev(monkeypatch):
    monkeypatch.setattr(webhook.settings, "env", "dev")
    assert webhook.verify_signature(b"x", {}), "the mock sends no signature"


def test_parse_comment_and_message():
    (comment,) = webhook.parse(COMMENT_BODY)
    assert comment.kind == "comment" and comment.ig_object_id == "COMMENT_1"
    assert comment.recipient_ig_id == "17841400000000000"
    assert comment.from_username == "asha_kitchen" and comment.from_id == "user_9"
    assert comment.text == "How much for 1L?" and comment.media_id == "MEDIA_1"

    (msg,) = webhook.parse(MESSAGE_BODY)
    assert msg.kind == "message" and msg.ig_object_id == "MID_1"
    assert msg.from_id == "user_5" and msg.text == "Do you deliver to Jayanagar?"


def test_parse_ignores_echoes_reactions_and_unknown_fields():
    echo = {
        "object": "instagram",
        "entry": [
            {
                "id": "1",
                "messaging": [
                    {
                        "sender": {"id": "1"},
                        "recipient": {"id": "x"},
                        "message": {"mid": "M", "text": "hi", "is_echo": True},
                    }
                ],
            }
        ],
    }
    (only,) = webhook.parse(echo)
    assert only.is_echo is True
    # a reaction (no mid) and an unknown change field produce nothing
    assert (
        webhook.parse(
            {"object": "instagram", "entry": [{"id": "1", "messaging": [{"reaction": {}}]}]}
        )
        == []
    )
    assert (
        webhook.parse(
            {"object": "instagram", "entry": [{"id": "1", "changes": [{"field": "likes"}]}]}
        )
        == []
    )
    assert webhook.parse({}) == [] and webhook.parse({"object": "page"}) == []


def test_ig_buttons_carry_the_event_id():
    from app.agent import buttons as btn

    eid = "abc-123"
    ids = [b.id for b in btn.ig_buttons(eid, "en")]
    assert ids == [f"igok:{eid}", f"iged:{eid}", f"igno:{eid}"]
    titles = [b.title for b in btn.ig_buttons(eid, "en")]
    assert titles == ["Send", "Edit", "Skip"]
    assert [b.title for b in btn.ig_buttons(eid, "hi-IN")] == ["Bhejo", "Badlo", "Rehne do"]
    assert all(len(b.title) <= 20 for b in btn.ig_buttons(eid, "ta"))


# --------------------------------------------------------------------------- #
# service + handlers (database)
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_ready():
    from sqlalchemy import text as sql_text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("select 1 from ig_events limit 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database with the schema available: {str(exc)[:80]}")


@pytest.fixture
def wired(db_ready):
    """An account, a brand, and a connected Instagram account, inside the window.

    The ig_user_id is unique per test: it is what routes an event to a brand,
    and reusing one across tests would make routing pick an older account.
    """
    from app.db import repo
    from app.db.models import Account, Brand, IgAccount
    from app.db.session import session_scope

    wa = f"9199{uuid.uuid4().int % 10**8:08d}"
    ig_uid = f"17841{uuid.uuid4().int % 10**12:012d}"
    with session_scope() as db:
        acct = Account(wa_phone=wa, credits_balance=10)
        db.add(acct)
        db.flush()
        brand = Brand(account_id=acct.id, name="Kadamba Naturals", category="cold-pressed oils")
        db.add(brand)
        db.flush()
        db.add(
            IgAccount(brand_id=brand.id, ig_user_id=ig_uid, status="connected", access_token="tok")
        )
        repo.touch_session(db, acct, wa, inbound=True)  # open the 24h window
        return acct.id, brand.id, wa, ig_uid


def _record(inbound):
    from app.db.session import session_scope
    from app.inbox import service

    with session_scope() as db:
        return service.record(db, inbound)


def _comment(cid, recipient, **kw):
    from app.integrations.instagram.webhook import IgInbound

    return IgInbound(
        kind="comment",
        ig_object_id=cid,
        recipient_ig_id=recipient,
        from_id=kw.get("from_id", "user_1"),
        from_username=kw.get("from_username", "asha"),
        text=kw.get("text", "nice!"),
    )


def _true():
    async def _c():
        return True

    return _c()


def test_record_dedupes_and_skips_the_uninteresting(wired):
    from app.integrations.instagram.webhook import IgInbound

    _, _, _, ig_uid = wired
    assert _record(_comment("C1", ig_uid)) is not None
    assert _record(_comment("C1", ig_uid)) is None, "a webhook retry is deduped"
    assert _record(_comment("C2", ig_uid, text="   ")) is None, "an empty comment is skipped"
    assert _record(_comment("C3", ig_uid, from_id=ig_uid)) is None, "our own comment is skipped"
    unrouted = IgInbound(kind="comment", ig_object_id="C4", recipient_ig_id="99999")
    assert _record(unrouted) is None, "an unknown account routes nowhere"


async def test_notify_drafts_and_sends_three_buttons(wired, monkeypatch):
    from app.channels.whatsapp import send
    from app.db.models import IgEvent
    from app.db.session import session_scope
    from app.queue import handlers

    account_id, brand_id, wa, ig_uid = wired
    event_id = _record(_comment("C10", ig_uid, from_username="asha", text="How much for 1L?"))
    sent = []

    async def fake_send_text(**kw):
        sent.append(kw)
        return True

    monkeypatch.setattr(send, "send_text", fake_send_text)
    await handlers.ig_event_notify({"event_id": str(event_id)})
    await handlers.ig_event_notify({"event_id": str(event_id)})  # a duplicate job: no second nudge
    assert len(sent) == 1
    msg = sent[0]
    assert "asha" in msg["text"] and "How much for 1L?" in msg["text"]
    assert [b.id for b in msg["buttons"]] == [
        f"igok:{event_id}",
        f"iged:{event_id}",
        f"igno:{event_id}",
    ]
    with session_scope() as db:
        ev = db.get(IgEvent, event_id)
        assert ev.status == "drafted" and ev.draft_reply, "the draft is stored for the tap"


async def test_notify_is_suppressed_outside_the_window(wired, monkeypatch):
    from app.channels.whatsapp import send
    from app.db.models import IgEvent, WaSession
    from app.db.session import session_scope
    from app.queue import handlers

    account_id, brand_id, wa, ig_uid = wired
    event_id = _record(_comment("C15", ig_uid, text="hello"))
    # Close the window: no live session for this number.
    with session_scope() as db:
        from datetime import UTC, datetime

        db.query(WaSession).filter(WaSession.account_id == account_id).update(
            {"closed_at": datetime.now(UTC)}
        )
    sent = []
    monkeypatch.setattr(send, "send_text", lambda **kw: sent.append(kw) or _true())
    await handlers.ig_event_notify({"event_id": str(event_id)})
    assert sent == [], "outside the 24h window nothing is sent"
    with session_scope() as db:
        assert db.get(IgEvent, event_id).status == "new", "left for a later inbound to resurface"


async def test_send_tap_posts_a_comment_reply(wired, monkeypatch):
    from app.channels.whatsapp import send
    from app.db.models import IgEvent
    from app.db.session import session_scope
    from app.integrations.instagram import client as ig
    from app.queue import handlers

    account_id, brand_id, wa, ig_uid = wired
    event_id = _record(_comment("C20", ig_uid, from_username="ravi", text="Beautiful!"))
    with session_scope() as db:
        db.get(IgEvent, event_id).draft_reply = "Thank you Ravi!"
        db.get(IgEvent, event_id).status = "drafted"

    replied = {}

    async def fake_reply(*, comment_id, access_token, message):
        replied["comment_id"] = comment_id
        replied["message"] = message
        return "REPLY_99"

    monkeypatch.setattr(ig, "reply_to_comment", fake_reply)
    monkeypatch.setattr(send, "send_text", lambda **kw: _true())
    await handlers.ig_action({"event_id": str(event_id), "action": "send"})
    assert replied == {"comment_id": "C20", "message": "Thank you Ravi!"}
    with session_scope() as db:
        ev = db.get(IgEvent, event_id)
        assert ev.status == "sent" and ev.reply_ig_id == "REPLY_99"
        assert ev.reply_text == "Thank you Ravi!"
    # A second tap on an already-sent event posts nothing more.
    replied.clear()
    await handlers.ig_action({"event_id": str(event_id), "action": "send"})
    assert replied == {}


async def test_a_dm_reply_uses_send_dm_and_skip_posts_nothing(wired, monkeypatch):
    from app.channels.whatsapp import send
    from app.db.models import IgEvent
    from app.db.session import session_scope
    from app.integrations.instagram import client as ig
    from app.integrations.instagram.webhook import IgInbound
    from app.queue import handlers

    account_id, brand_id, wa, ig_uid = wired
    dm = _record(
        IgInbound(
            kind="message",
            ig_object_id="MID_9",
            recipient_ig_id=ig_uid,
            from_id="user_77",
            text="do you deliver?",
        )
    )
    with session_scope() as db:
        db.get(IgEvent, dm).draft_reply = "Yes, across the city!"
        db.get(IgEvent, dm).status = "drafted"

    calls = {}

    async def fake_dm(*, ig_user_id, access_token, recipient_id=None, comment_id=None, text):
        calls["recipient_id"] = recipient_id
        calls["text"] = text
        return "DM_OUT_1"

    monkeypatch.setattr(ig, "send_dm", fake_dm)
    monkeypatch.setattr(send, "send_text", lambda **kw: _true())
    await handlers.ig_action({"event_id": str(dm), "action": "send"})
    assert calls == {"recipient_id": "user_77", "text": "Yes, across the city!"}
    with session_scope() as db:
        assert db.get(IgEvent, dm).status == "sent"

    # A separate event, skipped: nothing is posted, status is skipped.
    skip_id = _record(_comment("C_SKIP", ig_uid, text="hi"))
    posted = []
    monkeypatch.setattr(ig, "reply_to_comment", lambda **kw: posted.append(kw) or _true())
    await handlers.ig_action({"event_id": str(skip_id), "action": "skip"})
    assert posted == []
    with session_scope() as db:
        assert db.get(IgEvent, skip_id).status == "skipped"


def test_edit_tap_arms_the_session_and_the_next_message_is_the_reply(wired):
    """The tap flow through ingest: Edit arms ig_edit, a typed line becomes a
    custom reply, and the edit is disarmed once consumed."""
    from datetime import UTC, datetime

    from app.channels.base import InboundMessage
    from app.channels.whatsapp import ingest
    from app.db import repo
    from app.db.models import Job
    from app.db.session import session_scope

    account_id, brand_id, wa, ig_uid = wired
    event_id = _record(_comment("C30", ig_uid, text="Interested!"))

    def _tap(iid=None, text=None):
        return InboundMessage(
            provider="mock",
            wa_id=wa,
            provider_message_id=f"m-{uuid.uuid4().hex[:8]}",
            kind="text" if text else "interactive",
            timestamp=datetime.now(UTC),
            text=text,
            interactive_id=iid,
        )

    # Tap Edit -> the session is armed with the event id, job is ig_action/edit.
    ingest.ingest(_tap(iid=f"iged:{event_id}"))
    with session_scope() as db:
        sess = repo.latest_session(db, wa, account_id=account_id)
        assert sess.state.get("ig_edit") == str(event_id)
        job = db.query(Job).filter(Job.kind == "ig_action").order_by(Job.created_at.desc()).first()
        assert job.payload["action"] == "edit_prompt"

    # Now type the replacement -> a custom ig_action, and the edit is disarmed.
    ingest.ingest(_tap(text="Ships in 2 days across Bangalore"))
    with session_scope() as db:
        sess = repo.latest_session(db, wa, account_id=account_id)
        assert "ig_edit" not in (sess.state or {})
        job = db.query(Job).filter(Job.kind == "ig_action").order_by(Job.created_at.desc()).first()
        assert job.payload["action"] == "custom"
        assert job.payload["custom_text"] == "Ships in 2 days across Bangalore"
        assert job.payload["event_id"] == str(event_id)


def test_webhook_endpoint_verifies_and_persists(wired, monkeypatch):
    """GET handshake echoes the challenge; POST records an event and enqueues."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.db.models import IgEvent
    from app.db.session import session_scope
    from app.main import app
    from app.queue import client as qclient

    account_id, brand_id, wa, ig_uid = wired
    monkeypatch.setattr(qclient, "push_job", lambda *a, **k: True)
    client = TestClient(app)

    got = client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.ig_verify_token or settings.wa_verify_token,
            "hub.challenge": "PING-7",
        },
    )
    assert got.status_code == 200 and got.text == "PING-7"

    body = {
        "object": "instagram",
        "entry": [
            {
                "id": ig_uid,
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "WH_C1",
                            "text": "Price?",
                            "from": {"id": "u2", "username": "meena"},
                            "media": {"id": "M9"},
                        },
                    }
                ],
            }
        ],
    }
    res = client.post("/webhooks/instagram", json=body)
    assert res.status_code == 200
    with session_scope() as db:
        from sqlalchemy import select

        ev = db.scalar(
            select(IgEvent).where(IgEvent.brand_id == brand_id, IgEvent.ig_object_id == "WH_C1")
        )
        assert ev is not None and ev.from_username == "meena"

    # A bad signature in prod is refused.
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "ig_app_secret", "s3cret")
    bad = client.post(
        "/webhooks/instagram", json=body, headers={"X-Hub-Signature-256": "sha256=nope"}
    )
    assert bad.status_code == 403
