"""The copy playbook: what converts for an Indian shop owner's customer.

An agency copywriter carries this in their head after a few years of
small-business work; the model does not, so it is given here, per brand
category and post intent, as a short block in the system prompt. Patterns,
not sentences: the agent fills them from what the owner actually said, and
every number, price and date still has to come from the owner (the brief
validator and the claim guard enforce that).

Sources, in spirit: what SMB feeds in India that grow have in common --
rupee anchoring, bundles over percentages, a real deadline, the locality by
name, trust that is specific ("since 1998", "2,000+ customers"), and a CTA
the owner can actually answer (WhatsApp, a call, a visit).
"""

from __future__ import annotations

from app.creative.photoreal import _has_word

# Placeholders the agent fills from the owner's words. Anything it cannot
# fill it leaves out -- never invents.
HOOKS: dict[str, list[str]] = {
    "promo": [
        "₹{price} — {product}, sirf {deadline} tak",
        "Buy 2, get 1: {product}",
        "Combo offer: {product} + {product2} at ₹{price}",
        "Free delivery in {locality}, order by {time}",
        "Was ₹{old_price}, now ₹{price} — till {deadline}",
    ],
    "new_product": [
        "Naya aaya hai: {product}",
        "Fresh stock — {product} ab available",
        "First in {locality}: {product}",
        "Made in small batches: {product}",
    ],
    "festive": [
        "{festival} special: {product}",
        "{festival} gifting sorted — {product}",
        "Order by {date} for {festival} delivery",
    ],
    "testimonial": [
        "{count}+ families in {locality} trust us",
        '"{quote}" — {customer_first_name}, {locality}',
        "Since {year}, same recipe, same hands",
    ],
    "announcement": [
        "Ab {locality} mein bhi — new branch",
        "Now open till {time}",
        "Home delivery started — {locality}",
    ],
    "educational": [
        "3 ways to use {product}",
        "How to pick a good {product}",
        "{product}: what to check before you buy",
    ],
    "behind_the_scenes": [
        "Subah {time} se: how we make {product}",
        "Meet {name}, who makes your {product}",
        "What goes into {product}",
    ],
}

# Specific beats generic. These are the lines owners forget to say.
TRUST_LINES = [
    "Since {year}",
    "{count}+ customers",
    "Family-run",
    "Made fresh daily",
    "Same-day delivery in {locality}",
    "Replacement if not happy",
]

CTA_BY_INTENT: dict[str, list[str]] = {
    "promo": ["WhatsApp to order", "Order on WhatsApp", "Visit today"],
    "new_product": ["WhatsApp for price", "Come see it", "Book yours"],
    "festive": ["Order by {date}", "WhatsApp to book", "Pre-book now"],
    "testimonial": ["Try it once", "Visit us", "WhatsApp us"],
    "announcement": ["Save our number", "Visit us", "Call now"],
    "educational": ["Save this", "Share with a friend", "Ask us on WhatsApp"],
    "behind_the_scenes": ["Come visit", "Follow for more", "WhatsApp us"],
}

# What the offer coach knows. Given to the agent as rules, not code: the
# decision needs the owner's answer about margin, which only a conversation
# can get.
OFFER_RULES = (
    "Offers: prefer a bundle (buy 2 get 1, a combo) or a gift to a percentage "
    "discount -- it protects margin and reads as generous. Before proposing a "
    "discount over 15%, ask once what the margin is; never assume. Always show the "
    "anchor (usual price next to the offer price) and a real end date the owner "
    "named. A discount without a deadline is just a lower price."
)

# Category-level copy habits: the one or two things that matter most for
# this kind of shop. Whole-word matching on the brand's stated category.
CATEGORY_HABITS: list[tuple[tuple[str, ...], str]] = [
    (
        ("restaurant", "cafe", "bakery", "sweets", "sweet", "food", "tiffin", "catering", "snacks"),
        "Food sells on time and place: name the dish, the time it is fresh, the locality; "
        "mention delivery radius. Hinglish is natural here.",
    ),
    (
        ("skin", "skincare", "cosmetic", "beauty", "cream", "serum", "salon", "spa"),
        "Beauty sells on ingredients and ritual, never on medical outcomes: name the "
        "hero ingredient, the texture, when to use it. Claims are guarded by the server.",
    ),
    (
        ("cloth", "apparel", "boutique", "saree", "sari", "kurta", "fashion", "garment", "dress"),
        "Apparel sells on occasion and fit: name the occasion (wedding, office, Diwali), "
        "sizes available, and 'DM for price' is fine here.",
    ),
    (
        ("jewel", "gold", "silver", "diamond", "ornament"),
        "Jewellery sells on trust and occasion: hallmark/BIS if true, making charges, "
        "exchange policy; never a price without the owner's say.",
    ),
    (
        ("electronic", "mobile", "phone", "gadget", "laptop", "appliance", "repair"),
        "Electronics sells on price, warranty and speed: EMI if offered, same-day repair, "
        "genuine parts (only if true).",
    ),
    (
        ("coaching", "tuition", "academy", "institute", "classes", "school", "course"),
        "Education sells on outcomes you can show (batch results with records), batch "
        "size, timings and a free demo class; no guarantees.",
    ),
    (
        ("clinic", "dental", "dentist", "physio", "hospital", "doctor"),
        "Healthcare sells on access and care: timings, appointment on WhatsApp, the "
        "doctor's name and qualification; no cure or outcome claims.",
    ),
    (
        ("gym", "fitness", "yoga"),
        "Fitness sells on community and consistency: trial class, timings, trainer's "
        "name; no weight-loss numbers.",
    ),
    (
        ("furniture", "decor", "interior", "home"),
        "Home sells on the room: show it in use, name the material, delivery and "
        "assembly; 'made to order' is a plus.",
    ),
]


def habits(category: str | None) -> str:
    cat = (category or "").lower()
    for keys, line in CATEGORY_HABITS:
        if any(_has_word(cat, k) for k in keys):
            return line
    return "Name the product, the price if the owner gave one, the locality, and how to buy."


def hooks_for(intent: str | None) -> list[str]:
    return HOOKS.get(intent or "", [])


def prompt_block(category: str | None, locality: str | None = None) -> str:
    """One compact block for the system prompt: habits, a hook pattern per
    intent, trust lines, CTAs and the offer rules. ~900 characters."""
    lines = ["## Copy that converts here (patterns; fill only from what the owner said)"]
    lines.append(habits(category))
    if locality:
        lines.append(f"Their locality is '{locality}': use it by name where it fits.")
    lines.append(
        "Hooks by intent -- "
        + " | ".join(f"{intent}: {patterns[0]}" for intent, patterns in HOOKS.items())
    )
    lines.append("Trust lines (specific beats generic): " + "; ".join(TRUST_LINES[:5]) + ".")
    lines.append(
        "CTA: one action the owner can answer -- "
        + "; ".join(f"{k}: {v[0]}" for k, v in list(CTA_BY_INTENT.items())[:4])
        + ". 'DM for price' only for apparel/jewellery."
    )
    lines.append(OFFER_RULES)
    lines.append(
        "Caption: first line is the hook (it is all that shows before 'more'); 5-10 "
        "hashtags mixing locality + niche, never 30."
    )
    return "\n".join(lines)
