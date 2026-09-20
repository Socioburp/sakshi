"""The background gate: every generated picture is looked at before it is used.

What the compositor guarantees, it guarantees by construction. What the image
model returns cannot be guaranteed by prompting -- a "no text" instruction
lowers the rate of stray lettering, it does not make it zero -- so it is
guaranteed by REJECTION instead: look at the picture, and if it shows any of
the things below, throw it away and ask again with a prompt that says so.

    text_or_lettering    letters, numerals, labels, signage, pseudo-writing
    pagination_dots      a row of small dots, as under an app carousel
    slide_numbers        "1/6", "2 of 5", page counters
    ui_chrome            buttons, icons, status bars, like/share glyphs
    border_or_frame      a border, a card edge, a picture-in-picture panel
    watermark            a watermark, stock-site overlay, signature, logo
    phone_frame          a phone, tablet or browser mock-up around the picture
    visible_artefacts    melted or duplicated objects, broken anatomy, seams
    subject_cropped      the main subject cut off by the edge of the frame

The inspector is the vision model the product already talks to (the same
client logo.py uses). It is asked one narrow question and must answer in JSON.

This module FAILS CLOSED. If the inspector cannot be reached or does not give
a usable answer, the picture is not "probably fine": `InspectionUnavailable`
is raised and the slide fails. No image reaches a client uninspected.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


class InspectionUnavailable(RuntimeError):
    """The inspector gave no usable verdict. Treated as a failure, never a pass."""


# reason -> the sentence added to the NEXT attempt's prompt. Written as what the
# photograph should be, so it helps a vendor that ignores prohibitions too.
CORRECTIONS: dict[str, str] = {
    "text_or_lettering": (
        "Every surface, package, wall and object is completely blank and unmarked: "
        "no lettering, numerals, labels or signage anywhere in the frame."
    ),
    "pagination_dots": (
        "The lower edge of the photograph is plain, continuous scene with nothing "
        "overlaid: no dots, markers or indicators."
    ),
    "slide_numbers": "There are no numbers, counters or fractions anywhere in the frame.",
    "ui_chrome": (
        "This is a plain photograph straight from a camera, with no interface elements, "
        "buttons, icons or overlays of any kind."
    ),
    "border_or_frame": (
        "The scene runs uninterrupted to all four edges of the image: no border, no "
        "frame, no card, no inset panel."
    ),
    "watermark": "The image is clean, with no watermark, overlay, signature or logo.",
    "phone_frame": (
        "The photograph IS the whole image; it is not shown on a phone, screen or mock-up."
    ),
    "visible_artefacts": (
        "Every object is physically coherent, singular and correctly formed, with natural "
        "anatomy and clean edges."
    ),
    "subject_cropped": (
        "The entire main subject sits comfortably inside the frame with generous empty "
        "margin on all four sides; nothing important touches an edge."
    ),
    # raised by the pipeline, not by the inspector
    "duplicate_of_earlier_slide": (
        "A clearly different composition from before: a different angle, distance and arrangement."
    ),
    "wrong_size": "",
}
REASONS = tuple(k for k in CORRECTIONS if k not in ("duplicate_of_earlier_slide", "wrong_size"))

PROMPT = (
    "You are the quality gate for a photographic BACKGROUND image. Headline text and a logo "
    "are added later by other software, so this image must contain none of its own. Look "
    "carefully, including along the bottom edge and in the corners, and answer with JSON "
    "only -- one boolean per key, true when the problem IS present:\n"
    "{\n"
    '  "text_or_lettering": letters, words, numerals, labels, signage or pseudo-writing '
    "anywhere, however small or garbled,\n"
    '  "pagination_dots": a row of small dots or dashes like an app carousel indicator,\n'
    '  "slide_numbers": page or slide counters such as 1/6,\n'
    '  "ui_chrome": buttons, icons, status bars, hearts, share or menu glyphs,\n'
    '  "border_or_frame": a border, frame, card edge, rounded-corner panel or inset picture,\n'
    '  "watermark": a watermark, stock overlay, signature or logo,\n'
    '  "phone_frame": a phone, tablet, laptop or browser mock-up surrounding the picture,\n'
    '  "visible_artefacts": melted, fused or duplicated objects, malformed hands or faces, '
    "seams or tiling,\n"
    '  "subject_cropped": the main subject is cut off by the edge of the frame,\n'
    '  "notes": "<=20 words on what you saw, empty if clean"\n'
    "}\n"
    "Be strict: if you are unsure whether marks are lettering, answer true."
)

INSPECT_ATTEMPTS = 3
INSPECT_LONG_EDGE = 1024


@dataclass(slots=True)
class Verdict:
    reasons: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def ok(self) -> bool:
        return not self.reasons


def available() -> bool:
    return bool(settings.anthropic_api_key and settings.anthropic_model)


def corrected(prompt: str, reasons: list[str]) -> str:
    """The prompt for the next attempt: the same scene, plus what went wrong."""
    adds = [CORRECTIONS[r] for r in dict.fromkeys(reasons) if CORRECTIONS.get(r)]
    adds = [a for a in adds if a not in prompt]
    return f"{prompt.rstrip()} {' '.join(adds)}".strip() if adds else prompt


def _thumbnail(image: bytes) -> bytes:
    """A copy for the inspector only. The picture itself is never touched."""
    from PIL import Image

    with Image.open(BytesIO(image)) as im:
        im = im.convert("RGB")
        im.thumbnail((INSPECT_LONG_EDGE, INSPECT_LONG_EDGE), Image.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=90)
        return out.getvalue()


def parse(text: str) -> Verdict:
    """Strict: every key must be present and boolean, or there is no verdict."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise InspectionUnavailable(f"no JSON in the inspector's answer: {text[:120]!r}")
    try:
        data: dict[str, Any] = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise InspectionUnavailable(f"unparseable verdict: {text[:120]!r}") from exc
    missing = [k for k in REASONS if not isinstance(data.get(k), bool)]
    if missing:
        raise InspectionUnavailable(f"verdict is missing {missing}")
    return Verdict([k for k in REASONS if data[k]], str(data.get("notes") or "")[:200])


async def _ask(image: bytes) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=300,
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
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


async def inspect(image: bytes) -> Verdict:
    """Look at one generated background. Raises InspectionUnavailable, never guesses."""
    if not available():
        raise InspectionUnavailable(
            "no vision model configured (ANTHROPIC_API_KEY / ANTHROPIC_MODEL); generated "
            "pictures cannot be inspected, so none can be delivered"
        )
    small = await asyncio.to_thread(_thumbnail, image)
    last: Exception | None = None
    for attempt in range(1, INSPECT_ATTEMPTS + 1):
        try:
            return parse(await _ask(small))
        except Exception as exc:  # noqa: BLE001 - API error or unusable answer: ask again
            last = exc
            log.warning("inspection_retry", attempt=attempt, error=repr(exc)[:200])
            await asyncio.sleep(min(2.0 * attempt, 6.0))
    raise InspectionUnavailable(f"inspector failed {INSPECT_ATTEMPTS} times: {last!r}"[:300])
