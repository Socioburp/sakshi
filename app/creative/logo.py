"""Logo intake and analysis.

When the client sends their logo, two different things happen to it, and the
split matters:

* **The palette is measured, not guessed.** Colours come out of the actual
  pixels with Pillow. Asking a vision model "what colour is this logo" returns
  a plausible-sounding hex that is subtly wrong, and every creative afterwards
  is subtly off-brand. Counting pixels is deterministic, free, and correct.
* **The character is read by vision.** What the mark *is* -- a wordmark, an
  emblem, hand-drawn or geometric, premium or friendly -- is genuinely a
  judgement call, and that is what the model is for.

The result lands in `brands` (structured, loaded into every prompt) and in
`brand_memory` (prose, retrievable later).
"""

from __future__ import annotations

import base64
import colorsys
import io
import json
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

SAMPLE_SIZE = 220
QUANTIZE_COLORS = 12
MIN_SHARE = 0.02  # ignore colours occupying under 2% of the mark


@dataclass
class LogoAnalysis:
    palette: dict[str, str] = field(default_factory=dict)
    swatches: list[dict[str, Any]] = field(default_factory=list)
    has_transparency: bool = False
    width: int = 0
    height: int = 0
    style: str | None = None
    tone: str | None = None
    has_wordmark: bool | None = None
    summary: str | None = None

    def as_prose(self, brand_name: str) -> str:
        bits = [f"{brand_name} logo:"]
        if self.style:
            bits.append(self.style)
        if self.tone:
            bits.append(f"tone reads as {self.tone}")
        if self.palette:
            bits.append("colours " + ", ".join(f"{k} {v}" for k, v in self.palette.items()))
        return " ".join(bits)


# --------------------------------------------------------------------------- #
# colour, measured
# --------------------------------------------------------------------------- #
def _luminance(rgb: tuple[int, int, int]) -> float:
    def chan(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _saturation(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return colorsys.rgb_to_hsv(r, g, b)[1]


def _hue(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255 for c in rgb)
    return colorsys.rgb_to_hsv(r, g, b)[0]


def extract_palette(image_bytes: bytes) -> LogoAnalysis:
    """Count pixels. No model involved, so this cannot hallucinate a colour."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    result = LogoAnalysis(width=img.width, height=img.height)
    img = img.convert("RGBA")
    alpha = img.getchannel("A")
    result.has_transparency = alpha.getextrema()[0] < 250

    # Composite onto white so transparent logos do not contribute a black halo.
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=alpha)
    flat.thumbnail((SAMPLE_SIZE, SAMPLE_SIZE))

    quant = flat.quantize(colors=QUANTIZE_COLORS, method=Image.Quantize.FASTOCTREE)
    palette = quant.getpalette() or []
    total = sum(n for n, _ in quant.getcolors(1 << 16) or [])

    swatches: list[dict[str, Any]] = []
    for count, index in sorted(quant.getcolors(1 << 16) or [], reverse=True):
        rgb = tuple(palette[index * 3 : index * 3 + 3])
        if len(rgb) < 3:
            continue
        share = count / max(total, 1)
        if share < MIN_SHARE:
            continue
        swatches.append(
            {
                "hex": _hex(rgb),
                "rgb": list(rgb),
                "share": round(share, 4),
                "saturation": round(_saturation(rgb), 3),
                "luminance": round(_luminance(rgb), 3),
            }
        )
    result.swatches = swatches

    # Brand colour = the most-used colour that is not paper-white or ink-black.
    def is_neutral(sw: dict) -> bool:
        return sw["saturation"] < 0.12 and (sw["luminance"] > 0.88 or sw["luminance"] < 0.06)

    branded = [sw for sw in swatches if not is_neutral(sw)]
    branded.sort(key=lambda sw: sw["share"] * (0.4 + sw["saturation"]), reverse=True)

    if not branded:
        # A pure black-and-white mark is a legitimate answer, not a failure.
        result.palette = {
            "primary": "#111111",
            "secondary": "#FFFFFF",
            "accent": "#111111",
            "ink": "#FFFFFF",
        }
        return result

    primary = branded[0]
    accent = next(
        (
            sw
            for sw in branded[1:]
            if abs(_hue(tuple(sw["rgb"])) - _hue(tuple(primary["rgb"]))) > 0.08
        ),
        branded[1] if len(branded) > 1 else primary,
    )
    result.palette = {
        "primary": primary["hex"],
        "secondary": "#FFFFFF",
        "accent": accent["hex"],
        # Text laid over the primary colour: pick whichever actually reads.
        "ink": "#FFFFFF" if primary["luminance"] < 0.45 else "#111111",
    }
    return result


# --------------------------------------------------------------------------- #
# character, read
# --------------------------------------------------------------------------- #
VISION_PROMPT = """Look at this logo and answer as JSON only, no prose:

{"style": "<8 words max: geometric/hand-drawn/wordmark/emblem, modern/traditional>",
 "tone": "<3 words max, e.g. premium and calm / friendly and loud>",
 "has_wordmark": <true if the brand name is part of the mark>,
 "summary": "<one sentence a designer would write in a brand sheet>"}

Do not name the colours -- those are measured separately. Do not guess the
company's industry unless the mark makes it unambiguous."""


async def describe_logo(image_bytes: bytes, mime: str) -> dict[str, Any]:
    if not settings.anthropic_api_key or not settings.anthropic_model:
        log.warning("logo_vision_skipped", reason="no model configured")
        return {}
    from anthropic import AsyncAnthropic

    allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    media_type = mime if mime in allowed else "image/png"
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode(),
                        },
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }
        ],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _loads(text)


def _loads(text: str) -> dict[str, Any]:
    """Models wrap JSON in prose or fences more often than they should."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        log.warning("logo_vision_unparseable", head=text[:120])
        return {}


async def analyse(image_bytes: bytes, mime: str) -> LogoAnalysis:
    result = extract_palette(image_bytes)
    try:
        described = await describe_logo(image_bytes, mime)
    except Exception as exc:  # noqa: BLE001 - the palette alone is still useful
        log.error("logo_vision_failed", error=str(exc)[:200])
        described = {}
    result.style = described.get("style")
    result.tone = described.get("tone")
    result.has_wordmark = described.get("has_wordmark")
    result.summary = described.get("summary")
    return result
