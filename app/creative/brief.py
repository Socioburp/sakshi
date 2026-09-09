"""The brief contract.

This is a pydantic implementation of `docs/brief_schema.json`, which is the
canonical definition. The brief is emitted by the agent BEFORE any image call:
it is the unit of revision, the debug surface, and what gets replayed on
failure. If the brief is wrong, no money is spent on an image.

The rule the whole cost model rests on: `visual_direction.prompt` describes the
BACKGROUND/HERO IMAGE ONLY. It must never request text, logos or watermarks --
those are composited afterwards with the brand's real fonts. That makes a copy
change a re-composite (~1s, free) instead of a re-generation (~10s, paid). The
validator below enforces it at the type boundary rather than trusting the
prompt to hold.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AspectRatio = Literal["1:1", "4:5", "9:16"]
Intent = Literal[
    "promo",
    "new_product",
    "festive",
    "testimonial",
    "announcement",
    "educational",
    "behind_the_scenes",
]

PIXELS: dict[str, tuple[int, int]] = {
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "9:16": (1080, 1920),
}

# Phrases that mean "render letterforms", which the image model must not do.
_TEXT_REQUEST = re.compile(
    r"\b(text|texts|word|words|wording|caption|captions|headline|headlines|title|titles|"
    r"typography|typographic|font|fonts|lettering|letterform|calligraphy|slogan|tagline|"
    r"logo|logotype|wordmark|watermark|banner text|sign that (says|reads)|writing|written|"
    r"label(l|)ed|subtitle|price tag|number overlay)\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"[\"“”']{1}[^\"“”']{2,}[\"“”']{1}")

NEGATIVE_PROMPT_DEFAULT = (
    "text, letters, words, watermark, logo, signature, caption, typography, "
    "distorted hands, extra fingers, deformed, low quality, jpeg artifacts"
)


# What we sell today. Instagram itself allows up to 10, and the canonical
# schema was written for 10, but the product currently offers 2-6 -- so the
# limit lives here, in one place, rather than in the prompt where the model
# can talk itself past it.
CAROUSEL_MIN = 2
CAROUSEL_MAX = 6


class Format(BaseModel):
    type: Literal["single", "carousel"] = "single"
    aspect_ratio: AspectRatio = "1:1"
    slide_count: int | None = Field(default=None, ge=1, le=CAROUSEL_MAX)

    @model_validator(mode="after")
    def slide_count_matches_type(self) -> Format:
        if self.type == "single":
            self.slide_count = 1
        elif not self.slide_count:
            self.slide_count = 3
        elif not CAROUSEL_MIN <= self.slide_count <= CAROUSEL_MAX:
            raise ValueError(
                f"carousels are {CAROUSEL_MIN}-{CAROUSEL_MAX} slides; got {self.slide_count}"
            )
        return self


class VisualDirection(BaseModel):
    """Background / hero image only. No text, logos or watermarks, ever."""

    prompt: str = Field(min_length=10, max_length=900)
    negative_prompt: str = NEGATIVE_PROMPT_DEFAULT
    mood: str | None = None
    reference_asset_id: str | None = Field(
        default=None, description="brand_assets.id when the post features a real product photo"
    )
    seed: int | None = None

    @field_validator("prompt")
    @classmethod
    def no_text_in_prompt(cls, v: str) -> str:
        hit = _TEXT_REQUEST.search(v)
        if hit:
            raise ValueError(
                f"visual_direction.prompt must describe the background image only; it asked "
                f"for {hit.group(0)!r}. Headline, CTA and logo are composited afterwards -- "
                f"move that copy into the headline/subhead/cta fields."
            )
        if _QUOTED.search(v):
            raise ValueError(
                "visual_direction.prompt contains quoted copy. A quoted string reads as "
                "'render these letters' -- put the copy in headline/subhead/cta instead."
            )
        return v.strip()


class Caption(BaseModel):
    body: str = ""
    hashtags: list[str] = Field(default_factory=list, max_length=15)
    language: str = "en"

    @field_validator("hashtags")
    @classmethod
    def normalise(cls, v: list[str]) -> list[str]:
        out, seen = [], set()
        for tag in v:
            t = "#" + re.sub(r"[^0-9A-Za-z_]", "", tag.lstrip("#"))
            if len(t) > 1 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    def rendered(self) -> str:
        return f"{self.body}\n\n{' '.join(self.hashtags)}".strip()


class Grounding(BaseModel):
    """What retrieval returned when this brief was built. Logged so you can tell
    a bad creative caused by a bad brief from one caused by bad retrieval."""

    catalog_item_ids: list[str] = Field(default_factory=list)
    style_anchor_ids: list[str] = Field(default_factory=list)
    rejection_ids: list[str] = Field(default_factory=list)


class Slide(BaseModel):
    position: int = Field(ge=1, le=CAROUSEL_MAX)
    headline: str = Field(max_length=60)
    subhead: str | None = Field(default=None, max_length=100)
    visual_direction: VisualDirection
    template_id: str | None = None


class CreativeBrief(BaseModel):
    intent: Intent
    format: Format = Field(default_factory=Format)

    headline: str = Field(min_length=1, max_length=60)
    subhead: str | None = Field(default=None, max_length=100)
    cta: str | None = Field(default=None, max_length=30)

    visual_direction: VisualDirection

    template_id: str = "centered_overlay"
    palette_override: dict[str, Any] | None = None
    caption: Caption = Field(default_factory=Caption)
    alt_text: str | None = Field(
        default=None,
        max_length=300,
        description="Instagram accessibility field, passed on the media container.",
    )
    grounding: Grounding = Field(default_factory=Grounding)
    slides: list[Slide] = Field(default_factory=list)

    @model_validator(mode="after")
    def carousel_consistency(self) -> CreativeBrief:
        if self.format.type == "carousel":
            if not self.slides:
                raise ValueError(
                    "format.type is 'carousel' but slides[] is empty. Emit one slide per "
                    "image, each with its own headline and visual_direction."
                )
            if not CAROUSEL_MIN <= len(self.slides) <= CAROUSEL_MAX:
                raise ValueError(
                    f"we offer {CAROUSEL_MIN}-{CAROUSEL_MAX} slide carousels; this brief has "
                    f"{len(self.slides)}. Trim it, or make it a single post."
                )
            self.slides.sort(key=lambda s: s.position)
            seen = [s.position for s in self.slides]
            if len(set(seen)) != len(seen):
                raise ValueError(f"duplicate slide positions: {seen}")
            self.format.slide_count = len(self.slides)
        elif self.slides:
            raise ValueError("slides[] is only valid when format.type is 'carousel'.")
        return self

    # -- helpers used downstream -------------------------------------------- #
    def pixel_size(self) -> tuple[int, int]:
        return PIXELS[self.format.aspect_ratio]

    def is_carousel(self) -> bool:
        return self.format.type == "carousel"

    def units(self) -> list[Slide]:
        """Normalise single and carousel into one list the pipeline can loop over."""
        if self.is_carousel():
            return self.slides
        return [
            Slide(
                position=1,
                headline=self.headline,
                subhead=self.subhead,
                visual_direction=self.visual_direction,
                template_id=self.template_id,
            )
        ]

    def template_for(self, slide: Slide) -> str:
        return slide.template_id or self.template_id


def check_brand_rules(brief: CreativeBrief, brand: Any) -> list[str]:
    """Hard gate on `never_say`. Runs server-side, after the model.

    Deliberately not a field on the brief: the model emits the brief, and the
    thing that decides whether a creative may ship should not be something the
    model can set to true itself.
    """
    parts = [brief.headline, brief.subhead, brief.cta, brief.caption.body, brief.alt_text]
    for s in brief.slides:
        parts += [s.headline, s.subhead]
    haystack = " ".join(p for p in parts if p).lower()
    rules = getattr(brand, "never_say", None) or []
    return [phrase for phrase in rules if phrase.lower() in haystack]


EXAMPLE = {
    "intent": "promo",
    "format": {"type": "single", "aspect_ratio": "4:5"},
    "headline": "Weekend Sale",
    "subhead": "Cold-pressed, ghar jaisa shudh",
    "cta": "Order on WhatsApp",
    "template_id": "centered_overlay",
    "visual_direction": {
        "prompt": (
            "a glass bottle of golden coconut oil on a warm terracotta surface, "
            "fresh coconut halves and green leaves beside it, soft morning window "
            "light, shallow depth of field, empty space in the upper third"
        ),
        "mood": "wholesome, homely",
    },
    "caption": {
        "body": "Is weekend sirf. Cold-pressed coconut oil, 500ml ₹249.",
        "hashtags": ["#coconutoil", "#coldpressed", "#bengaluru"],
        "language": "hi",
    },
    "alt_text": (
        "A glass bottle of golden coconut oil beside fresh coconut halves in morning light."
    ),
}

EXAMPLE_CAROUSEL = {
    "intent": "educational",
    "format": {"type": "carousel", "aspect_ratio": "1:1", "slide_count": 3},
    "headline": "3 ways to use cold-pressed oil",
    "cta": "Save this post",
    "visual_direction": {
        "prompt": "warm flatlay of a coconut oil bottle on a jute mat, soft daylight"
    },
    "slides": [
        {
            "position": 1,
            "headline": "3 ways to use it",
            "visual_direction": {
                "prompt": "warm flatlay of a coconut oil bottle on jute, soft daylight"
            },
        },
        {
            "position": 2,
            "headline": "Cooking",
            "subhead": "High smoke point, no smell",
            "visual_direction": {
                "prompt": "a hot kadai with oil shimmering, steam, warm kitchen light"
            },
        },
        {
            "position": 3,
            "headline": "Hair and skin",
            "visual_direction": {
                "prompt": "oil poured into an open palm, soft window light, plain background"
            },
        },
    ],
}
