"""Provider-agnostic channel contract.

Everything above this line (ingestion, agent, replies) speaks `InboundMessage`
and `OutboundMessage`. Swapping Meta Cloud API for Gupshup or Twilio is one
file in `channels/whatsapp/adapters/` and one env var.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

MessageKind = Literal[
    "text", "audio", "image", "video", "document", "interactive",
    "location", "sticker", "system", "unsupported",
]


@dataclass(slots=True)
class MediaRef:
    """How to fetch the media. Providers differ: Meta gives an id you resolve
    against the Graph API, Twilio gives a URL behind basic auth, Gupshup gives
    a plain URL."""

    id: str | None = None
    url: str | None = None
    mime: str | None = None
    size_bytes: int | None = None


@dataclass(slots=True)
class InboundMessage:
    provider: str
    provider_message_id: str
    wa_id: str
    kind: MessageKind
    timestamp: datetime
    profile_name: str | None = None
    text: str | None = None
    media: MediaRef | None = None
    duration_ms: int | None = None
    reply_to_id: str | None = None
    interactive_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Button:
    id: str
    title: str  # <= 20 chars on WhatsApp


@dataclass(slots=True)
class OutboundMessage:
    to: str
    kind: Literal["text", "image", "buttons"] = "text"
    text: str | None = None
    image_url: str | None = None
    caption: str | None = None
    buttons: list[Button] = field(default_factory=list)


@dataclass(slots=True)
class SendResult:
    provider_message_id: str | None
    ok: bool = True
    error: str | None = None


@runtime_checkable
class ChannelAdapter(Protocol):
    name: str

    def verify_webhook(self, params: dict[str, str]) -> str | None:
        """Return the challenge string to echo, or None to reject."""

    def verify_signature(self, raw_body: bytes, headers: dict[str, str]) -> bool: ...

    def parse(self, body: dict[str, Any], headers: dict[str, str]) -> list[InboundMessage]: ...

    async def download_media(self, ref: MediaRef) -> tuple[bytes, str]: ...

    async def send(self, msg: OutboundMessage) -> SendResult: ...

    async def mark_read(self, provider_message_id: str) -> None: ...
