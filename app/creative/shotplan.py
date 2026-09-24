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


# A rung is the CAMERA only: lens, distance and angle. Where the words will
# sit is not a property of the slide's position -- it is a property of the
# layout the slide is set in, and of the copy measured on it -- so it is said
# by `copy_space_clause`, never here. It used to be here: the "hero" rung
# asked for the subject filling the middle of the frame and the lower third
# kept quiet, on a centered_overlay whose type runs through the middle.
@dataclass(frozen=True, slots=True)
class Shot:
    key: str
    camera: str  # replaces the fixed CAMERA_DIRECTION lens clause
    framing: str  # what is in the frame and how it sits


LADDER: tuple[Shot, ...] = (
    Shot(
        "establishing",
        "35mm at f/4, most of the frame in focus",
        "the subject in its setting, seen whole, a little distance from the camera",
    ),
    Shot(
        "hero",
        "50mm prime at f/2.0, shallow depth of field with soft falloff",
        "the subject seen whole, three-quarter view, eye level",
    ),
    # A texture study, not an object cut by the frame: the gate refuses a
    # subject cropped by an edge, and a rung that asked to crop "in hard"
    # bought six rejections a carousel before the picture it wanted.
    Shot(
        "detail",
        "90mm macro at f/2.8, focus on one small area, the rest falling away",
        "20cm from a surface: texture, edge, weave or grain filling the frame, no whole object",
    ),
    Shot(
        "overhead",
        "50mm at f/5.6 looking straight down, flat even light",
        "a top-down arrangement on a plain surface, objects spaced apart, not touching",
    ),
    Shot(
        "in_context",
        "28mm at f/2.8 from slightly below, a step back",
        "hands or a person using the subject, caught mid-action, the room visible behind",
    ),
    Shot(
        "graphic",
        "85mm at f/8, flat frontal light, no falloff",
        "the subject small against a broad plain field of one colour",
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


# --------------------------------------------------------------------------- #
# the shoot style: one per BRAND
# --------------------------------------------------------------------------- #
# ART_DIRECTION above was one string for every brand on the platform: the same
# side daylight, the same grade, the same "candid editorial". Inside a brand
# that sameness is the point. ACROSS brands it meant a sweet shop, a jeweller
# and a gym all came back from the same imaginary photographer, and a feed of
# our customers looked like one account.
#
# So the lock stays -- one art direction per brief, repeated on every slide --
# but WHICH one is a property of the brand: chosen once from its look, kept in
# template_prefs["shoot"], and the same on every post thereafter. Each string
# is kept no longer than ART_DIRECTION so the enrichment budget in photoreal.py
# is unchanged.
SHOOT_STYLES: dict[str, str] = {
    "daylight": ART_DIRECTION,
    "bright_airy": (
        "bright, airy high-key daylight through a sheer curtain, pale clean tones, "
        "soft open shadows, fine natural grain, fresh editorial photography"
    ),
    "moody": (
        "low-key light from one small window, deep shadows with rich blacks, muted "
        "jewel tones, fine natural grain, quiet luxury editorial photography"
    ),
    "warm_documentary": (
        "warm late-afternoon light, golden tones, lived-in surfaces left as they are, "
        "fine natural grain, unposed documentary photography"
    ),
    "graphic_studio": (
        "crisp studio light with one hard-edged shadow, saturated clean colour, bold "
        "simple shapes, fine natural grain, graphic advertising photography"
    ),
}
assert all(len(v) <= len(ART_DIRECTION) for v in SHOOT_STYLES.values()), "shoot style too long"

# Two candidates per look, so two brands that share a look can still differ.
STYLES_BY_LOOK: dict[str, tuple[str, ...]] = {
    "clean": ("bright_airy", "daylight"),
    "editorial": ("moody", "bright_airy"),
    "warm": ("warm_documentary", "daylight"),
    "bold": ("graphic_studio", "warm_documentary"),
}


def pick_style(look: str | None, brand_key: str) -> str:
    """A brand's shoot style: from its look, split by a stable hash of the brand."""
    options = STYLES_BY_LOOK.get((look or "").strip(), ("daylight",))
    digest = hashlib.blake2b((brand_key or "").encode(), digest_size=2).digest()
    return options[int.from_bytes(digest, "big") % len(options)]


def style_of(template_prefs: dict | None, brand_key: str = "") -> str:
    """The style to shoot in NOW: the stored one, else the one this brand would
    be given -- so brands created before this existed are consistent from their
    first post, without a backfill."""
    prefs = template_prefs or {}
    stored = prefs.get("shoot")
    return stored if stored in SHOOT_STYLES else pick_style(prefs.get("look"), brand_key)


def order_for(slide_count: int) -> tuple[str, ...]:
    n = max(1, min(6, int(slide_count or 1)))
    return ORDERS.get(n, ORDERS[6])


def shot_for(position: int, slide_count: int) -> Shot:
    """The shot for slide `position` (1-based) of a `slide_count` carousel."""
    order = order_for(slide_count)
    idx = max(1, min(len(order), int(position or 1))) - 1
    return SHOT_BY_KEY[order[idx]]


def camera_clause(position: int, slide_count: int, style: str | None = None) -> str:
    """The camera + framing + art-direction clause for one slide.

    Starts with photoreal's marker so the enrichment stays idempotent and the
    providers' prompt trimming still knows where the camera clause begins.
    """
    shot = shot_for(position, slide_count)
    return (
        f"shot on a full-frame camera with a {shot.camera}, "
        f"{shot.framing}, {SHOOT_STYLES.get(style or '', ART_DIRECTION)}"
    )


# --------------------------------------------------------------------------- #
# copy space: where the words will sit, from the LAYOUT
# --------------------------------------------------------------------------- #
# Layouts that set the words on a panel, a band or a card beside the picture:
# no word touches the photograph, so the model is free to fill its frame.
TYPE_BESIDE_PHOTO = frozenset({"split_card", "frame_card", "top_band"})
# Where the type block sits on the layouts that set it OVER the photograph,
# as fractions of the photo window (l, t, r, b) -- used when a slide's copy
# has not been measured yet. The measured box from the layout gate replaces
# these on every real job.
TYPE_ZONES: dict[str, tuple[float, float, float, float]] = {
    "centered_overlay": (0.08, 0.30, 0.92, 0.75),
    "lower_third": (0.08, 0.55, 0.85, 0.93),
    "poster_stack": (0.08, 0.07, 0.72, 0.50),
}
COPY_SPACE_MAX = 180


def copy_space_clause(
    template: str | None, text_box: tuple[float, float, float, float] | None = None
) -> str:
    """One sentence telling the image model where the compositor's words will
    sit, from the layout and the measured type block -- never from the rung.

    `text_box` is the words' box as fractions of the photo window. On a layout
    that sets type beside the picture there is nothing to keep clear and the
    subject may fill the frame.
    """
    name = (template or "").strip()
    if name in TYPE_BESIDE_PHOTO:
        return (
            "No words will be set on this picture: the subject sits whole and centred with "
            "a little room on every side"
        )
    box = text_box or TYPE_ZONES.get(name) or TYPE_ZONES["centered_overlay"]
    left, top, right, bottom = (min(1.0, max(0.0, v)) for v in box)
    top, bottom = round(top * 100), round(bottom * 100)
    width, centre = right - left, (left + right) / 2
    if width >= 0.70:
        across = "across the full width"
    elif centre < 0.45:
        across = "on the left"
    elif centre > 0.55:
        across = "on the right"
    else:
        across = "in the centre"
    if top >= 45:
        subject = "in the upper part of the frame, above that band"
    elif bottom <= 55:
        subject = "in the lower part of the frame, below that band"
    else:
        subject = "high or low in the frame, never across the middle"
    return (
        f"Keep the band from {top}% to {bottom}% of the height, {across}, calm and low in "
        f"detail for words to sit on; the subject sits {subject}"
    )


def _longest_copy_space() -> int:
    boxes = [None, (0.0, 0.5, 1.0, 1.0), (0.0, 0.0, 0.5, 0.5), (0.5, 0.25, 1.0, 0.75)]
    names = [*TYPE_BESIDE_PHOTO, *TYPE_ZONES, "other"]
    return max(len(copy_space_clause(n, b)) for n in names for b in boxes)


assert _longest_copy_space() <= COPY_SPACE_MAX, "copy-space clause too long"


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
