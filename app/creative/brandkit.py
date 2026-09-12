"""A brand kit per client, so no two clients look like the same agency.

Three templates shared by every brand becomes a "template look" within a
month. A designer avoids that with a kit: a type pairing, a visual
signature (one small repeated mark), and a layout family the brand uses
most. The kit is chosen from the category the moment it is known, stored
on the brand, and can be changed with update_brand(look=...).

Faces are Google Fonts, all SIL Open Font License; Poppins carries
Devanagari, and app/creative/fonts.py adds a Noto face for any other
script in the copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.creative.photoreal import _has_word


@dataclass(frozen=True, slots=True)
class Look:
    key: str
    heading: str
    body: str
    signature: str  # rule | corner | bar | none
    family: tuple[str, ...]  # preferred templates, first is the default
    note: str
    categories: tuple[str, ...] = field(default_factory=tuple)


LOOKS: dict[str, Look] = {
    "clean": Look(
        key="clean",
        heading="Poppins",
        body="Inter",
        signature="rule",
        family=("lower_third", "top_band", "centered_overlay"),
        note="modern, friendly, works for almost anything",
    ),
    "editorial": Look(
        key="editorial",
        heading="Playfair Display",
        body="Inter",
        signature="none",
        family=("frame_card", "poster_stack", "lower_third"),
        note="premium, quiet -- skincare, jewellery, boutiques, decor",
        categories=(
            "skin",
            "skincare",
            "cosmetic",
            "beauty",
            "jewel",
            "jewellery",
            "jewelry",
            "jeweller",
            "jeweler",
            "gold",
            "silver",
            "diamond",
            "boutique",
            "saree",
            "sari",
            "decor",
            "interior",
            "furniture",
            "perfume",
            "spa",
            "premium",
            "luxury",
        ),
    ),
    "warm": Look(
        key="warm",
        heading="Fraunces",
        body="Inter",
        signature="corner",
        family=("split_card", "lower_third", "frame_card"),
        note="warm, homely -- food, bakery, sweets, tiffin, cafe",
        categories=(
            "food",
            "restaurant",
            "cafe",
            "bakery",
            "sweets",
            "sweet",
            "tiffin",
            "catering",
            "snacks",
            "oil",
            "oils",
            "spice",
            "spices",
            "pickle",
            "pickles",
            "dairy",
            "honey",
            "ghee",
            "chocolate",
            "tea",
            "coffee",
        ),
    ),
    "bold": Look(
        key="bold",
        heading="Manrope",
        body="Manrope",
        signature="bar",
        family=("top_band", "poster_stack", "split_card"),
        note="loud, offer-led -- electronics, mobile, gym, coaching, hardware",
        categories=(
            "electronic",
            "electronics",
            "mobile",
            "phone",
            "gadget",
            "laptop",
            "appliance",
            "gym",
            "fitness",
            "coaching",
            "tuition",
            "academy",
            "institute",
            "hardware",
            "auto",
            "car",
            "bike",
            "repair",
        ),
    ),
}

DEFAULT_LOOK = "clean"


def pick(category: str | None) -> Look:
    cat = (category or "").lower()
    for look in LOOKS.values():
        if look.categories and any(_has_word(cat, k) for k in look.categories):
            return look
    return LOOKS[DEFAULT_LOOK]


def apply(brand, look_key: str | None = None) -> Look:
    """Set the brand's fonts and signature from a look; returns the look."""
    look = LOOKS.get(look_key or "", None) or pick(getattr(brand, "category", None))
    brand.fonts = {**(brand.fonts or {}), "heading": look.heading, "body": look.body}
    brand.template_prefs = {
        **(brand.template_prefs or {}),
        "look": look.key,
        "signature": look.signature,
    }
    return look


def describe(look: Look) -> str:
    return f"{look.key}: {look.heading}/{look.body}, {look.signature} mark, {look.note}"
