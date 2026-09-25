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

# Reasons that NOTHING this gate is allowed to do can change, so re-testing
# them is buying the same verdict twice.
#
# logo_problem is a property of the BRAND'S MARK. The mark is built from the
# brand kit -- the same image, or the same name set as the same wordmark -- and
# it is laid on every template in the same place, over whatever picture the
# frame carries. A brand with no logo image scored logo_problem on all four
# layouts of one real creative and on every picture bought for it: 62 cents and
# five and a half minutes, one credit refunded, nothing delivered.
#
# Nothing else in the rubric belongs here, and it is worth saying why, because
# the temptation is to add the ones that FEEL fixed. text_cut_off covers a
# glyph that did not render, which no layout cures -- but it also covers a
# letter clipped by a panel, which a layout cures every day, and the inspector
# gives one boolean for both; a flag that is sometimes curable is treated as
# curable, because refusing to look is how a deliverable card gets thrown away.
# text_hard_to_read, text_covers_subject, subject_cut_off and looks_unfinished
# all describe where the words fell on THIS picture in THIS layout, and both of
# those move. stray_text_in_photo and artefacts are the picture's own fault:
# invariant to a layout change, which is why PICTURE_REASONS skips the ladder,
# but curable by the one thing worth buying another picture for.
INVARIANT_REASONS = frozenset({"logo_problem"})

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


def settled(reasons: list[str]) -> list[str]:
    """The reasons that make this verdict final: it cannot be argued out of.

    A frame carrying one of these is refused exactly as before -- it is just
    refused ONCE, instead of once per layout and once per picture bought.
    """
    return [r for r in reasons if r in INVARIANT_REASONS]


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

# The deterministic checker's codes answer to the same sentences. A regeneration
# is asked for by whichever of the two refused the frame, and a prompt that
# corrected only the inspector's half would buy a second picture with the
# measured fault still in it.
CORRECTIONS |= {
    "contrast_below_bar": CORRECTIONS["text_hard_to_read"],
    "scrim_saturated": CORRECTIONS["text_hard_to_read"],
    "text_over_subject": CORRECTIONS["text_covers_subject"],
    "subject_cut_by_window": CORRECTIONS["subject_cut_off"],
}


# What the inspector is looking at where the mark should be. A brand with no
# logo image gets its NAME set as letterspaced type (compose._brand_context
# falls back to it), and an inspector told only "a logo or brand name" reads
# that as a logo that is not there -- which it then reports, on every layout
# and over every picture, for ever. So it is told which of the two it is, and
# when there is no image the wordmark is judged as a wordmark.
_MARK_WITH_LOGO = (
    '  "logo_problem": a logo or brand name that is missing where one is expected, '
    "stretched, distorted, illegible, or on a ground that clashes with it,\n"
)
_MARK_WORDMARK_ONLY = (
    '  "logo_problem": this brand has NO logo image, so its name is set as type instead -- '
    "that wordmark is the mark, and it is correct for the card to carry no other. Judge the "
    "wordmark itself: true only when it is clipped, stretched, distorted, illegible, or on a "
    "ground that clashes with it. Never answer true because no logo image appears,\n"
)


def prompt_for(
    headline: str = "",
    subhead: str = "",
    cta: str = "",
    brand: str = "",
    has_logo: bool = True,
) -> str:
    """The rubric, carrying the copy the card is supposed to show.

    Without the copy the inspector cannot do the one thing it is here for that
    no measurement can: tell the headline apart from lettering the image model
    invented, and notice that a Devanagari conjunct came out as a tofu box.

    `has_logo` says whether the brand has a logo IMAGE. It is not a hint: with
    it wrong, a brand that has no mark to show is refused for not showing one.
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
        + (_MARK_WITH_LOGO if has_logo else _MARK_WORDMARK_ONLY)
        + '  "stray_text_in_photo": lettering, numerals, labels, signage or pseudo-writing '
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
    has_logo: bool = True,
) -> Verdict:
    """Look at one finished creative. Raises InspectionUnavailable, never guesses.

    `image` is the EXPORTED JPEG -- what the client receives, byte for byte --
    scaled to a 1024px long edge for the inspector only. Inspecting the PNG
    before export would miss exactly the faults the export can introduce.
    """
    verdict = await bggate.inspect(
        image,
        prompt=prompt_for(headline, subhead, cta, brand, has_logo),
        keys=KEYS,
        scored=True,
    )
    # A brand with no logo still gets their post. The owner's rule, in their
    # own words: "if the logo is not there you should create the image
    # regardless." One real client had every creative refused for
    # logo_problem, run after run, over a mark that does not exist -- and no
    # logo they have not uploaded will ever satisfy an inspector asking to see
    # one, so the refusal was permanent rather than corrective.
    #
    # This drops the complaint, it does not ignore the mark. The compositor
    # guarantees the wordmark by construction, before any model is asked: it
    # is sized from the canvas (compose.logo_box), held above its own contrast
    # bar (legibility.bar_for("logo")) and refused outright if it would be
    # clipped. Those guarantees are measured on the rendered frame and do not
    # depend on anyone's opinion. What is dropped here is a second opinion
    # about a thing we already prove.
    if not has_logo and "logo_problem" in verdict.reasons:
        verdict.reasons = [r for r in verdict.reasons if r != "logo_problem"]
        log.info("final_gate_logo_absent", notes=verdict.notes, score=verdict.score)
    # The notes are the whole point of asking a model rather than a ruler: they
    # say WHAT it saw. The background gate has always logged them -- that line
    # is how we learned a clock's hour markers were being read as lettering --
    # and this one dropped them, so a client's creative was refused for
    # "logo_problem" three times running with nobody able to say why.
    # has_logo rides along because the same reason means two different things:
    # a mark that is wrong, or a mark that is not there.
    log.info(
        "final_gate",
        reasons=verdict.reasons,
        score=verdict.score,
        notes=verdict.notes,
        has_logo=has_logo,
        cost_micros=verdict.cost_micros,
    )
    return verdict


__all__ = [
    "AS_CODE",
    "CORRECTIONS",
    "INVARIANT_REASONS",
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
    "settled",
]
