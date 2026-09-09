"""In-memory adapter. Used by tests and by `WA_PROVIDER=mock` local runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.channels.base import InboundMessage, MediaRef, OutboundMessage, SendResult
from app.config import settings


class MockAdapter:
    name = "mock"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self.media: dict[str, tuple[bytes, str]] = {}

    def verify_webhook(self, params: dict[str, str]) -> str | None:
        if params.get("hub.verify_token") == settings.wa_verify_token:
            return params.get("hub.challenge")
        return None

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        return True

    def parse(self, body: dict[str, Any], headers: dict[str, str]) -> list[InboundMessage]:
        msgs = body.get("messages") or [body]
        out = []
        for m in msgs:
            media = None
            if m.get("media_id") or m.get("media_url"):
                media = MediaRef(
                    id=m.get("media_id"),
                    url=m.get("media_url"),
                    mime=m.get("mime", "audio/ogg"),
                )
            out.append(
                InboundMessage(
                    provider=self.name,
                    provider_message_id=m.get("id", f"mock-{datetime.now(UTC).timestamp()}"),
                    wa_id=m.get("from", "919999999999"),
                    kind=m.get("type", "text"),
                    timestamp=datetime.now(UTC),
                    profile_name=m.get("name", "Test Shop"),
                    text=m.get("text"),
                    media=media,
                    interactive_id=m.get("interactive_id"),
                    raw=m,
                )
            )
        return out

    async def download_media(self, ref: MediaRef) -> tuple[bytes, str]:
        key = ref.id or ref.url or ""
        return self.media.get(key, (b"\x00\x00", ref.mime or "audio/ogg"))

    async def send(self, msg: OutboundMessage) -> SendResult:
        self.sent.append(msg)
        return SendResult(provider_message_id=f"mock-out-{uuid.uuid4()}")

    async def mark_read(self, provider_message_id: str) -> None:
        return None
