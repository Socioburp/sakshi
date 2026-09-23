"""The composite gate: somebody looks at the finished card before it is sent.

bggate looks at the generated BACKGROUND -- before the text, the logo, the
scrim, the window crop and the JPEG encode -- and never looks at the owner's
photograph, the product studio, a reused background or a recompose at all.
compositeqa measures the finished frame, but it can only measure what it knows
to measure: it cannot tell a misrendered Devanagari glyph from a correct one,
or notice that the model wrote a word into the tablecloth.

So the exported JPEG -- the byte-exact deliverable, not a proxy for it -- is
shown to the same vision model bggate uses, against its own rubric:

    text_cut_off         a letter clipped by an edge, a panel or another element
    text_hard_to_read    low contrast, or a busy picture behind the words
    text_covers_subject  words or a panel hiding the subject, face or product
    subject_cut_off      the subject cut by the frame, panel or card edge
    logo_problem         missing, distorted, illegible or on clashing ground
    stray_text_in_photo  lettering in the photograph that is not the copy
    artefacts            melted or duplicated objects, broken hands or faces
    looks_unfinished     dead areas, unbalanced, amateur

...plus a score out of 100 and a short note. The supplied copy goes IN the
prompt, so the inspector can tell the headline it is meant to see from
lettering the image model invented, and can say when a glyph came out wrong.

This module FAILS CLOSED, exactly as bggate does: a flag that is not a
boolean, a missing key, a score outside 0-100 or an inspector that cannot be
reached all raise InspectionUnavailable. Nothing ships uninspected.
"""

from __future__ import annotations

from app.creative import bggate, shotplan
from app.creative.bggate import InspectionUnavailable, Verdict
from app.logging import get_logger

log = get_logger(__name__)

# true = the problem IS present, the same polarity as bggate's rubric.
KEYS: tuple[str, ...] = (
    "text_cut_off",
    "text_hard_to_read",
    "text_covers_subject",
    "subject_cut_off",
    "logo_problem",
    "stray_text_in_photo",
    "artefacts",
    "looks_unfinished",
)

# Which of those are the PICTURE's fault rather than the arrangement's. Only
# these can be cured by buying another picture; everything else is cured by
# setting the same picture differently, which is free. The split decides
# whether the owner's money is spent, so it is written down rather than
# inferred at the call site.
PICTURE_REASONS = frozenset({"stray_text_in_photo", "artefacts"})

# What each reason means to the deterministic checker, so ONE repair ladder can
# be steered by either of them: the cure for "the words are on the jar" does
# not depend on who noticed it. The two picture faults map to nothing, because
# no arrangement of a photograph with a word baked into it is acceptable --
# those are the ones worth buying another picture for.
AS_CODE: dict[str, str] = {
    "text_cut_off": "headline_at_floor",
    "text_hard_to_read": "scrim_saturated",
    "text_covers_subject": "text_over_subject",
    "subject_cut_off": "subject_cut_by_window",
}


def as_codes(reasons: list[str]) -> set[str]:
    """The inspector's reasons in the repair ladder's vocabulary."""
    return {AS_CODE[r] for r in reasons if r in AS_CODE}


def picture_faults(reasons: list[str]) -> list[str]:
    """The reasons only a different PICTURE can cure."""
    return [r for r in reasons if r in PICTURE_REASONS]


# reason -> the sentence added to a regeneration prompt, in the same voice as
# bggate.CORRECTIONS: what the photograph should BE, so it helps a vendor that
# ignores prohibitions. The three that describe the picture itself reuse
# bggate's wording exactly -- one rubric, one vocabulary.
CORRECTIONS: dict[str, str] = {
    "stray_text_in_photo": bggate.CORRECTIONS["text_or_lettering"],
    "artefacts": bggate.CORRECTIONS["visible_artefacts"],
    "subject_cut_off": bggate.CORRECTIONS["subject_cropped"],
    "text_covers_subject": (
        "The main subject sits entirely clear of the area the words occupy, with the "
        "rest of the frame calm and uncluttered behind them."
    ),
    "text_hard_to_read": (
        "The area behind the words is an even, uncluttered tone -- no highlights, "
        "no busy detail and no strong edges running through it."
    ),
    "looks_unfinished": (
        "A balanced, finished photograph: one clear subject, deliberate composition, "
        "no empty dead corners."
    ),
}


def prompt_for(
    headline: str = "",
    subhead: str = "",
    cta: str = "",
    brand: str = "",
) -> str:
    """The rubric, carrying the copy the card is supposed to show.

    Without the copy the inspector cannot do the one thing it is here for that
    no measurement can: tell the headline apart from lettering the image model
    invented, and notice that a Devanagari conjunct came out as a tofu box.
    """
    wanted = [
        f'  headline: "{headline}"' if headline else "",
        f'  subhead: "{subhead}"' if subhead else "",
        f'  call to action: "{cta}"' if cta else "",
        f'  brand name: "{brand}"' if brand else "",
    ]
    copy = "\n".join(line for line in wanted if line) or "  (none supplied)"
    return (
        "You are the final quality gate for a FINISHED social-media creative that is about "
        "to be sent to a paying client. It is a photograph with a headline, possibly a "
        "subhead and a call-to-action button, and possibly a logo or brand name laid over "
        "it by software.\n"
        "The text that SHOULD appear, exactly:\n"
        f"{copy}\n"
        "Any other lettering is a fault in the photograph. Compare the words you can see "
        "with the list: a character that is a box, a blank, a broken conjunct or otherwise "
        "not the supplied text is text_cut_off.\n"
        "Look carefully at all four edges and at every letter, then answer with JSON only "
        "-- one boolean per key, true when the problem IS present:\n"
        "{\n"
        '  "text_cut_off": any letter clipped or cut by an edge of the image, by a panel, '
        "by the button, or by another element; or a character that did not render,\n"
        '  "text_hard_to_read": any words that are hard to read against what is behind '
        "them -- too little contrast, or busy detail through the letters,\n"
        '  "text_covers_subject": the words, the button or a panel sit on top of the main '
        "subject, a face, or the product, hiding part of it,\n"
        '  "subject_cut_off": the main subject is cut off by the edge of the image or by a '
        "panel, band or card edge,\n"
        '  "logo_problem": a logo or brand name that is missing where one is expected, '
        "stretched, distorted, illegible, or on a ground that clashes with it,\n"
        '  "stray_text_in_photo": lettering, numerals, labels, signage or pseudo-writing '
        "INSIDE the photograph that is not the supplied copy, however small or garbled,\n"
        '  "artefacts": melted, fused or duplicated objects, malformed hands or faces, '
        "seams or tiling,\n"
        '  "looks_unfinished": empty dead areas, badly unbalanced composition, or work '
        "that would read to a client as amateur,\n"
        '  "score": <integer 0-100, how good this is as a piece of paid-for design work>,\n'
        '  "notes": "<=25 words on what you saw, empty if clean"\n'
        "}\n"
        "Be strict: this is the last look before a client who paid a lot sees it. If you "
        "are unsure whether something is a fault, answer true."
    )


def corrected(
    prompt: str,
    reasons: list[str],
    *,
    template: str | None = None,
    text_box: tuple[float, float, float, float] | None = None,
) -> str:
    """The image prompt for the one regeneration this gate may buy.

    bggate's corrections say what was wrong with the picture. This adds the
    thing bggate never knew: which band of the frame the words are about to
    occupy, so the model keeps it clear instead of being told off for it
    afterwards. The layout sentence is the one the first prompt already uses,
    so the two never contradict each other.
    """
    adds = [CORRECTIONS[r] for r in dict.fromkeys(reasons) if CORRECTIONS.get(r)]
    if template and any(r in ("text_covers_subject", "text_hard_to_read") for r in reasons):
        adds.append(shotplan.copy_space_clause(template, text_box))
    adds = [a for a in adds if a and a not in prompt]
    return f"{prompt.rstrip()} {' '.join(adds)}".strip() if adds else prompt


def available() -> bool:
    return bggate.available()


async def inspect(
    image: bytes,
    *,
    headline: str = "",
    subhead: str = "",
    cta: str = "",
    brand: str = "",
) -> Verdict:
    """Look at one finished creative. Raises InspectionUnavailable, never guesses.

    `image` is the EXPORTED JPEG -- what the client receives, byte for byte --
    scaled to a 1024px long edge for the inspector only. Inspecting the PNG
    before export would miss exactly the faults the export can introduce.
    """
    verdict = await bggate.inspect(
        image,
        prompt=prompt_for(headline, subhead, cta, brand),
        keys=KEYS,
        scored=True,
    )
    log.info(
        "final_gate",
        reasons=verdict.reasons,
        score=verdict.score,
        cost_micros=verdict.cost_micros,
    )
    return verdict


__all__ = [
    "AS_CODE",
    "CORRECTIONS",
    "KEYS",
    "PICTURE_REASONS",
    "InspectionUnavailable",
    "Verdict",
    "as_codes",
    "available",
    "corrected",
    "inspect",
    "picture_faults",
    "prompt_for",
]
