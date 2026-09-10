"""Make the generated lane look photographed rather than rendered.

Realism is decided before the image model runs, not after. Two levers do most
of the work, and both are applied here in code rather than left to the agent:

* **Camera language in the positive prompt.** A model given "coconut oil bottle
  on jute" invents a glossy studio render. The same model given a lens, an
  aperture and a light source returns something that reads as a photograph,
  because that is what those words are attached to in its training data.
* **The rendered look in the negative prompt.** "3d render", "cgi", "airbrushed"
  and "plastic" are the words for the exact failure the owner means when they
  say a creative "looks AI". Naming them is far more effective than asking for
  realism in the positive.

Deliberately NOT here: anything that would put letterforms in the image. The
brief validator forbids that at the type boundary, and enrichment must not
smuggle it back in -- headline, subhead, CTA and logo are composited afterwards
in the brand's real fonts.

The enrichment is idempotent: `regenerate_image` re-runs this on a prompt that
may already carry it, and stacking the camera clause twice makes the model
weight it oddly.
"""

from __future__ import annotations

import re

from app.creative.brief import MOOD_MAX

# The brief validator caps the model-authored prompt at 900 characters and the
# mood at MOOD_MAX. The enrichment adds a bounded amount on top, so the total on
# the wire is known: 900 + ENRICHMENT_MAX. Enforced below, not just documented.
ENRICHMENT_MAX = 720

# Present in every enriched prompt; also the idempotency marker.
_MARKER = "shot on a full-frame camera"
# Public name for the providers: the camera clause starts here.
CAMERA_MARKER = _MARKER

CAMERA_DIRECTION = (
    f"{_MARKER} with a 50mm prime at f/2.0, natural directional daylight from one side, "
    "shallow depth of field with soft falloff, true-to-life colour, "
    "fine natural grain, real surfaces with small imperfections left in, "
    "candid editorial product photography"
)

# The rendered-looking failure modes, named. Ordered roughly by how often they
# are what someone means by "it looks AI".
PHOTOREAL_NEGATIVE = (
    "3d render, cgi, digital art, illustration, painting, concept art, "
    "airbrushed, plastic surfaces, waxy, glossy cgi highlights, overprocessed, "
    "oversaturated, hdr, unrealistic lighting, neon glow, surreal, "
    "perfectly symmetrical, duplicated objects, mangled anatomy, extra limbs"
)


# --------------------------------------------------------------------------- #
# the playbook: what a product photographer does differently per category
# --------------------------------------------------------------------------- #
# Read from the brand's stated category. Each clause is the ONE decision that
# separates a category's good product photography from generic "nice photo":
# the surface, the props that are true, the angle, and one light. Nothing here
# may ask for lettering -- see the validator in brief.py; a test checks it.
PLAYBOOK: list[tuple[tuple[str, ...], str]] = [
    (
        (
            "food",
            "sweet",
            "mithai",
            "bakery",
            "cake",
            "restaurant",
            "cafe",
            "snack",
            "oil",
            "ghee",
            "spice",
            "pickle",
            "tea",
            "coffee",
            "dairy",
            "juice",
            "kitchen",
        ),
        "on a natural surface (worn wood, stone or linen), the real ingredients as props, "
        "three-quarter angle at table height, warm side light with a soft fill, "
        "steam or fresh droplets only where they would truly be",
    ),
    (
        (
            "skincare",
            "cosmetic",
            "beauty",
            "salon",
            "serum",
            "cream",
            "soap",
            "perfume",
            "hair",
            "makeup",
        ),
        "on a smooth pastel or stone surface, diffused soft light with a gentle gradient, "
        "one botanical or water-droplet prop, glossy highlights on the packaging kept, "
        "generous negative space",
    ),
    (
        (
            "cloth",
            "apparel",
            "boutique",
            "saree",
            "sari",
            "kurta",
            "fashion",
            "garment",
            "dress",
            "ethnic wear",
            "tailor",
        ),
        "worn by a real person mid-movement or arranged as a tidy flat-lay on linen, "
        "natural window light, weave and drape visible, no mannequin stiffness",
    ),
    (
        ("jewel", "gold", "silver", "diamond", "ornament", "bangle"),
        "macro on dark velvet or veined marble, one hard key light with a broad soft fill "
        "so facets sparkle without blown highlights, shallow depth of field",
    ),
    (
        ("electronic", "mobile", "phone", "gadget", "laptop", "appliance", "electric"),
        "clean graduated studio backdrop, subtle rim light on the edges, a faint "
        "reflection on the surface, cool neutral tones, precise focus edge to edge",
    ),
    (
        ("furniture", "decor", "home", "interior", "lamp", "mattress", "curtain"),
        "styled in a lived-in room corner, daylight from one large window, a wider lens "
        "showing the piece in scale with the room, warm and quiet",
    ),
    (
        (
            "gym",
            "fitness",
            "clinic",
            "dental",
            "coaching",
            "class",
            "academy",
            "real estate",
            "property",
            "service",
            "repair",
            "travel",
            "event",
        ),
        "a candid environmental photograph of the actual place or people at work, "
        "documentary framing, available light, nothing staged",
    ),
]


# The camera clause, the longest playbook clause and the longest mood, plus
# punctuation, must fit the budget.
_LONGEST_PLAY = max(len(c) for _, c in PLAYBOOK)
assert len(CAMERA_DIRECTION) + _LONGEST_PLAY + MOOD_MAX + 12 <= ENRICHMENT_MAX, (
    "enrichment exceeds its budget"
)


def _has_word(text: str, key: str) -> bool:
    """Whole-word match with a plural allowed: "hair" must not match "chair"."""
    return re.search(rf"(?<![a-z]){re.escape(key)}(?:s|es)?(?![a-z])", text) is not None


def category_direction(category: str | None) -> str:
    """The playbook clause for a brand category, or an empty string."""
    cat = (category or "").lower()
    if not cat:
        return ""
    for keys, clause in PLAYBOOK:
        if any(_has_word(cat, k) for k in keys):
            return clause
    return ""


def photographic(
    prompt: str,
    negative: str | None,
    *,
    mood: str | None = None,
    category: str | None = None,
) -> tuple[str, str]:
    """Return (prompt, negative) tuned for a photograph, safely re-runnable."""
    p = (prompt or "").strip()
    if _MARKER not in p:
        mood_clause = f", {mood.strip()[:MOOD_MAX]}" if mood and mood.strip() else ""
        play = category_direction(category)
        play_clause = f" {play}." if play else ""
        p = f"{p.rstrip('.,; ')}{mood_clause}.{play_clause} {CAMERA_DIRECTION}."

    parts = [s.strip() for s in (negative or "").split(",") if s.strip()]
    seen = {s.lower() for s in parts}
    for extra in PHOTOREAL_NEGATIVE.split(","):
        e = extra.strip()
        if e and e.lower() not in seen:
            parts.append(e)
            seen.add(e.lower())
    return p, ", ".join(parts)
