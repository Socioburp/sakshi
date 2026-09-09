"""Instagram Login callback. Needed early because the redirect URI must be a
real HTTPS URL before you can register the app -- same reason the webhook does."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.db.models import Brand, IgAccount
from app.db.session import session_scope
from app.integrations.instagram import client as ig
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/oauth/instagram", tags=["instagram"])

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
    try:
        account_id = uuid.UUID(state)
    except ValueError:
        return HTMLResponse(PAGE.format(title="Couldn't connect", body="Bad link."), 400)

    token = await ig.exchange_code_for_token(code)
    profile = await ig.get_profile(token.access_token)

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
