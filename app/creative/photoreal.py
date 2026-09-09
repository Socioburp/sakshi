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

from app.creative.brief import MOOD_MAX

# The brief validator caps the model-authored prompt at 900 characters and the
# mood at MOOD_MAX. The enrichment adds a bounded amount on top, so the total on
# the wire is known: 900 + ENRICHMENT_MAX. Enforced below, not just documented.
ENRICHMENT_MAX = 400

# Present in every enriched prompt; also the idempotency marker.
_MARKER = "shot on a full-frame camera"

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


# The whole clause plus the longest mood plus punctuation must fit the budget.
assert len(CAMERA_DIRECTION) + MOOD_MAX + 8 <= ENRICHMENT_MAX, "enrichment exceeds its budget"


def photographic(prompt: str, negative: str | None, *, mood: str | None = None) -> tuple[str, str]:
    """Return (prompt, negative) tuned for a photograph, safely re-runnable."""
    p = (prompt or "").strip()
    if _MARKER not in p:
        mood_clause = f", {mood.strip()[:MOOD_MAX]}" if mood and mood.strip() else ""
        p = f"{p.rstrip('.,; ')}{mood_clause}. {CAMERA_DIRECTION}."

    parts = [s.strip() for s in (negative or "").split(",") if s.strip()]
    seen = {s.lower() for s in parts}
    for extra in PHOTOREAL_NEGATIVE.split(","):
        e = extra.strip()
        if e and e.lower() not in seen:
            parts.append(e)
            seen.add(e.lower())
    return p, ", ".join(parts)
