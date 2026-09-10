"""Meta WhatsApp Cloud API adapter."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

import httpx

from app.channels.base import (
    Button,
    InboundMessage,
    MediaRef,
    OutboundMessage,
    SendResult,
)
from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

_KIND_MAP = {
    "text": "text",
    "audio": "audio",
    "voice": "audio",
    "image": "image",
    "video": "video",
    "document": "document",
    "interactive": "interactive",
    "button": "interactive",
    "location": "location",
    "sticker": "sticker",
    "system": "system",
}


class MetaAdapter:
    name = "meta"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self.base = f"https://graph.facebook.com/{settings.wa_graph_version}"

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.wa_access_token}"}

    # -- webhook ----------------------------------------------------------- #
    def verify_webhook(self, params: dict[str, str]) -> str | None:
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == settings.wa_verify_token
        ):
            return params.get("hub.challenge")
        return None

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        if not settings.wa_app_secret:
            # No secret configured: only tolerable outside prod.
            return not settings.is_prod
        sent = headers.get("x-hub-signature-256", "")
        if not sent.startswith("sha256="):
            return False
        digest = hmac.new(settings.wa_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, sent.removeprefix("sha256="))

    def parse(self, body: dict[str, Any], headers: dict[str, str]) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                profiles = {
                    c.get("wa_id"): c.get("profile", {}).get("name")
                    for c in value.get("contacts", [])
                }
                for m in value.get("messages", []):
                    out.append(self._parse_one(m, profiles))
        return out

    def _parse_one(self, m: dict[str, Any], profiles: dict[str, str]) -> InboundMessage:
        raw_kind = m.get("type", "unsupported")
        kind = _KIND_MAP.get(raw_kind, "unsupported")
        wa_id = m.get("from", "")
        ts = datetime.fromtimestamp(int(m.get("timestamp", 0)), tz=UTC)

        text = media = duration = interactive_id = None
        if raw_kind == "text":
            text = m["text"]["body"]
        elif raw_kind in ("audio", "voice"):
            node = m[raw_kind]
            media = MediaRef(id=node.get("id"), mime=node.get("mime_type"))
        elif raw_kind in ("image", "video", "document", "sticker"):
            node = m[raw_kind]
            media = MediaRef(id=node.get("id"), mime=node.get("mime_type"))
            text = node.get("caption")
        elif raw_kind == "interactive":
            node = m["interactive"]
            inner = node.get("button_reply") or node.get("list_reply") or {}
            interactive_id = inner.get("id")
            text = inner.get("title")
        elif raw_kind == "button":
            interactive_id = m["button"].get("payload")
            text = m["button"].get("text")

        return InboundMessage(
            provider=self.name,
            provider_message_id=m.get("id", ""),
            wa_id=wa_id,
            kind=kind,
            timestamp=ts,
            profile_name=profiles.get(wa_id),
            text=text,
            media=media,
            duration_ms=duration,
            reply_to_id=(m.get("context") or {}).get("id"),
            interactive_id=interactive_id,
            raw=m,
        )

    # -- media ------------------------------------------------------------- #
    async def download_media(self, ref: MediaRef) -> tuple[bytes, str]:
        url = ref.url
        mime = ref.mime or "application/octet-stream"
        if url is None:
            meta = await self.client.get(f"{self.base}/{ref.id}", headers=self._headers())
            meta.raise_for_status()
            j = meta.json()
            url, mime = j["url"], j.get("mime_type", mime)
        # The CDN URL still requires the bearer token.
        resp = await self.client.get(url, headers=self._headers())
        resp.raise_for_status()
        return resp.content, mime

    # -- send -------------------------------------------------------------- #
    async def send(self, msg: OutboundMessage) -> SendResult:
        payload = self._build_payload(msg)
        try:
            resp = await self.client.post(
                f"{self.base}/{settings.wa_phone_number_id}/messages",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
            return SendResult(provider_message_id=body["messages"][0]["id"])
        except httpx.HTTPError as exc:
            log.error("wa_send_failed", provider=self.name, error=str(exc))
            return SendResult(provider_message_id=None, ok=False, error=str(exc))

    def _build_payload(self, msg: OutboundMessage) -> dict[str, Any]:
        base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": msg.to}
        if msg.kind == "text":
            return base | {"type": "text", "text": {"body": msg.text or "", "preview_url": False}}
        if msg.kind == "image":
            return base | {
                "type": "image",
                "image": {"link": msg.image_url, "caption": msg.caption or ""},
            }
        if msg.kind == "video":
            return base | {
                "type": "video",
                "video": {"link": msg.video_url, "caption": msg.caption or ""},
            }
        if msg.kind == "buttons":
            return base | {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": msg.text or ""},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": b.id, "title": b.title[:20]}}
                            for b in msg.buttons[:3]
                        ]
                    },
                },
            }
        raise ValueError(f"unsupported outbound kind {msg.kind}")

    async def mark_read(self, provider_message_id: str) -> None:
        try:
            await self.client.post(
                f"{self.base}/{settings.wa_phone_number_id}/messages",
                headers=self._headers(),
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": provider_message_id,
                },
            )
        except httpx.HTTPError:
            pass


__all__ = ["MetaAdapter", "Button"]
