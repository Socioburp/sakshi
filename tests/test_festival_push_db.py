"""The festival offer end to end, against real Postgres (skips without one).

In the window it is a free message with buttons; outside it, the paid template
-- once per festival, capped per month, and the owner's tap still finds the idea.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def db_ready():
    from sqlalchemy import text as sql_text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("select billed from creatives limit 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database with the schema available: {str(exc)[:80]}")


@pytest.fixture
def outbox():
    from app.channels.whatsapp.adapters import get_adapter

    adapter = get_adapter()
    adapter.sent.clear()
    return adapter.sent


@pytest.fixture
def festival(monkeypatch):
    """A verified festival two days from today, whatever today is."""
    from app.insights import festival_push as fp
    from app.insights import suggest as sg

    day = datetime.now(UTC).astimezone(fp.IST).date() + timedelta(days=2)
    cal = [{"name": "Testotsav", "date": day.isoformat(), "angle": "lamps, sweets"}]
    monkeypatch.setattr(sg, "load_festivals", lambda path=None: cal)
    return "Testotsav"


def _shop(*, window_open: bool, photo: bool = True):
    from app.db.models import Account, Brand, BrandAsset, WaSession
    from app.db.session import session_scope

    wa_id = f"9198{uuid.uuid4().int % 10**8:08d}"
    now = datetime.now(UTC)
    with session_scope() as db:
        acct = Account(wa_phone=wa_id, credits_balance=5, locale="en-IN")
        db.add(acct)
        db.flush()
        brand = Brand(
            account_id=acct.id,
            name="Sri Test Sweets",
            category="sweet shop",
            palette={"primary": "#7A1E1E", "accent": "#F2B233", "ink": "#FFFFFF"},
            fonts={"heading": "Poppins", "body": "Inter"},
        )
        db.add(brand)
        db.flush()
        asset_id = None
        if photo:
            asset = BrandAsset(
                brand_id=brand.id, kind="product", label="motichoor box", storage_key="k/p.jpg"
            )
            db.add(asset)
            db.flush()
            asset_id = str(asset.id)
        expires = now + timedelta(hours=5) if window_open else now - timedelta(hours=30)
        db.add(
            WaSession(
                account_id=acct.id,
                wa_id=wa_id,
                state={},
                last_inbound_at=expires - timedelta(hours=24),
                window_expires_at=expires,
            )
        )
        return {"account_id": str(acct.id), "brand_id": str(brand.id), "wa_id": wa_id}, asset_id


def _events(brand_id):
    from app.db.models import CreativeEvent
    from app.db.session import session_scope

    with session_scope() as db:
        rows = db.query(CreativeEvent).filter(CreativeEvent.brand_id == uuid.UUID(brand_id)).all()
        return [dict(r.meta or {}) for r in rows if r.kind == "suggested"]


async def test_inside_the_window_it_is_a_free_message_about_their_own_photo(
    db_ready, outbox, festival
):
    from app.db import repo
    from app.db.session import session_scope
    from app.queue.handlers import festival_push

    payload, asset_id = _shop(window_open=True)
    await festival_push(payload)

    assert len(outbox) == 1 and outbox[0].kind == "buttons"
    assert "Testotsav is in 2 days" in outbox[0].text
    assert "your motichoor box photo" in outbox[0].text
    assert [b.id for b in outbox[0].buttons] == ["make:1", "next", "skip"]
    with session_scope() as db:
        sess = repo.latest_session(db, payload["wa_id"])
        first = sess.state["suggestions"][0]
        assert first["festival"] == "Testotsav" and first["reference_asset_id"] == asset_id
    (event,) = _events(payload["brand_id"])
    assert event["paid_template"] is False and event["own_photo"] is True

    await festival_push(payload)
    assert len(outbox) == 1, "one offer per festival, ever"


async def test_outside_the_window_nothing_is_sent_until_a_template_is_configured(
    db_ready, outbox, festival, monkeypatch
):
    from app.queue import handlers

    monkeypatch.setattr(handlers.settings, "wa_template_festival", "")
    payload, _ = _shop(window_open=False)
    await handlers.festival_push(payload)
    assert outbox == [] and _events(payload["brand_id"]) == []


async def test_outside_the_window_it_is_the_template_and_the_tap_finds_the_idea(
    db_ready, outbox, festival, monkeypatch
):
    from app.db import repo
    from app.db.models import Account
    from app.db.session import session_scope
    from app.queue import handlers

    monkeypatch.setattr(handlers.settings, "wa_template_festival", "festival_offer")
    payload, asset_id = _shop(window_open=False)
    await handlers.festival_push(payload)

    (msg,) = outbox
    assert msg.kind == "template" and msg.template_name == "festival_offer"
    assert msg.template_params == ["Testotsav", "2", "your motichoor box photo"]
    assert [b.id for b in msg.buttons] == ["make:1", "skip"]
    (event,) = _events(payload["brand_id"])
    assert event["paid_template"] is True

    # The owner taps "Make it": a NEW session opens, and the idea is still there.
    with session_scope() as db:
        acct = db.get(Account, uuid.UUID(payload["account_id"]))
        sess = repo.touch_session(db, acct, payload["wa_id"], inbound=True)
        assert repo.window_is_open(sess)
        assert sess.state["suggestions"][0]["reference_asset_id"] == asset_id


async def test_paid_pushes_stop_at_the_monthly_cap(db_ready, outbox, festival, monkeypatch):
    from app.queue import handlers

    monkeypatch.setattr(handlers.settings, "wa_template_festival", "festival_offer")
    monkeypatch.setattr(handlers.settings, "festival_push_monthly_cap", 0)
    payload, _ = _shop(window_open=False)
    await handlers.festival_push(payload)
    assert outbox == []


async def test_an_owner_who_turned_nudges_off_hears_nothing(db_ready, outbox, festival):
    from app.db.models import Brand
    from app.db.session import session_scope
    from app.queue.handlers import festival_push

    payload, _ = _shop(window_open=True)
    with session_scope() as db:
        brand = db.get(Brand, uuid.UUID(payload["brand_id"]))
        brand.template_prefs = {**(brand.template_prefs or {}), "daily_nudge": False}
    await festival_push(payload)
    assert outbox == []


async def test_a_shop_with_no_photo_is_still_offered_the_festival_honestly(
    db_ready, outbox, festival
):
    from app.queue.handlers import festival_push

    payload, _ = _shop(window_open=True, photo=False)
    await festival_push(payload)
    (msg,) = outbox
    assert "Testotsav is in 2 days" in msg.text and "photo" not in msg.text
