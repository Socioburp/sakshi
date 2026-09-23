from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

GEN_EDGE_MULTIPLE = 16

# The generation frame is sized for the WINDOW the layout shows, so the
# picture is never cover-cropped into it. A window is rarely a ratio that two
# multiples of 16 can hit exactly, so the search takes the frame that comes
# closest and lets fit_background trim what is left -- at most CROP_TOLERANCE
# pixels of the window, which is what "not cropped" means at 1080 wide.
CROP_TOLERANCE = 2.0
# How far above the window the frame is generated, as the post's own
# 1600x2000 over 1080x1350 (1.48x): the compositor Lanczos-resamples it down,
# which is where the sharpness of the type-free picture comes from. The
# search runs between these two and settles nearest the target; below the
# floor the picture is being enlarged, above the ceiling it is being paid for
# and thrown away.
OVERSAMPLE_MIN, OVERSAMPLE_MAX = 1.30, 1.60
# Every vendor's limits, taken together: OpenAI's floor and its largest
# non-experimental frame (2560x1440), inside BFL's 4MP ceiling, edges under
# 3840, ratio under 3:1. A frame outside these is never asked for.
GEN_MIN_PIXELS = 655_360
GEN_MAX_PIXELS = 2560 * 1440
GEN_MAX_EDGE = 3840
GEN_MAX_RATIO = 3.0


def _snap(n: float) -> int:
    return max(GEN_EDGE_MULTIPLE, int(round(n / GEN_EDGE_MULTIPLE)) * GEN_EDGE_MULTIPLE)


def crop_for(size: tuple[int, int], window: tuple[int, int]) -> float:
    """Pixels of the WINDOW lost when `size` is scaled to cover it (the larger
    axis's overhang), 0.0 when the ratios agree."""
    (gw, gh), (bw, bh) = size, window
    scale = max(bw / gw, bh / gh)
    return max(gw * scale - bw, gh * scale - bh)


def _legal(w: int, h: int) -> bool:
    return (
        w % GEN_EDGE_MULTIPLE == 0
        and h % GEN_EDGE_MULTIPLE == 0
        and max(w, h) <= GEN_MAX_EDGE
        and max(w, h) / min(w, h) <= GEN_MAX_RATIO
        and GEN_MIN_PIXELS <= w * h <= GEN_MAX_PIXELS
    )


def _configured() -> tuple[int, int]:
    from app.config import settings

    raw = settings.imagegen_size
    try:
        gw, gh = (int(v) for v in raw.lower().split("x"))
    except ValueError as exc:
        raise ValueError(f"IMAGEGEN_SIZE must be WIDTHxHEIGHT, got {raw!r}") from exc
    if gw % GEN_EDGE_MULTIPLE or gh % GEN_EDGE_MULTIPLE:
        raise ValueError(f"IMAGEGEN_SIZE {gw}x{gh}: both edges must be multiples of 16")
    return gw, gh


def generation_size_for_box(window: tuple[int, int]) -> tuple[int, int]:
    """The frame a picture is GENERATED at to fill `window` (pixels) uncropped.

    Same ratio as the window as nearly as multiples of 16 allow, both edges
    above the window's, inside every vendor's limits. A frame that hits the
    ratio exactly (1440x2560 for a 1080x1920 story) is preferred over one
    that is nearer the oversampling target but needs a trim; where no exact
    frame exists (a 892x652 card window) the trim is under CROP_TOLERANCE.
    """
    bw, bh = int(window[0]), int(window[1])
    if bw <= 0 or bh <= 0:
        raise ValueError(f"window must be positive, got {bw}x{bh}")
    from app.creative.brief import POST_SIZE

    target = _configured()[0] / POST_SIZE[0]
    best: tuple[tuple[bool, float, float], tuple[int, int]] | None = None
    lo = _snap(math.ceil(bw * OVERSAMPLE_MIN))
    hi = _snap(bw * OVERSAMPLE_MAX)
    for w in range(lo, hi + 1, GEN_EDGE_MULTIPLE):
        for h in (_snap(w * bh / bw), _snap(w * bh / bw) + GEN_EDGE_MULTIPLE):
            if w < bw or h < bh or not _legal(w, h):
                continue
            crop = crop_for((w, h), (bw, bh))
            cost = (crop > CROP_TOLERANCE, round(crop, 2), abs(w - bw * target))
            if best is None or cost < best[0]:
                best = (cost, (w, h))
    if best is None:
        raise ValueError(f"no legal generation frame covers a {bw}x{bh} window")
    return best[1]


def generation_size(export: tuple[int, int]) -> tuple[int, int]:
    """The size a picture is GENERATED at for a given export size.

    A post (4:5) is generated natively at `settings.imagegen_size` -- same
    ratio, more pixels -- and Lanczos-resampled down to the export size by the
    compositor. It is never generated at a preset and cropped. Any other ratio
    (a story's or reel's 9:16) gets the same-ratio oversampled frame from
    `generation_size_for_box`: 1440x2560, not the 1088x1920 that was trimmed
    by 4px a side and delivered with no oversampling at all.
    """
    w, h = export
    gw, gh = _configured()
    if gw * h == gh * w:
        if gw < w:
            raise ValueError(f"IMAGEGEN_SIZE {gw}x{gh} is below the {w}x{h} export")
        return gw, gh
    return generation_size_for_box((w, h))


def generation_size_for_window(window: tuple[int, int], export: tuple[int, int]) -> tuple[int, int]:
    """The frame for one slide: the configured post frame when the layout
    shows the whole canvas, the window's own frame when it shows less."""
    if tuple(window) == tuple(export):
        return generation_size(export)
    return generation_size_for_box(window)


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
