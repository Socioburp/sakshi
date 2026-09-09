from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class ImageRequest:
    prompt: str
    negative: str = ""
    width: int = 1080
    height: int = 1080
    seed: int | None = None
    style: str | None = None


@dataclass(slots=True)
class ImageResult:
    data: bytes
    mime: str
    provider: str
    job_id: str | None = None
    cost_micros: int = 0
    latency_ms: int = 0
    seed: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ImageProvider(Protocol):
    name: str
    cost_micros_per_image: int

    async def generate(self, req: ImageRequest) -> ImageResult: ...
