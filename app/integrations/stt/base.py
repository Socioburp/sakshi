from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class Transcript:
    text: str
    provider: str
    language: str | None = None
    confidence: float | None = None
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SttProvider(Protocol):
    name: str

    async def transcribe(
        self, audio: bytes, mime: str, hint_languages: list[str] | None = None
    ) -> Transcript: ...
