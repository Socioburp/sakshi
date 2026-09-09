"""Twilio WhatsApp adapter. Form-encoded webhooks, media behind basic auth."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

import httpx

from app.channels.base import InboundMessage, MediaRef, OutboundMessage, SendResult
from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


class TwilioAdapter:
    name = "twilio"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
        return self._client

    def verify_webhook(self, params: dict[str, str]) -> str | None:
        if params.get("hub.verify_token") == settings.wa_verify_token:
            return params.get("hub.challenge")
        return None

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Twilio signs url + sorted POST params, not the raw body.

        The router passes the reconstructed signing string in
        `x-sakshi-signing-base` so this stays a pure function.
        """
        token = settings.twilio_auth_token
        if not token:
            return not settings.is_prod
        sent = headers.get("x-twilio-signature", "")
        base = headers.get("x-sakshi-signing-base", "").encode()
        digest = base64.b64encode(hmac.new(token.encode(), base, hashlib.sha1).digest()).decode()
        return hmac.compare_digest(digest, sent)

    def parse(self, body: dict[str, Any], headers: dict[str, str]) -> list[InboundMessage]:
        wa_id = (body.get("From") or "").replace("whatsapp:", "")
        num_media = int(body.get("NumMedia", 0) or 0)
        media = None
        kind = "text"
        if num_media:
            mime = body.get("MediaContentType0", "")
            media = MediaRef(url=body.get("MediaUrl0"), mime=mime)
            kind = (
                "audio" if mime.startswith("audio")
                else "image" if mime.startswith("image")
                else "video" if mime.startswith("video")
                else "document"
            )
        return [
            InboundMessage(
                provider=self.name,
                provider_message_id=body.get("MessageSid", ""),
                wa_id=wa_id,
                kind=kind,
                timestamp=datetime.now(UTC),
                profile_name=body.get("ProfileName"),
                text=body.get("Body") or None,
                media=media,
                interactive_id=body.get("ButtonPayload"),
                raw=body,
            )
        ]

    async def download_media(self, ref: MediaRef) -> tuple[bytes, str]:
        resp = await self.client.get(ref.url)
        resp.raise_for_status()
        mime = ref.mime or resp.headers.get("content-type", "application/octet-stream")
        return resp.content, mime

    async def send(self, msg: OutboundMessage) -> SendResult:
        data = {"From": settings.twilio_from, "To": f"whatsapp:{msg.to}"}
        if msg.kind == "image":
            data["MediaUrl"] = msg.image_url
            data["Body"] = msg.caption or ""
        else:
            # Twilio has no native reply buttons on the basic API; degrade to
            # a numbered list so the agent's affordances still work.
            body = msg.text or ""
            if msg.buttons:
                body += "\n\n" + "\n".join(
                    f"{i+1}. {b.title}" for i, b in enumerate(msg.buttons)
                )
            data["Body"] = body
        try:
            resp = await self.client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}"
                f"/Messages.json",
                data=data,
            )
            resp.raise_for_status()
            return SendResult(provider_message_id=resp.json().get("sid"))
        except httpx.HTTPError as exc:
            log.error("wa_send_failed", provider=self.name, error=str(exc))
            return SendResult(provider_message_id=None, ok=False, error=str(exc))

    async def mark_read(self, provider_message_id: str) -> None:
        return None
