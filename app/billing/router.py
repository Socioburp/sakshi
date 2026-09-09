from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse

from app.integrations.razorpay import client as rzp
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["billing"])


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> Response:
    raw = await request.body()
    if not rzp.verify_webhook(raw, request.headers.get("x-razorpay-signature", "")):
        log.warning("razorpay_bad_signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    try:
        event = await request.json()
    except ValueError:
        return Response(status_code=status.HTTP_200_OK)
    rzp.apply_payment(event)
    return Response(status_code=status.HTTP_200_OK)


@router.get("/billing/thanks", response_class=HTMLResponse)
async def thanks() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<style>body{font:16px/1.5 system-ui;display:grid;place-items:center;height:100vh}"
        "</style><div>Payment received. Credits are on your account — back to WhatsApp.</div>"
    )
