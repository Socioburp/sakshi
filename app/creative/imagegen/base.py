from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

GEN_EDGE_MULTIPLE = 16


def generation_size(export: tuple[int, int]) -> tuple[int, int]:
    """The size a picture is GENERATED at for a given export size.

    A post (4:5) is generated natively at `settings.imagegen_size` -- same
    ratio, more pixels -- and Lanczos-resampled down to the export size by the
    compositor. It is never generated at a preset and cropped. Any other ratio
    (a reel's 9:16) gets the smallest multiple-of-16 frame that covers it.
    """
    from app.config import settings

    w, h = export
    raw = settings.imagegen_size
    try:
        gw, gh = (int(v) for v in raw.lower().split("x"))
    except ValueError as exc:
        raise ValueError(f"IMAGEGEN_SIZE must be WIDTHxHEIGHT, got {raw!r}") from exc
    if gw % GEN_EDGE_MULTIPLE or gh % GEN_EDGE_MULTIPLE:
        raise ValueError(f"IMAGEGEN_SIZE {gw}x{gh}: both edges must be multiples of 16")
    if gw * h == gh * w:
        if gw < w:
            raise ValueError(f"IMAGEGEN_SIZE {gw}x{gh} is below the {w}x{h} export")
        return gw, gh

    def up(n: int) -> int:
        return -(-n // GEN_EDGE_MULTIPLE) * GEN_EDGE_MULTIPLE

    return up(w), up(h)


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
