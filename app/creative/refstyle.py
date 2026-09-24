"""Reading the creatives our own designers made for a brand -- for STYLE only.

A brand's first weeks are the ones the client judges us on, and they are
exactly the weeks when every learned lane is empty: taste needs five votes,
the grid signature needs six approved posts. So at onboarding our team hands
over five to ten finished creatives for the brand, and this is what is done
with them.

What is NOT done with them, ever: they are never given to the image model.
A reference creative carries its own headline and its own logo, and an image
model shown one copies the lettering -- which is the single thing brief.py,
photoreal.py and the background gate exist to prevent. So the picture is
looked at once, by the vision model, and turned into WORDS in the vocabulary
the rest of the code already speaks: a layout family from compose.TEMPLATES,
a shoot style from shotplan.SHOOT_STYLES, hex colours, where the type sits,
how the product is shown. Words are safe to carry into a prompt; pixels of
someone else's finished post are not.

This module FAILS CLOSED, like bggate. A reference that the model describes
with a layout family we do not have, or a colour that is not a hex, is not
"mostly right": it is refused, and the onboarding run says which file and
why. A wrong layout family here becomes a brand kit that points at a
template the compositor cannot render, on the client's first creative.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.creative import compose, shotplan
from app.logging import get_logger

log = get_logger(__name__)


class ReferenceUnreadable(RuntimeError):
    """No usable description of this reference. Never a shrug, always a refusal."""


# The layout families. Exactly the templates the compositor can render, so a
# seeded preference can always be honoured.
LAYOUTS = tuple(compose.TEMPLATES)

# Where the type sits, in the terms a designer would use looking at the post.
TYPE_PLACES = ("solid_panel", "bare_on_photo", "top_band", "lower_third", "centred")

# How the product is shown.
PRODUCT_SHOWS = ("whole", "detail", "in_context", "flat_lay")

# The light. These are keys of shotplan.SHOOT_STYLES on purpose: the dominant
# one is written straight into template_prefs["shoot"], which shotplan.style_of
# reads on every generated picture for this brand.
LIGHTS = ("bright_airy", "moody", "warm_documentary", "graphic_studio")
assert all(light in shotplan.SHOOT_STYLES for light in LIGHTS), "light is not a shoot style"

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_PALETTE = 4
MAX_MOOD = 4

# Stamped on every style anchor's meta, so a later run can tell the references
# our own team's set seeded from any other style_anchor a brand collects.
ANCHOR_SOURCE = "onboarding_reference"

PROMPT = (
    "This is a finished social media post a professional design team made for one brand. "
    "Describe its STYLE so another designer could make a new post that belongs beside it. "
    "Do not transcribe the words in it. Answer with JSON only:\n"
    "{\n"
    '  "layout": one of ' + "|".join(LAYOUTS) + ", the layout family it is closest to "
    "(centered_overlay: type centred over the whole picture; lower_third: type in a band "
    "low down; split_card: picture and a colour panel side by side; top_band: type in a "
    "band across the top; poster_stack: big stacked type filling most of the frame; "
    "frame_card: the picture inset inside a border),\n"
    '  "type_place": one of ' + "|".join(TYPE_PLACES) + ", where the words actually sit,\n"
    '  "palette": ["#RRGGBB", ...] 2 to 4 colours the post is built on, most important '
    "first,\n"
    '  "mood": ["<word>", ...] 1 to 4 single words for how it feels,\n'
    '  "product": one of ' + "|".join(PRODUCT_SHOWS) + ", how the product is shown "
    "(whole: the whole item; detail: a close crop of part of it; in_context: in use or in "
    "a real setting; flat_lay: arranged and shot from directly above),\n"
    '  "light": one of ' + "|".join(LIGHTS) + " (bright_airy: bright high-key daylight; "
    "moody: low-key, deep shadows; warm_documentary: warm late-afternoon, unposed; "
    "graphic_studio: crisp studio light, hard shadow, saturated colour),\n"
    '  "summary": "<one sentence a designer would write in a brand sheet>"\n'
    "}\n"
    "Every key is required. Use exactly the given values, lower case, no other words."
)

# The other question the vision model is asked about an onboarding file, and the
# cheaper one: has anything been composited ONTO this image? It is how the
# command catches the two folder arguments the wrong way round. The carve-out
# for printed packaging is the whole difficulty of the question -- a jar with
# its own label on it is a photograph of a jar, and a guard that called that a
# finished post would refuse the most ordinary product photo there is.
FINISHED_PROMPT = (
    "Look at this image and decide ONE thing: has any graphic design been laid ON TOP of "
    "it?\n"
    'Answer with JSON only: {"finished": true|false, "why": "<up to 8 words>"}\n'
    "true  -- a finished social media post: a headline, a caption, a price, a sticker or a "
    "brand logo composited over or beside the picture.\n"
    "false -- a plain photograph of a thing, a place or people, with nothing added.\n"
    "Words printed on the product itself -- a label on a jar, a name on a box, a sign above "
    "a shop -- are part of the photograph and DO NOT make it true. Judge only what was added "
    "afterwards, in a design tool."
)

ATTEMPTS = 2
LONG_EDGE = 1024


@dataclass(slots=True)
class Reference:
    """One reference creative, in words."""

    layout: str
    type_place: str
    palette: list[str]
    mood: list[str]
    product: str
    light: str
    summary: str

    def as_meta(self) -> dict[str, Any]:
        return {
            "source": ANCHOR_SOURCE,
            "layout": self.layout,
            "type_place": self.type_place,
            "palette": list(self.palette),
            "mood": list(self.mood),
            "product": self.product,
            "light": self.light,
        }

    def as_prose(self, brand_name: str) -> str:
        """What gets embedded and retrieved later, written the way the style lane
        expects: a sentence about a creative that worked for this brand."""
        return (
            f"{brand_name} reference creative (made by our design team): {self.summary} "
            f"Layout {self.layout}, words {TYPE_PLACE_WORDS[self.type_place]}, "
            f"product shown {PRODUCT_WORDS[self.product]}, {LIGHT_WORDS[self.light]}. "
            f"Colours {', '.join(self.palette)}. Mood {', '.join(self.mood)}."
        )


TYPE_PLACE_WORDS = {
    "solid_panel": "on a solid panel",
    "bare_on_photo": "straight on the photo",
    "top_band": "in a band across the top",
    "lower_third": "in the lower third",
    "centred": "centred over the picture",
}
PRODUCT_WORDS = {
    "whole": "whole",
    "detail": "as a close detail",
    "in_context": "in use, in a real setting",
    "flat_lay": "as a flat-lay from above",
}
LIGHT_WORDS = {
    "bright_airy": "bright and airy in soft daylight",
    "moody": "moody, one low light source and deep shadows",
    "warm_documentary": "warm and unposed in late-afternoon light",
    "graphic_studio": "crisp studio light with a hard shadow and strong colour",
}


def available() -> bool:
    return bool(settings.anthropic_api_key and settings.anthropic_model)


def parse(text: str) -> Reference:
    """Strict: every key present, every value one we can act on, or no reference.

    A tolerant parse here is the expensive kind of tolerance -- it produces a
    brand kit that looks seeded and points at a layout that does not exist.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ReferenceUnreadable(f"no JSON in the style pass's answer: {text[:120]!r}")
    try:
        data: dict[str, Any] = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReferenceUnreadable(f"unparseable style description: {text[:120]!r}") from exc

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ReferenceUnreadable("summary is missing")
    return _checked(data, summary)


def from_meta(meta: dict[str, Any], summary: str = "") -> Reference:
    """A reference we already read, rebuilt from the memory row we wrote it to.

    Every style anchor keeps its facts in `meta` (see as_meta), which is what
    lets a re-run decide the house style over a brand's WHOLE reference set
    rather than over the files that run happened to open. Held to exactly the
    same bar as a fresh answer: a row written by an older version of this
    module, or edited by hand, is refused rather than allowed to tilt a kit.
    """
    if meta.get("source") != ANCHOR_SOURCE:
        raise ReferenceUnreadable(
            f"meta is not an onboarding reference: source={meta.get('source')!r}"
        )
    return _checked(meta, summary or "a reference creative")


def _checked(data: dict[str, Any], summary: str) -> Reference:
    """Every field held to the values the rest of the code can act on."""

    def one_of(key: str, allowed: tuple[str, ...]) -> str:
        value = data.get(key)
        if not isinstance(value, str) or value.strip().lower() not in allowed:
            raise ReferenceUnreadable(f"{key}={value!r} is not one of {list(allowed)}")
        return value.strip().lower()

    palette = data.get("palette")
    if not isinstance(palette, list) or not 1 <= len(palette) <= MAX_PALETTE:
        raise ReferenceUnreadable(f"palette must be 1 to {MAX_PALETTE} colours, got {palette!r}")
    hexes = []
    for colour in palette:
        if not isinstance(colour, str) or not _HEX.match(colour.strip()):
            raise ReferenceUnreadable(f"{colour!r} is not a #RRGGBB colour")
        hexes.append(colour.strip().upper())

    mood = data.get("mood")
    if not isinstance(mood, list) or not mood or not all(isinstance(w, str) and w for w in mood):
        raise ReferenceUnreadable(f"mood must be a list of words, got {mood!r}")

    return Reference(
        layout=one_of("layout", LAYOUTS),
        type_place=one_of("type_place", TYPE_PLACES),
        palette=hexes,
        mood=[w.strip().lower() for w in mood[:MAX_MOOD]],
        product=one_of("product", PRODUCT_SHOWS),
        light=one_of("light", LIGHTS),
        summary=" ".join(summary.split())[:300],
    )


def parse_finished(text: str) -> tuple[bool, str]:
    """Yes or no, and why. Anything else is no answer at all.

    Strict for the same reason `parse` is: "probably not a post" is the answer
    that files our own design team's work as a photograph of a product.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ReferenceUnreadable(f"no JSON in the lettering check's answer: {text[:120]!r}")
    try:
        data: dict[str, Any] = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReferenceUnreadable(f"unparseable lettering check: {text[:120]!r}") from exc
    verdict = data.get("finished")
    if not isinstance(verdict, bool):
        raise ReferenceUnreadable(f"finished={verdict!r} is not true or false")
    why = data.get("why")
    return verdict, " ".join(str(why).split())[:60] if isinstance(why, str) else ""


def _thumbnail(image: bytes) -> bytes:
    """A copy for the model only. The file itself is stored untouched."""
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(image)) as im:
        im = im.convert("RGB")
        im.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=90)
        return out.getvalue()


async def _ask(image: bytes, prompt: str = PROMPT, max_tokens: int = 500) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(image).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


async def describe(image: bytes) -> Reference:
    """One reference creative in words. Raises ReferenceUnreadable, never guesses."""
    if not available():
        raise ReferenceUnreadable(
            "no vision model configured (ANTHROPIC_API_KEY / ANTHROPIC_MODEL); reference "
            "creatives cannot be read for style"
        )
    small = await asyncio.to_thread(_thumbnail, image)
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return parse(await _ask(small))
        except Exception as exc:  # noqa: BLE001 - API error or unusable answer: ask again
            last = exc
            log.warning("refstyle_retry", attempt=attempt, error=repr(exc)[:200])
            await asyncio.sleep(1.5 * attempt)
    raise ReferenceUnreadable(f"style pass failed {ATTEMPTS} times: {last!r}"[:300])


async def looks_finished(image: bytes) -> tuple[bool, str] | None:
    """Has design been laid on top of this image? With the model's own reason.

    `None` means nobody could tell -- no model is configured, or it would not
    answer in two tries. That is deliberately not a refusal. This pass is not a
    guarantee, it is a check on which folder a human typed: the guarantee that
    a reference creative is never composited over lives in the kind whitelist
    (photoref.USABLE_KINDS) and holds whether or not this ever runs. Refusing
    an onboarding because a vision call timed out would cost a paying client
    their first day for nothing.
    """
    if not available():
        return None
    small = await asyncio.to_thread(_thumbnail, image)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return parse_finished(await _ask(small, FINISHED_PROMPT, max_tokens=100))
        except Exception as exc:  # noqa: BLE001 - API error or unusable answer: ask again
            log.warning("refstyle_lettering_retry", attempt=attempt, error=repr(exc)[:200])
            await asyncio.sleep(1.5 * attempt)
    return None


# --------------------------------------------------------------------------- #
# the aggregate: one set of references -> one brand kit
# --------------------------------------------------------------------------- #
# A lesson is only written when the set actually agrees. Five posts that each
# do something different are five posts, not a house style, and a standing
# rule invented from a 40% plurality is a rule the client never asked for.
MAJORITY = 0.5
MAX_LESSONS = 6
# Colours are counted in coarse buckets: two shots of the same green come back
# as #1F5B3D and #215E40, which are the same colour to everyone except ==.
COLOUR_BUCKET = 32


@dataclass(slots=True)
class Kit:
    """What a reference set says this brand looks like."""

    layout: str
    light: str
    palette: list[str]
    moods: list[str]
    lessons: list[str] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)


def _dominant(values: list[str], order: tuple[str, ...]) -> tuple[str, float]:
    """The most common value and its share; ties broken by `order`, never by
    dict iteration, so the same reference set always seeds the same kit."""
    counts = Counter(values)
    best = min(counts, key=lambda v: (-counts[v], order.index(v) if v in order else len(order)))
    return best, counts[best] / max(1, len(values))


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _palette(refs: list[Reference]) -> list[str]:
    """The colours the SET is built on, not the colours one post happens to use.

    Each reference votes once per bucket, so a single post with four shades of
    the same green does not outvote the other four posts.
    """
    counts: Counter[tuple[int, int, int]] = Counter()
    first: dict[tuple[int, int, int], str] = {}
    for ref in refs:
        here: dict[tuple[int, int, int], str] = {}
        for colour in ref.palette:
            here.setdefault(tuple(c // COLOUR_BUCKET for c in _rgb(colour)), colour)
        counts.update(here.keys())
        for bucket, colour in here.items():
            first.setdefault(bucket, colour)
    seq = list(first)
    ranked = sorted(seq, key=lambda b: (-counts[b], seq.index(b)))
    return [first[b] for b in ranked[:3]]


def aggregate(refs: list[Reference]) -> Kit:
    """The house style of a reference set, decided in code, not by a model."""
    if not refs:
        raise ValueError("no references to aggregate")
    layout, layout_share = _dominant([r.layout for r in refs], LAYOUTS)
    light, light_share = _dominant([r.light for r in refs], LIGHTS)
    place, place_share = _dominant([r.type_place for r in refs], TYPE_PLACES)
    product, product_share = _dominant([r.product for r in refs], PRODUCT_SHOWS)
    palette = _palette(refs)
    moods = [w for w, _ in Counter(w for r in refs for w in r.mood).most_common(3)]

    lessons: list[str] = []
    if layout_share > MAJORITY:
        lessons.append(f"their posts are built as {layout} -- keep new ones in that family")
    if place_share > MAJORITY:
        lessons.append(f"their posts set the words {TYPE_PLACE_WORDS[place]}")
    if product_share > MAJORITY:
        lessons.append(f"their product is shown {PRODUCT_WORDS[product]}")
    if light_share > MAJORITY:
        lessons.append(f"their pictures are {LIGHT_WORDS[light]}")
    if palette:
        lessons.append("their set runs on " + ", ".join(palette))
    if moods:
        lessons.append("the set reads " + ", ".join(moods))

    return Kit(
        layout=layout,
        light=light,
        palette=palette,
        moods=moods,
        lessons=lessons[:MAX_LESSONS],
        counts={
            "layout": dict(Counter(r.layout for r in refs)),
            "light": dict(Counter(r.light for r in refs)),
            "type_place": dict(Counter(r.type_place for r in refs)),
            "product": dict(Counter(r.product for r in refs)),
        },
    )
