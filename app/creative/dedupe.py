"""Catch two slides of the same carousel that came back as the same picture.

The shot ladder in `shotplan` is prevention: it asks the model for six
different photographs. This is detection, for when it asks and the model does
it anyway -- which happens, because a short brief ("new stock arrived") gives
every slide near-identical subject matter and the model falls back on its
favourite composition.

Perceptual hashing, not byte comparison. Two renders of the same scene with
different seeds are never byte-identical and often not even close in file
size, but they are obviously the same picture to a person, which is the only
judgement that matters here. A dHash over an 8x9 greyscale thumbnail gives a
64-bit fingerprint where Hamming distance tracks how similar two images look.

The threshold is deliberately loose. Refusing a slide costs one regeneration;
shipping a carousel where slides 2 and 4 are the same photograph is the kind
of thing an owner notices immediately and does not forgive, because it is the
single clearest sign that nobody looked at it.

No new dependency: Pillow is already in the image path.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from app.logging import get_logger

log = get_logger(__name__)

# dHash grid: 9 columns give 8 horizontal comparisons per row.
_W, _H = 9, 8

# Hamming distance at or below which two 64-bit hashes are "the same picture".
# Calibrated on the failure this exists to catch: two FLUX renders of one
# prompt with different seeds land around 4-9; genuinely different shots of
# one subject land above 14. 10 sits in the gap, nearer the failure side.
DUPLICATE_DISTANCE = 10


@dataclass(frozen=True, slots=True)
class Duplicate:
    position: int  # the slide to redo
    matches: int  # the earlier slide it copied
    distance: int


def dhash(image: bytes) -> int:
    """A 64-bit perceptual fingerprint. Raises nothing the caller must handle."""
    from PIL import Image

    with Image.open(BytesIO(image)) as im:
        small = im.convert("L").resize((_W, _H), Image.LANCZOS)
        px = small.tobytes()  # one byte per pixel in mode L, row-major
    bits = 0
    for row in range(_H):
        base = row * _W
        for col in range(_W - 1):
            bits = (bits << 1) | int(px[base + col] < px[base + col + 1])
    return bits


def distance(a: int, b: int) -> int:
    return int(a ^ b).bit_count()


def find_duplicates(
    hashes: dict[int, int], *, threshold: int = DUPLICATE_DISTANCE
) -> list[Duplicate]:
    """Slides that repeat an earlier slide, in position order.

    `hashes` maps slide position -> dhash. The EARLIER slide always wins: the
    first slide is the feed thumbnail and carries the post, so when two
    collide it is the later one that gets redone.
    """
    out: list[Duplicate] = []
    positions = sorted(hashes)
    for i, pos in enumerate(positions):
        for earlier in positions[:i]:
            d = distance(hashes[pos], hashes[earlier])
            if d <= threshold:
                out.append(Duplicate(position=pos, matches=earlier, distance=d))
                break  # one report per slide; it only needs redoing once
    return out


def report(hashes: dict[int, int], *, threshold: int = DUPLICATE_DISTANCE) -> dict:
    """Telemetry for one carousel: how varied it actually came out.

    `min_distance` is the useful number to watch over time. A brand whose
    carousels trend towards the threshold is a brand whose briefs are too thin
    to carry six slides -- the fix there is asking the owner one more question,
    not another regeneration.
    """
    dupes = find_duplicates(hashes, threshold=threshold)
    positions = sorted(hashes)
    pairs = [
        distance(hashes[a], hashes[b]) for i, a in enumerate(positions) for b in positions[i + 1 :]
    ]
    return {
        "slides": len(hashes),
        "duplicates": [{"position": d.position, "matches": d.matches} for d in dupes],
        "min_distance": min(pairs) if pairs else None,
        "mean_distance": round(sum(pairs) / len(pairs), 1) if pairs else None,
    }
