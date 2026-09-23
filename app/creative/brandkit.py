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
    # The photographic style rides with the look, split by brand so two brands
    # on the same look do not share a photographer. Set once; changing the look
    # re-picks it, an explicit template_prefs["shoot"] survives only until then.
    from app.creative import shotplan

    shoot = shotplan.pick_style(look.key, str(getattr(brand, "name", "") or ""))
    prefs = {
        **(brand.template_prefs or {}),
        "look": look.key,
        "signature": look.signature,
        "shoot": shoot,
    }
    # The preferred family belongs to the look. A family seeded from an
    # onboarding reference set survives a category guess -- nothing calls this
    # with a look once one is set -- but not an owner who asks for a different
    # look outright: leaving it would point them at a layout their new look
    # does not use. seed_from_references sets it again, after this.
    prefs.pop("family", None)
    brand.template_prefs = prefs
    return look


def describe(look: Look) -> str:
    return f"{look.key}: {look.heading}/{look.body}, {look.signature} mark, {look.note}"


# --------------------------------------------------------------------------- #
# seeded from our own team's work, at onboarding
# --------------------------------------------------------------------------- #
def look_for_template(template: str) -> Look:
    """The look whose family leads with this layout.

    A reference set answers "what layout do these posts use", not "which of our
    four looks is this". The look is the layout's home: the one that reaches for
    it first, so the fonts and the signature that come with it are the ones that
    layout was drawn for.
    """
    ranked = [
        (look.family.index(template), key) for key, look in LOOKS.items() if template in look.family
    ]
    if not ranked:
        return LOOKS[DEFAULT_LOOK]
    return LOOKS[min(ranked)[1]]


def seed_from_references(brand, kit) -> dict:
    """Set this brand's kit from the reference creatives our designers made.

    The category guess is a good guess. A folder of finished posts made FOR
    THIS BRAND by our own team is not a guess, so it wins: the layout family
    their set uses becomes the preferred family, the light it was shot in
    becomes the shoot style every generated picture is lit by, and the set's
    colours are checked against the ones measured from the logo.

    Colour rule: the logo's palette is measured from actual pixels and the
    reference palette is a vision model's reading, so where they disagree the
    logo wins the primary -- but the colour the team actually built the set on
    is not thrown away either, it becomes the accent. A brand with no logo yet
    adopts the set's colours outright, which is better than the default.

    Returns what was decided, for the run's printed summary.
    """
    look = apply(brand, look_for_template(kit.layout).key)
    family = (kit.layout, *(t for t in look.family if t != kit.layout))
    prefs = dict(brand.template_prefs or {})
    prefs.update(
        {
            "look": look.key,
            "family": list(family),
            "shoot": kit.light,
            "lessons": list(kit.lessons),
            "seeded_from": "reference_set",
        }
    )
    brand.template_prefs = prefs

    palette = dict(getattr(brand, "palette", None) or {})
    verdict = "kept"
    if kit.palette:
        primary = palette.get("primary")
        if not primary:
            palette["primary"] = kit.palette[0]
            palette.setdefault("secondary", "#FFFFFF")
            palette.setdefault("ink", "#FFFFFF")
            if len(kit.palette) > 1:
                palette.setdefault("accent", kit.palette[1])
            verdict = "adopted from the reference set"
        elif _close(primary, kit.palette[0]):
            verdict = "confirmed by the reference set"
        else:
            palette["accent"] = kit.palette[0]
            verdict = f"logo primary {primary} kept, {kit.palette[0]} taken as the accent"
    brand.palette = palette
    return {
        "look": look.key,
        "family": list(family),
        "shoot": kit.light,
        "lessons": list(kit.lessons),
        "palette": verdict,
    }


# Two colours a person would call the same one. 48 per channel is about the
# distance between a logo's measured green and the same green read off a
# compressed post; beyond it they are different colours and the disagreement
# is worth recording rather than smoothing over.
SAME_COLOUR = 48


def _close(a: str, b: str) -> bool:
    try:
        left = [int(a.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
        right = [int(b.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return False
    return all(abs(x - y) <= SAME_COLOUR for x, y in zip(left, right, strict=True))
