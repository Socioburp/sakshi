"""Razorpay: credit top-ups.

Payment links only -- the bot never handles card details, and the webhook is
the only thing that moves credits. Signature verification is not optional here.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

import httpx

from app.billing import credits
from app.config import settings
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger(__name__)
API = "https://api.razorpay.com/v1"

PACKS = {
    "starter": {"credits": 25, "amount_paise": 29900, "label": "25 creatives"},
    "growth": {"credits": 100, "amount_paise": 99900, "label": "100 creatives"},
}


async def create_payment_link(*, account_id: uuid.UUID, pack: str, phone: str) -> str:
    p = PACKS[pack]
    async with httpx.AsyncClient(
        timeout=30, auth=(settings.razorpay_key_id, settings.razorpay_key_secret)
    ) as c:
        r = await c.post(
            f"{API}/payment_links",
            json={
                "amount": p["amount_paise"],
                "currency": "INR",
                "description": f"Sakshi — {p['label']}",
                "customer": {"contact": f"+{phone}"},
                "notify": {"sms": False, "email": False},
                "notes": {"account_id": str(account_id), "pack": pack},
                "callback_url": f"{settings.public_base_url}/billing/thanks",
                "callback_method": "get",
            },
        )
        r.raise_for_status()
        return r.json()["short_url"]


def verify_webhook(raw_body: bytes, signature: str) -> bool:
    if not settings.razorpay_webhook_secret:
        return not settings.is_prod
    digest = hmac.new(
        settings.razorpay_webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, signature or "")


def apply_payment(event: dict) -> bool:
    """Idempotent on the Razorpay payment id."""
    if event.get("event") not in ("payment_link.paid", "payment.captured"):
        return False
    entity = event.get("payload", {}).get("payment", {}).get("entity", {}) or event.get(
        "payload", {}
    ).get("payment_link", {}).get("entity", {})
    notes = entity.get("notes", {}) or {}
    account_id, pack = notes.get("account_id"), notes.get("pack")
    if not account_id or pack not in PACKS:
        log.warning("razorpay_unmapped_payment", entity_id=entity.get("id"))
        return False
    with session_scope() as db:
        credits.topup(
            db,
            account_id=uuid.UUID(account_id),
            credits=PACKS[pack]["credits"],
            reason=f"topup:{pack}",
            idempotency_key=f"razorpay:{entity.get('id')}",
        )
    log.info("credits_topped_up", account_id=account_id, pack=pack)
    return True
