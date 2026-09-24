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
    """The inspector gave no usable verdict. Treated as a failure, never a pass.

    Carries `cost_micros` like BackgroundRejected and CompositeRejected do: an
    attempt the model ANSWERED was paid for even when the answer was unusable,
    and an outage after the picture was bought does not un-buy the picture. The
    slide's row is the only place that loss is ever recorded, and it used to
    record nothing at all for this path.
    """

    def __init__(self, message: str, *, cost_micros: int = 0):
        super().__init__(message)
        self.cost_micros = int(cost_micros or 0)


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
# Enough for the JSON and a short note, and no more: the inspector is asked
# for a verdict, not an essay, and output tokens are the expensive half.
INSPECT_MAX_TOKENS = 300


@dataclass(slots=True)
class Verdict:
    reasons: list[str] = field(default_factory=list)
    notes: str = ""
    # What the inspector charged for this look. Vision calls used to be free
    # in the ledger and nowhere else: resp.usage was read and dropped, so a
    # creative's cost_micros told the owner the picture cost $0.29 when the
    # gate around it had spent more on top. Every caller adds this on.
    cost_micros: int = 0
    # Only a scored rubric fills this in (see `scored` on parse/inspect).
    score: int | None = None

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


def parse(text: str, keys: tuple[str, ...] = REASONS, *, scored: bool = False) -> Verdict:
    """Strict: every key must be present and boolean, or there is no verdict.

    `keys` is the rubric being answered -- this module's REASONS by default,
    finalgate's own list when the finished composite is what was looked at.
    `scored` additionally requires an integer 0-100, so a judge that answers
    "score": "pretty good" is an outage, not a pass. Strictness is the whole
    design: a gate that shrugs is a gate that is not there.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise InspectionUnavailable(f"no JSON in the inspector's answer: {text[:120]!r}")
    try:
        data: dict[str, Any] = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise InspectionUnavailable(f"unparseable verdict: {text[:120]!r}") from exc
    missing = [k for k in keys if not isinstance(data.get(k), bool)]
    if missing:
        raise InspectionUnavailable(f"verdict is missing {missing}")
    score = None
    if scored:
        score = data.get("score")
        # bool is an int in Python, and "score": true is not a score.
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
            raise InspectionUnavailable(f"verdict has no score in 0-100: {score!r}")
    return Verdict(
        [k for k in keys if data[k]],
        str(data.get("notes") or "")[:200],
        score=score,
    )


def cost_micros(usage: Any) -> int:
    """What one inspection cost, in micro-dollars, from the reply's own usage.

    Priced off the settings rather than a constant because the inspector model
    is a setting: the same code runs against whichever id ANTHROPIC_MODEL
    names. An outage, or an SDK that reports no usage, costs 0 here rather
    than guessing -- an invented number on the ledger is worse than a missing
    one.
    """
    if usage is None:
        return 0
    read = int(getattr(usage, "input_tokens", 0) or 0)
    wrote = int(getattr(usage, "output_tokens", 0) or 0)
    return round(
        read * settings.inspector_input_micros_per_ktok / 1000
        + wrote * settings.inspector_output_micros_per_ktok / 1000
    )


async def _ask(image: bytes, prompt: str = PROMPT) -> tuple[str, int]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=INSPECT_MAX_TOKENS,
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
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, cost_micros(getattr(resp, "usage", None))


async def inspect(
    image: bytes,
    *,
    prompt: str = PROMPT,
    keys: tuple[str, ...] = REASONS,
    scored: bool = False,
) -> Verdict:
    """Look at one image against one rubric. Raises InspectionUnavailable, never guesses.

    The defaults are this module's own: the generated BACKGROUND, before any
    text or mark is laid on it. finalgate passes its own prompt and keys to
    look at the finished composite through the same client, the same retries
    and the same refusal to guess.

    An attempt the model answered cost money even when the answer was
    unusable, so the spend accumulates across attempts and rides home on the
    Verdict.
    """
    if not available():
        raise InspectionUnavailable(
            "no vision model configured (ANTHROPIC_API_KEY / ANTHROPIC_MODEL); generated "
            "pictures cannot be inspected, so none can be delivered"
        )
    small = await asyncio.to_thread(_thumbnail, image)
    last: Exception | None = None
    spent = 0
    for attempt in range(1, INSPECT_ATTEMPTS + 1):
        try:
            text, cost = await _ask(small, prompt)
            spent += cost
            verdict = parse(text, keys, scored=scored)
            verdict.cost_micros = spent
            return verdict
        except Exception as exc:  # noqa: BLE001 - API error or unusable answer: ask again
            last = exc
            log.warning("inspection_retry", attempt=attempt, error=repr(exc)[:200])
            await asyncio.sleep(min(2.0 * attempt, 6.0))
    raise InspectionUnavailable(
        f"inspector failed {INSPECT_ATTEMPTS} times: {last!r}"[:300], cost_micros=spent
    )
