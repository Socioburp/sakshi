"""One shot per slide, so a carousel reads as a photo shoot and not a repeat.

The problem this exists to fix
-----------------------------
`photoreal.CAMERA_DIRECTION` is a single fixed clause -- 50mm prime, f/2.0,
daylight from one side, shallow depth of field -- and it was appended to the
prompt of EVERY slide of EVERY carousel for EVERY brand. Same lens, same
aperture, same light direction, same distance, six times in a row. The model
did exactly what it was told, so slide 2 came back as a near-copy of slide 1
and the carousel looked like one picture printed six times.

Two things have to be true at once, and they pull in opposite directions:

  cohesion   the six slides must look like one shoot for one brand -- same
             light quality, same colour grade, same surfaces. That is the
             ART DIRECTION, and it is decided ONCE per brief and shared.
  variety    each slide must be a different photograph -- different distance,
             different lens, different angle. That is the SHOT, and it is
             assigned per slide position.

A real photographer covers a subject exactly this way: they do not change the
light between frames, they change where they stand. So the art direction is
locked and only the camera moves.

Ordering is not decorative. Slide 1 is the thumbnail in the feed and has to
carry the whole post on its own, so it gets the establishing frame. The last
slide carries the CTA (compose.py only shows the CTA there), so it gets the
calmest, most graphic frame with room for type.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# the shot ladder
# --------------------------------------------------------------------------- #
# Ordered by camera distance, which is the axis a viewer notices first. Each
# entry changes lens, distance AND angle together -- changing only one of the
# three still reads as the same photograph.


@dataclass(frozen=True, slots=True)
class Shot:
    key: str
    camera: str  # replaces the fixed CAMERA_DIRECTION lens clause
    framing: str  # what is in the frame and how it sits
    copy_space: str  # where the compositor's type will go, so the model leaves it calm


LADDER: tuple[Shot, ...] = (
    Shot(
        "establishing",
        "35mm at f/4, most of the frame in focus",
        "the subject in its setting, seen whole, a little distance from the camera",
        "keep the upper third quiet and uncluttered",
    ),
    Shot(
        "hero",
        "50mm prime at f/2.0, shallow depth of field with soft falloff",
        "the subject filling the middle of the frame, three-quarter view, eye level",
        "keep the lower third quiet and uncluttered",
    ),
    Shot(
        "detail",
        "90mm macro at f/2.8, focus on one small area, the rest falling away",
        "20cm from the surface: texture, edge, weave or grain, cropped in hard",
        "keep one side of the frame soft and free of detail",
    ),
    Shot(
        "overhead",
        "50mm at f/5.6 looking straight down, flat even light",
        "a top-down arrangement on a plain surface, objects spaced apart, not touching",
        "leave clear empty surface along the top edge",
    ),
    Shot(
        "in_context",
        "28mm at f/2.8 from slightly below, a step back",
        "hands or a person using the subject, caught mid-action, the room visible behind",
        "keep the left half quiet and uncluttered",
    ),
    Shot(
        "graphic",
        "85mm at f/8, flat frontal light, no falloff",
        "the subject small and centred against a broad plain field of one colour",
        "leave the whole lower half as clean empty background",
    ),
)

SHOT_BY_KEY = {s.key: s for s in LADDER}

# Which rungs to use, in order, for a carousel of N slides. Not simply the
# first N: a 2-slide carousel wants the widest contrast it can get (whole
# thing, then close), while a 6-slide one walks the ladder properly. The last
# entry is always `graphic` -- that is the slide the CTA lands on.
ORDERS: dict[int, tuple[str, ...]] = {
    1: ("hero",),
    2: ("hero", "detail"),
    3: ("establishing", "detail", "graphic"),
    4: ("establishing", "hero", "detail", "graphic"),
    5: ("establishing", "hero", "detail", "in_context", "graphic"),
    6: ("establishing", "hero", "detail", "overhead", "in_context", "graphic"),
}

# --------------------------------------------------------------------------- #
# the art direction lock
# --------------------------------------------------------------------------- #
# Everything here is decided once per brief and repeated verbatim on every
# slide. This is what keeps six different photographs looking like one shoot.
ART_DIRECTION = (
    "natural directional daylight from one side, true-to-life colour, "
    "fine natural grain, real surfaces with small imperfections left in, "
    "candid editorial photography"
)


def order_for(slide_count: int) -> tuple[str, ...]:
    n = max(1, min(6, int(slide_count or 1)))
    return ORDERS.get(n, ORDERS[6])


def shot_for(position: int, slide_count: int) -> Shot:
    """The shot for slide `position` (1-based) of a `slide_count` carousel."""
    order = order_for(slide_count)
    idx = max(1, min(len(order), int(position or 1))) - 1
    return SHOT_BY_KEY[order[idx]]


def camera_clause(position: int, slide_count: int) -> str:
    """The full camera + framing + copy-space clause for one slide.

    Starts with photoreal's marker so the enrichment stays idempotent and the
    providers' prompt trimming still knows where the camera clause begins.
    """
    shot = shot_for(position, slide_count)
    return (
        f"shot on a full-frame camera with a {shot.camera}, "
        f"{shot.framing}, {shot.copy_space}, {ART_DIRECTION}"
    )


# --------------------------------------------------------------------------- #
# seeds
# --------------------------------------------------------------------------- #
# Two slides that share a seed AND share most of their prompt come back nearly
# identical. Nothing was setting a seed per slide, so the vendor picked one at
# random per call -- which sounds like variety but is not: random seeds with a
# near-identical prompt still converge on the same composition, and they make
# a rerun unreproducible, so "regenerate slide 3" could not be reasoned about.
#
# Derive it instead: stable for a given brief+slide (so a recompose is
# repeatable), far apart between slides (so they diverge).
SEED_MAX = 2**31 - 1


def seed_for(brief_key: str, position: int, *, salt: int = 0) -> int:
    """A deterministic, well-separated seed for one slide."""
    raw = f"{brief_key}|{position}|{salt}".encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=4).digest(), "big") % SEED_MAX


def brief_key(brief) -> str:  # noqa: ANN001 - CreativeBrief, avoid an import cycle
    """A stable identity for a brief, for seeding.

    The headline and CTA are what the owner actually asked for; hashing them
    means the same request reproduces the same pictures, and an edited request
    gets fresh ones.
    """
    bits = [getattr(brief, "headline", "") or "", getattr(brief, "cta", "") or ""]
    for sl in getattr(brief, "slides", []) or []:
        bits.append(getattr(sl, "headline", "") or "")
    return hashlib.blake2b("|".join(bits).encode(), digest_size=8).hexdigest()


# --------------------------------------------------------------------------- #
# applying the plan
# --------------------------------------------------------------------------- #
def apply(brief) -> list[dict]:  # noqa: ANN001 - CreativeBrief
    """Give every slide its own shot and its own seed. Mutates the brief.

    Returns one row per slide for the telemetry, so a flat-looking carousel
    can be traced back to the plan that produced it.

    Only fills what the agent left empty: a `seed` the model chose on purpose
    (regenerating one slide with a known seed) is never overwritten.
    """
    slides = list(getattr(brief, "slides", []) or [])
    if not slides:
        return []
    key = brief_key(brief)
    n = len(slides)
    plan = []
    for sl in slides:
        pos = int(getattr(sl, "position", 0) or 0)
        shot = shot_for(pos, n)
        vd = sl.visual_direction
        if vd.seed is None:
            vd.seed = seed_for(key, pos)
        plan.append({"position": pos, "shot": shot.key, "seed": vd.seed})
    return plan
