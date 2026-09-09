"""Real photograph first, generated background second.

The owner's own photo of their own product is the most real image that can
possibly go in their creative, it costs nothing, and it is already sitting in
`brand_assets` once they have sent a few. The brief has always had a slot for
it -- `visual_direction.reference_asset_id` -- but filling that slot was left to
the agent, which meant it happened only when the model remembered to look.

So the choice is made here instead, deterministically, after the brief is
validated and before anything is charged:

  explicit reference_asset_id  ->  always wins; the agent (or the owner) asked
  no reference, a photo matches ->  use the photo, free
  no reference, nothing matches ->  generate, and charge for it

Matching is deliberately literal: the words the owner used when they sent the
photo, against the words in this slide. A wrong photograph is worse than a
generated one -- it shows a product they are not selling today -- so a slide
with no real overlap generates instead.

Two rules keep that bar honest, and both exist because a looser version picked
a photo of a shopfront for a post about mangoes:

* **The copy counts double.** `visual_direction.prompt` is a long scene
  description full of words like "light", "warm" and "soft". The headline is
  what the owner is actually selling. A word shared with the headline is
  evidence; a word shared with the lighting notes is a coincidence.
* **One word is never enough.** MIN_SCORE is 2, so a single generic collision
  cannot spend a slide on the wrong photograph.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

# Photos of the real business. "logo" is excluded: the mark is composited as a
# lockup, never used as a background.
USABLE_KINDS = {"product", "shop", "team", "packaging", "ingredient"}

# Below this, upscaling shows before the creative does.
MIN_SHORT_EDGE = 800

# Weighted so a headline match outranks a scene-description match, and the bar
# is set above a single accidental word.
COPY_WEIGHT = 2
DIRECTION_WEIGHT = 1
MIN_SCORE = 2

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "and", "for", "with", "our", "your", "this", "that", "from", "into", "onto",
    "new", "now", "get", "buy", "off", "all", "one", "two", "best", "top", "more",
    "photo", "photos", "picture", "pictures", "image", "images", "shot", "pic", "img",
    "jpg", "jpeg", "png", "final", "copy", "edit", "sale", "offer", "today", "week",
    "weekend", "special", "free", "order", "shop", "store", "post", "insta", "instagram",
}


class _Asset(Protocol):
    id: Any
    kind: str
    label: str | None
    width: int | None
    height: int | None


def _tokens(*chunks: str | None) -> set[str]:
    out: set[str] = set()
    for chunk in chunks:
        if not chunk:
            continue
        for w in _WORD.findall(chunk.lower()):
            if len(w) > 2 and w not in _STOP:
                out.add(_singular(w))
    return out


def _singular(w: str) -> str:
    """Crude but symmetric: both sides of the comparison get the same treatment.

    Only a single trailing "s", and never after another "s" -- rstrip("s") turned
    "glass" into "gla" and quietly changed what matched.
    """
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def is_usable(asset: _Asset) -> bool:
    if (asset.kind or "").lower() not in USABLE_KINDS:
        return False
    w, h = asset.width or 0, asset.height or 0
    # Unknown dimensions are allowed through: older rows predate the columns and
    # refusing them would silently disable the whole free lane for those brands.
    if w and h and min(w, h) < MIN_SHORT_EDGE:
        return False
    return True


def score(copy: str, direction: str, asset: _Asset) -> int:
    """Weighted words shared between this slide and the photo's label."""
    label = _tokens(asset.label, asset.kind)
    return (
        COPY_WEIGHT * len(_tokens(copy) & label)
        + DIRECTION_WEIGHT * len(_tokens(direction) & label)
    )


def choose(copy: str, assets: list[_Asset], direction: str = "") -> Any | None:
    """Best real photograph for this slide, or None to fall through to the model."""
    ranked = [(score(copy, direction, a), a) for a in assets if is_usable(a)]
    ranked = [(s, a) for s, a in ranked if s >= MIN_SCORE]
    if not ranked:
        return None
    best = max(s for s, _ in ranked)
    # Ties go to the largest photograph: same relevance, more pixels to crop from.
    winners = [a for s, a in ranked if s == best]
    winners.sort(key=lambda a: ((a.width or 0) * (a.height or 0)), reverse=True)
    return winners[0].id


def copy_text(slide: Any, brief: Any) -> str:
    """What the owner is selling: the words a person will read."""
    return " ".join(
        str(x)
        for x in (
            getattr(slide, "headline", None) or getattr(brief, "headline", None),
            getattr(slide, "subhead", None),
        )
        if x
    )


def direction_text(slide: Any) -> str:
    """How the picture was described. Corroborating evidence, never the case."""
    vd = getattr(slide, "visual_direction", None)
    return " ".join(
        str(x) for x in (getattr(vd, "prompt", None), getattr(vd, "mood", None)) if x
    )
