"""Gupshup WhatsApp adapter. Different payload shape, same contract."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

import httpx

from app.channels.base import InboundMessage, MediaRef, OutboundMessage, SendResult
from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

_KIND_MAP = {
    "text": "text",
    "audio": "audio",
    "voice": "audio",
    "image": "image",
    "video": "video",
    "file": "document",
    "location": "location",
    "button_reply": "interactive",
    "list_reply": "interactive",
    "sticker": "sticker",
}


class GupshupAdapter:
    name = "gupshup"
    API = "https://api.gupshup.io/wa/api/v1"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def verify_webhook(self, params: dict[str, str]) -> str | None:
        # Gupshup has no handshake; keep the Meta-style one so the same URL works
        # if you migrate providers later.
        if params.get("hub.verify_token") == settings.wa_verify_token:
            return params.get("hub.challenge")
        return None

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        secret = settings.wa_app_secret
        if not secret:
            return not settings.is_prod
        sent = headers.get("x-gupshup-signature", "")
        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, sent)

    def parse(self, body: dict[str, Any], headers: dict[str, str]) -> list[InboundMessage]:
        if body.get("type") != "message":
            return []
        p = body.get("payload", {}) or {}
        inner = p.get("payload", {}) or {}
        raw_kind = p.get("type", "unsupported")
        kind = _KIND_MAP.get(raw_kind, "unsupported")
        media = None
        if raw_kind in ("audio", "voice", "image", "video", "file", "sticker"):
            media = MediaRef(url=inner.get("url"), mime=inner.get("contentType"))
        return [
            InboundMessage(
                provider=self.name,
                provider_message_id=p.get("id", ""),
                wa_id=p.get("sender", {}).get("phone", ""),
                kind=kind,
                timestamp=datetime.fromtimestamp(body.get("timestamp", 0) / 1000, tz=UTC),
                profile_name=p.get("sender", {}).get("name"),
                text=inner.get("text") or inner.get("title") or inner.get("caption"),
                media=media,
                interactive_id=inner.get("postbackText") or inner.get("id"),
                raw=body,
            )
        ]

    async def download_media(self, ref: MediaRef) -> tuple[bytes, str]:
        resp = await self.client.get(ref.url)
        resp.raise_for_status()
        mime = ref.mime or resp.headers.get("content-type", "application/octet-stream")
        return resp.content, mime

    async def send(self, msg: OutboundMessage) -> SendResult:
        if msg.kind == "text":
            message = {"type": "text", "text": msg.text or ""}
        elif msg.kind == "image":
            message = {
                "type": "image",
                "originalUrl": msg.image_url,
                "previewUrl": msg.image_url,
                "caption": msg.caption or "",
            }
        elif msg.kind == "video":
            message = {"type": "video", "url": msg.video_url, "caption": msg.caption or ""}
        else:
            message = {
                "type": "quick_reply",
                "content": {"type": "text", "text": msg.text or ""},
                "options": [{"type": "text", "title": b.title[:20]} for b in msg.buttons[:3]],
            }
        data = {
            "channel": "whatsapp",
            "source": settings.gupshup_source,
            "destination": msg.to,
            "src.name": settings.gupshup_app_name,
            "message": __import__("json").dumps(message),
        }
        try:
            resp = await self.client.post(
                f"{self.API}/msg",
                headers={
                    "apikey": settings.gupshup_api_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=data,
            )
            resp.raise_for_status()
            return SendResult(provider_message_id=resp.json().get("messageId"))
        except httpx.HTTPError as exc:
            log.error("wa_send_failed", provider=self.name, error=str(exc))
            return SendResult(provider_message_id=None, ok=False, error=str(exc))

    async def mark_read(self, provider_message_id: str) -> None:
        return None
