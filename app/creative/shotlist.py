"""Photo coaching: which photographs a brand needs, and which it still lacks.

Every creative is bounded by the photographs the owner sends. An agency
solves that on day one with a shot list and a "photo day": five specific
pictures, taken by a window, three angles each. This module is that shot
list, per kind of business, plus a coverage score over the photos already
on file so the agent asks for the ONE photo that is missing rather than
"send more photos".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.creative.photoreal import _has_word


@dataclass(slots=True)
class Shot:
    key: str
    title: str
    how: str  # one line the owner can act on
    kinds: tuple[str, ...]  # asset kinds that satisfy it
    words: tuple[str, ...] = ()  # label words that satisfy it (any kind)


PRODUCT_SHOTS = [
    Shot(
        "hero",
        "Hero shot",
        "The product alone on a plain surface, by a window, phone at product height",
        ("product",),
    ),
    Shot(
        "in_use",
        "In use",
        "Someone using or holding it -- hands are enough",
        ("product", "other", "team"),
        ("using", "use", "hand", "hands", "holding", "wearing", "applying", "pouring"),
    ),
    Shot(
        "detail",
        "Close-up",
        "Texture, label or stitching from 20 cm away, tap to focus",
        ("product", "other"),
        ("close", "closeup", "close-up", "detail", "texture", "label", "macro"),
    ),
    Shot(
        "range",
        "The range",
        "3-6 products together, flat on a table, shot from above",
        ("product", "other"),
        ("range", "all", "collection", "flat", "flatlay", "set", "variants", "flavours"),
    ),
    Shot("shop", "The shop", "Shopfront or interior, daytime, straight-on", ("shop",)),
    Shot("team", "The people", "You or your team at work, candid", ("team",)),
]

FOOD_SHOTS = [
    Shot(
        "hero",
        "The dish",
        "One plate, top-down, natural light, no flash",
        ("product",),
    ),
    Shot(
        "detail",
        "Close-up",
        "45 degrees, 20 cm away, steam or texture visible",
        ("product", "other"),
        ("close", "closeup", "close-up", "detail", "texture", "macro"),
    ),
    Shot(
        "making",
        "The making",
        "Hands cooking, the tawa, the oven -- movement",
        ("team", "other", "product"),
        ("making", "cooking", "kitchen", "prep", "process", "baking", "frying"),
    ),
    Shot(
        "range",
        "The spread",
        "Several dishes together on a table",
        ("product", "other"),
        ("spread", "range", "menu", "thali", "platter", "all", "collection"),
    ),
    Shot("shop", "The shop", "Shopfront or seating, daytime", ("shop",)),
    Shot("team", "The people", "The cook, the owner, the counter -- candid", ("team",)),
]

SERVICE_SHOTS = [
    Shot("shop", "The place", "Shopfront or interior, straight-on, daytime", ("shop",)),
    Shot("team", "The people", "You or the team at work, candid, faces visible", ("team",)),
    Shot(
        "work",
        "The work",
        "Hands doing the job: the chair, the tools, the screen",
        ("other", "product", "team"),
        ("work", "working", "service", "tools", "equipment", "session", "class"),
    ),
    Shot(
        "result",
        "Before / after",
        "Same angle, same light, two photos",
        ("other", "product"),
        ("before", "after", "result", "results", "done", "finished"),
    ),
    Shot(
        "customer",
        "A happy customer",
        "With permission -- a smile at the counter",
        ("other", "team"),
        ("customer", "client", "happy", "review"),
    ),
]

APPAREL_SHOTS = [
    Shot("hero", "Flat-lay", "One piece laid flat on a plain sheet, shot from above", ("product",)),
    Shot(
        "worn",
        "On a person",
        "Someone wearing it, full length, by a window",
        ("product", "team", "other"),
        ("wearing", "worn", "model", "on", "styled", "look"),
    ),
    Shot(
        "detail",
        "Fabric close-up",
        "Weave, embroidery or print from 20 cm",
        ("product", "other"),
        ("close", "closeup", "close-up", "detail", "fabric", "embroidery", "print", "weave"),
    ),
    Shot(
        "range",
        "The rack",
        "The collection on a rack or shelf",
        ("product", "shop", "other"),
        ("rack", "range", "collection", "shelf", "all", "new arrivals"),
    ),
    Shot("shop", "The shop", "Interior or shopfront, daytime", ("shop",)),
]

_CATEGORY_SHOTS: list[tuple[tuple[str, ...], list[Shot]]] = [
    (
        (
            "restaurant",
            "cafe",
            "bakery",
            "sweets",
            "sweet",
            "food",
            "tiffin",
            "catering",
            "snacks",
            "biryani",
            "pizza",
            "dhaba",
            "hotel",
        ),
        FOOD_SHOTS,
    ),
    (
        ("cloth", "apparel", "boutique", "saree", "sari", "kurta", "fashion", "garment", "dress"),
        APPAREL_SHOTS,
    ),
    (
        (
            "salon",
            "spa",
            "clinic",
            "dental",
            "dentist",
            "physio",
            "gym",
            "fitness",
            "yoga",
            "coaching",
            "tuition",
            "academy",
            "institute",
            "classes",
            "repair",
            "service",
            "services",
            "studio",
            "consult",
            "consultant",
            "agency",
            "tailor",
            "photograph",
        ),
        SERVICE_SHOTS,
    ),
]


def shots_for(category: str | None) -> list[Shot]:
    cat = (category or "").lower()
    for keys, shots in _CATEGORY_SHOTS:
        if any(_has_word(cat, k) for k in keys):
            return shots
    return PRODUCT_SHOTS


def _satisfies(shot: Shot, kind: str | None, label: str | None) -> bool:
    kind = (kind or "").lower()
    words = (label or "").lower()
    if shot.words:
        if any(_has_word(words, w) for w in shot.words) and (not shot.kinds or kind in shot.kinds):
            return True
        # A plain product photo with no descriptive label is a hero shot, not
        # an in-use shot; the label decides for word-keyed shots.
        return False
    return kind in shot.kinds


def coverage(assets: list[Any], category: str | None) -> dict[str, Any]:
    """Which shots the photos on file cover, which are missing, and a score.

    `assets` are objects (or dicts) with `kind` and `label`.
    """
    shots = shots_for(category)
    have: list[str] = []
    for shot in shots:
        for a in assets:
            kind = a.get("kind") if isinstance(a, dict) else getattr(a, "kind", None)
            label = a.get("label") if isinstance(a, dict) else getattr(a, "label", None)
            if kind == "logo":
                continue
            if _satisfies(shot, kind, label):
                have.append(shot.key)
                break
    missing = [s for s in shots if s.key not in have]
    return {
        "have": have,
        "missing": [{"key": s.key, "title": s.title, "how": s.how} for s in missing],
        "score": round(len(have) / max(1, len(shots)), 2),
        "next": (
            {"key": missing[0].key, "title": missing[0].title, "how": missing[0].how}
            if missing
            else None
        ),
    }


def checklist(category: str | None, lang: str = "en") -> str:
    """The photo-day checklist as one WhatsApp message."""
    shots = shots_for(category)
    intro = {
        "hi": "Photo day! In 5 photos mein aapki saari posts ban jaayengi:",
        "en": "Photo day! These 5 photos will carry all your posts:",
    }.get(lang, "Photo day! These 5 photos will carry all your posts:")
    lines = [intro]
    for i, s in enumerate(shots[:5], 1):
        lines.append(f"{i}. {s.title} — {s.how}")
    lines.append(
        {
            "hi": "Khidki ke paas, din mein, flash band. Ek ek karke bhej dijiye.",
            "en": "By a window, in daylight, flash off. Send them one at a time.",
        }.get(lang, "By a window, in daylight, flash off. Send them one at a time.")
    )
    return "\n".join(lines)
