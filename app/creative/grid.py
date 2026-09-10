"""The grid guard: does this brief belong on their Instagram?

A professionally run page has a signature you can see from the profile
grid: one aspect ratio, one or two layouts, a palette, a mood. A brand with
nine posts already has one whether they planned it or not. This module reads
the signature from what the owner has APPROVED, scores a new brief against
it, and -- when the brief would break the pattern -- says so with a concrete
alternative, before a credit is spent.

It is a guard, not a gate. The owner can always have the post they asked
for; they should never have it by accident.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Brief, Creative

# A signature needs this many approved posts before anything is "usual".
MIN_POSTS = 6
# Dominance: a value is the brand's signature when it holds this share.
DOMINANT = 0.7
LOOKBACK = 30
_WORD = re.compile(r"[a-z]+")
_NOISE = {"and", "the", "with", "very", "more", "less", "bit", "slightly"}

TEMPLATE_NAMES = {
    "centered_overlay": "big centred headline over the photo",
    "lower_third": "photo on top, words in the lower third",
    "split_card": "photo above a solid brand-colour panel",
    "top_band": "brand-colour band with the words on top, photo below",
    "poster_stack": "poster: big headline top-left over the photo",
    "frame_card": "catalogue card: framed photo, words beneath it",
}


@dataclass
class Fingerprint:
    posts: int = 0
    aspects: Counter = field(default_factory=Counter)
    templates: Counter = field(default_factory=Counter)
    formats: Counter = field(default_factory=Counter)
    moods: Counter = field(default_factory=Counter)
    headline_lengths: list[int] = field(default_factory=list)
    photo_share: float = 0.0

    @property
    def enough(self) -> bool:
        return self.posts >= MIN_POSTS

    def dominant(self, counter: Counter) -> str | None:
        if not counter:
            return None
        value, n = counter.most_common(1)[0]
        return value if n / max(1, self.posts) >= DOMINANT else None

    def top_moods(self, n: int = 4) -> list[str]:
        return [m for m, c in self.moods.most_common(n) if c >= 2]

    def describe(self) -> str:
        if not self.enough:
            return ""
        bits = []
        if a := self.dominant(self.aspects):
            bits.append(f"{a} posts")
        if t := self.dominant(self.templates):
            bits.append(TEMPLATE_NAMES.get(t, t))
        if moods := self.top_moods(3):
            bits.append("a " + ", ".join(moods) + " mood")
        if self.photo_share >= 0.6:
            bits.append("their own product photos")
        return "; ".join(bits)


@dataclass
class GridNote:
    deviations: list[str]
    suggested_changes: dict[str, Any]
    signature: str
    severity: int  # number of signature-level breaks

    def as_dict(self) -> dict[str, Any]:
        return {
            "deviations": self.deviations,
            "suggested_changes": self.suggested_changes,
            "their_usual_grid": self.signature,
            "severity": self.severity,
        }


def _moods(text: str | None) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if len(w) > 2 and w not in _NOISE]


def fingerprint(db: Session, brand_id: uuid.UUID) -> Fingerprint:
    """The signature of what the owner has approved or published, latest first."""
    briefs = db.scalars(
        select(Brief)
        .join(Creative, Creative.brief_id == Brief.id)
        .where(Brief.brand_id == brand_id, Creative.status.in_(("approved", "published")))
        .order_by(Brief.created_at.desc())
        .distinct()
        .limit(LOOKBACK)
    ).all()
    fp = Fingerprint(posts=len(briefs))
    with_photo = 0
    for b in briefs:
        p = b.payload or {}
        fmt = p.get("format") or {}
        vd = p.get("visual_direction") or {}
        fp.aspects[fmt.get("aspect_ratio", "1:1")] += 1
        fp.templates[p.get("template_id") or "centered_overlay"] += 1
        fp.formats[fmt.get("type", "single")] += 1
        for w in _moods(vd.get("mood")):
            fp.moods[w] += 1
        fp.headline_lengths.append(len(p.get("headline") or ""))
        if vd.get("reference_asset_id") or any(
            (s.get("visual_direction") or {}).get("reference_asset_id")
            for s in p.get("slides") or []
        ):
            with_photo += 1
    fp.photo_share = with_photo / max(1, fp.posts)
    return fp


def check(fp: Fingerprint, brief: Any) -> GridNote | None:
    """Compare a brief to the signature. None when it fits or the signature is too thin."""
    if not fp.enough:
        return None
    # A reel is a video: always 9:16, shown in its own tab, cropped to 4:5 in
    # the grid. Judging it against the still grid's ratio would flag every reel.
    if getattr(brief, "is_reel", None) and brief.is_reel():
        return None
    deviations: list[str] = []
    changes: dict[str, Any] = {}
    severity = 0

    aspect = brief.format.aspect_ratio
    usual_aspect = fp.dominant(fp.aspects)
    if usual_aspect and aspect != usual_aspect:
        deviations.append(
            f"this is {aspect}; their last {fp.posts} approved posts are {usual_aspect}, and mixed "
            "ratios make the profile grid look uneven"
        )
        changes["format.aspect_ratio"] = usual_aspect
        severity += 1

    template = brief.template_id
    usual_template = fp.dominant(fp.templates)
    if usual_template and template != usual_template:
        deviations.append(
            f"layout '{TEMPLATE_NAMES.get(template, template)}' differs from their usual "
            f"'{TEMPLATE_NAMES.get(usual_template, usual_template)}'"
        )
        changes["template_id"] = usual_template
        severity += 1

    top = fp.top_moods()
    mine = set(_moods(brief.visual_direction.mood))
    if top and mine and not (mine & set(top)):
        deviations.append(
            f"the picture mood ('{brief.visual_direction.mood}') is unlike their usual "
            f"({', '.join(top)})"
        )
        changes["visual_direction.mood"] = ", ".join(top[:2])

    if fp.headline_lengths:
        avg = sum(fp.headline_lengths) / len(fp.headline_lengths)
        if len(brief.headline) > max(24, avg * 1.7):
            deviations.append(
                f"the headline is {len(brief.headline)} characters; theirs average {avg:.0f}, "
                "and long headlines shrink on the grid thumbnail"
            )
            changes["headline"] = "shorten to their usual length"

    if not deviations:
        return None
    return GridNote(deviations, changes, fp.describe(), severity)


def adjusted_payload(payload: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Apply the suggested changes to a brief payload (copy), for the agent to reuse."""
    out = {**payload, "format": {**(payload.get("format") or {})}}
    out["visual_direction"] = {**(payload.get("visual_direction") or {})}
    if "format.aspect_ratio" in changes:
        out["format"]["aspect_ratio"] = changes["format.aspect_ratio"]
    if "template_id" in changes:
        out["template_id"] = changes["template_id"]
    if "visual_direction.mood" in changes:
        out["visual_direction"]["mood"] = changes["visual_direction.mood"]
    return out
