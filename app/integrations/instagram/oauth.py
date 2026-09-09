"""Instagram Login callback. Needed early because the redirect URI must be a
real HTTPS URL before you can register the app -- same reason the webhook does."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.config import settings
from app.db.models import Brand, IgAccount
from app.db.session import session_scope
from app.integrations.instagram import client as ig
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/oauth/instagram", tags=["instagram"])

# The connect link travels over WhatsApp, where it gets forwarded and
# screenshotted. A bare account id as `state` let anyone holding the link
# complete THEIR Instagram login against the owner's account, after which the
# owner's next approved creative would post to the wrong feed. The state is
# now signed and expires.
STATE_TTL_S = 30 * 60


def _secret() -> bytes:
    key = settings.ig_app_secret or settings.wa_app_secret
    if not key:
        if settings.is_prod:
            raise RuntimeError("IG_APP_SECRET is unset; cannot sign OAuth state")
        key = "dev-only-state-secret"
    return key.encode()


def sign_state(account_id: uuid.UUID) -> str:
    ts = str(int(time.time()))
    msg = f"{account_id}.{ts}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{msg}.{sig}"


def verify_state(state: str) -> uuid.UUID | None:
    """The account id the state was minted for, or None if forged or stale."""
    try:
        raw_id, ts, sig = state.split(".")
        account_id = uuid.UUID(raw_id)
        issued = int(ts)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(_secret(), f"{raw_id}.{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return None
    # The worker signs, the web process verifies; allow a minute of skew.
    if not -60 <= time.time() - issued <= STATE_TTL_S:
        return None
    return account_id


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{{font:16px/1.5 system-ui;margin:0;display:grid;place-items:center;height:100vh;
padding:24px;text-align:center;color:#111}}p{{max-width:32ch}}</style>
<div><h2>{title}</h2><p>{body}</p></div>"""


@router.get("/callback", response_class=HTMLResponse)
async def callback(request: Request) -> HTMLResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return HTMLResponse(
            PAGE.format(title="Couldn't connect", body="Please try the link again."),
            status_code=400,
        )
    account_id = verify_state(state)
    if account_id is None:
        return HTMLResponse(
            PAGE.format(
                title="This link has expired",
                body=(
                    "Ask Sakshi on WhatsApp for a fresh Instagram link and tap it "
                    "within 30 minutes."
                ),
            ),
            status_code=400,
        )

    try:
        token = await ig.exchange_code_for_token(code)
        profile = await ig.get_profile(token.access_token)
    except Exception:  # noqa: BLE001 - a raw 500 page is not an answer for a shop owner
        log.exception("ig_oauth_exchange_failed", account_id=str(account_id))
        return HTMLResponse(
            PAGE.format(
                title="Instagram didn't complete the connection",
                body="Nothing was changed. Go back to WhatsApp and ask for the link again.",
            ),
            status_code=502,
        )

    with session_scope() as db:
        brand = db.scalar(select(Brand).where(Brand.account_id == account_id).limit(1))
        if brand is None:
            return HTMLResponse(PAGE.format(title="Couldn't connect", body="Unknown account."), 404)
        row = db.scalar(
            select(IgAccount).where(
                IgAccount.brand_id == brand.id, IgAccount.ig_user_id == profile.user_id
            )
        )
        if row is None:
            row = IgAccount(brand_id=brand.id, ig_user_id=profile.user_id)
            db.add(row)
        row.username = profile.username
        row.access_token = token.access_token
        row.token_expires_at = token.expires_at
        row.scopes = ig.SCOPES
        row.status = "connected"

    log.info("ig_connected", account_id=str(account_id), username=profile.username)
    return HTMLResponse(
        PAGE.format(
            title=f"Connected @{profile.username}",
            body="You can close this and go back to WhatsApp.",
        )
    )
