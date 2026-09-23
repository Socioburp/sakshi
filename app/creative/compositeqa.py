"""The last look at the finished frame, before anyone is asked to pay for it.

Everything upstream checks a PART. The layout gate proves the copy can be set
on a blank page. The background gate looks at the generated picture as the
vendor returned it -- before the text, the logo, the scrim, the window crop
and the JPEG encode. Nothing, human or machine, has ever looked at what the
client actually receives.

This module does, deterministically and for nothing: it reads the report the
compositor already produced for the frame it just rendered, re-derives the
crop the compositor made, and answers two questions the owner asks in those
words -- "is the text cropped?" and "is the image cropped?".

    shrink / at_floor        how far each piece of type had to come down
    photo_visible_share      how much of the source picture the layout shows
    text_over_photo_share    how much of the picture the words sit on
    scrim_saturated          the gradient or a plate standing at its cap
    contrast                 measured on the rendered frame (by the compositor)
    subject_cut_by_window    the share of the subject the window does not show
    text_over_subject        the share of the subject the words sit on

It returns two lists and a number. FAULTS are frames that must not ship as
they are. REPAIRS are frames that should ship better if a free variant does
better, and that ship as they are if none does -- a guarantee that refuses a
valid job is a bug, so "the headline is at its floor" can ask for another
template but can never, on its own, refuse the creative. The number is a
0-100 score for ranking variants against each other.

The vocabulary lives HERE, not in pipeline.py: tests/test_background_gate.py
reads the pipeline's source and bans severity words in it, and it is right
to -- a quality knob named in the pipeline is a quality knob someone will
turn down under load. These are codes, not severities, and there is no
setting anywhere that makes a frame with a fault acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.creative import compose, legibility
from app.logging import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# the bar
# --------------------------------------------------------------------------- #
# The share of the subject a window may cut before the frame is refused.
# Measured on real renders: a 4:5 photograph whose subject sits low, shown
# through split_card's window, loses 35% of the jar -- the panel beheads it and
# every check upstream passed. A subject-aware crop lands at 0.0, and the
# rounding of a crop that only just fits sits under 0.02. 0.08 is clear of the
# arithmetic and nowhere near a real beheading.
SUBJECT_CUT_MAX = 0.08
# The share of the subject the words may sit on. FIT_JS already refuses type
# that overlaps a PLACED subject (the product lane's cut-out, which it knows as
# an element); this is the same rule for a subject the layout only found out
# about afterwards -- an owner's photograph, or a generated picture. A word
# clipping the corner of a jar reads as a mistake at about a fifth of it; 0.15
# leaves room for the descender of a headline overhanging a shadow.
TEXT_OVER_SUBJECT_MAX = 0.15
# A picture whose window shows less than this much of it has been cropped, not
# framed. Panel layouts legitimately show 45-70% of a 4:5 source, so this is
# NOT a fault -- it is what ranking is for. Below a third, the layout is
# showing a detail of a photograph the owner chose as a whole.
PHOTO_VISIBLE_MIN = 0.33
# Type is "at its floor" when the binary search could not keep a pixel more.
FLOOR_SLACK_PX = 0.5

FAULTS = (
    "subject_cut_by_window",
    "text_over_subject",
    "contrast_below_bar",
    "photo_mostly_cropped",
    "export_size_wrong",
)
REPAIRS = ("headline_at_floor", "type_shrunk", "scrim_saturated")


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
Box = tuple[float, float, float, float]

# Elements whose ink the owner reads. `rule` is a hairline and `subject` is the
# placed product itself, so neither counts as words sitting on a picture.
INK = ("headline", "subhead", "cta", "brandline", "logo")


def _area(box: Box | None) -> float:
    if box is None:
        return 0.0
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _overlap(a: Box | None, b: Box | None) -> float:
    if a is None or b is None:
        return 0.0
    return _area(
        (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])),
    )


def _as_box(raw: dict | list | tuple | None) -> Box | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return float(raw["l"]), float(raw["t"]), float(raw["r"]), float(raw["b"])
    left, top, right, bottom = raw
    return float(left), float(top), float(right), float(bottom)


def ink_boxes(report: dict) -> list[Box]:
    """Every piece of type on the canvas, as boxes."""
    boxes = report.get("boxes") or {}
    return [b for b in (_as_box(boxes.get(cls)) for cls in INK) if b is not None]


def subject_on_canvas(
    subject: tuple[int, int, int, int],
    fit: compose.PictureFit,
    window: tuple[int, int, int, int],
) -> Box:
    """The subject's box in the source photograph, put where it lands on the
    finished canvas -- through the crop the compositor made and the window the
    layout shows it in. Not clipped: the part that falls outside the window is
    exactly what was cut off, and the caller needs to see it."""
    sx, sy = fit.scale, fit.offset
    x0, y0, x1, y1 = subject
    return (
        window[0] + x0 * sx + sy[0],
        window[1] + y0 * sx + sy[1],
        window[0] + x1 * sx + sy[0],
        window[1] + y1 * sx + sy[1],
    )


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Metrics:
    shrink: dict[str, float] = field(default_factory=dict)
    at_floor: list[str] = field(default_factory=list)
    photo_visible_share: float = 1.0
    text_over_photo_share: float = 0.0
    scrim_saturated: bool = False
    contrast: dict[str, float] = field(default_factory=dict)
    subject_cut_by_window: float = 0.0
    text_over_subject: float = 0.0
    fit_mode: str = "whole"

    def as_dict(self) -> dict:
        return {
            "shrink": {k: round(v, 3) for k, v in self.shrink.items()},
            "at_floor": list(self.at_floor),
            "photo_visible_share": round(self.photo_visible_share, 3),
            "text_over_photo_share": round(self.text_over_photo_share, 3),
            "scrim_saturated": self.scrim_saturated,
            "contrast": dict(self.contrast),
            "subject_cut_by_window": round(self.subject_cut_by_window, 3),
            "text_over_subject": round(self.text_over_subject, 3),
            "fit_mode": self.fit_mode,
        }


@dataclass(slots=True)
class Assessment:
    faults: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    score: int = 100
    metrics: Metrics = field(default_factory=Metrics)
    notes: str = ""

    @property
    def ok(self) -> bool:
        """No fault. A repair is not a refusal -- see the module docstring."""
        return not self.faults

    @property
    def worth_a_variant(self) -> bool:
        """Worth spending a second of Chromium on another template."""
        return bool(self.faults or self.repairs)

    def as_dict(self) -> dict:
        return {
            "faults": list(self.faults),
            "repairs": list(self.repairs),
            "score": self.score,
            "metrics": self.metrics.as_dict(),
            "notes": self.notes,
        }


def _floors(width: int) -> dict[str, float]:
    return {
        "headline": round(width * compose.HEADLINE_MIN),
        "subhead": round(width * compose.SUBHEAD_MIN),
        "cta": round(width * compose.CTA_MIN),
    }


def _shrink(report: dict, width: int) -> tuple[dict[str, float], list[str]]:
    floors = _floors(width)
    shrink: dict[str, float] = {}
    at_floor: list[str] = []
    for cls, size in (report.get("sizes") or {}).items():
        if not size or not size.get("design"):
            continue
        px, design = float(size["px"]), float(size["design"])
        shrink[cls] = px / design
        floor = floors.get(cls)
        if floor is not None and px <= floor + FLOOR_SLACK_PX and px < design - FLOOR_SLACK_PX:
            at_floor.append(cls)
    return shrink, at_floor


def _saturated(legible: dict) -> bool:
    if float(legible.get("boost") or 1.0) >= legibility.MAX_BOOST - 1e-6:
        return True
    return any(
        float(p.get("alpha") or 0.0) >= legibility.LOCAL_MAX - 1e-6
        for p in (legible.get("plates") or [])
    )


def _contrast_short(contrast: dict[str, float]) -> dict[str, float]:
    """Elements under their bar on the frame that was rendered.

    The compositor already refuses these (it plates until they clear, and
    raises when a plate at full strength cannot). This reads the same numbers
    back off the delivered report, held to the same bar including the same
    measurement slack, so the two can never disagree -- and so a frame that
    reached here from anywhere else is still checked. A guarantee with one
    caller is a guarantee that stops working the day someone adds a second.
    """
    short = {}
    for cls, ratio in contrast.items():
        bar = legibility.bar_for(cls) - legibility.MEASURE_SLACK
        if float(ratio) < bar:
            short[cls] = float(ratio)
    return short


def assess(
    report: dict,
    *,
    template: str,
    jpeg: bytes | None = None,
    source_size: tuple[int, int] | None = None,
    subject: tuple[int, int, int, int] | None = None,
    focus: tuple[int, int, int, int] | None = None,
) -> Assessment:
    """Look at one finished slide and say what is wrong with it.

    `report` is what compose.compose_with_report returned; `jpeg` the bytes
    that would be delivered. `source_size` and `subject` describe the picture
    that went in -- the subject's box in the SOURCE photograph's own upright
    pixels -- and `focus` is what fit_background was given to crop around, so
    the crop it made is reproduced exactly rather than guessed at. A slide
    with no located subject is still measured for everything else.

    Costs nothing and calls nobody: every number here was already paid for.
    """
    canvas = report.get("canvas") or [0, 0]
    width, height = int(canvas[0]), int(canvas[1])
    window = tuple(report.get("photo_window") or (0, 0, width, height))
    win_box: Box = (float(window[0]), float(window[1]), float(window[2]), float(window[3]))
    legible = report.get("legibility") or {}

    metrics = Metrics()
    metrics.shrink, metrics.at_floor = _shrink(report, width)
    metrics.contrast = dict(legible.get("contrast") or {})
    metrics.scrim_saturated = _saturated(legible)

    inks = ink_boxes(report)
    if _area(win_box):
        over = sum(_overlap(ink, win_box) for ink in inks)
        metrics.text_over_photo_share = min(1.0, over / _area(win_box))

    # The product lane stands its cut-out on the canvas and FIT_JS reports it
    # as an element, so that box is already where it lands. Every other lane
    # fitted a picture into the window and the placement must be re-derived.
    placed: Box | None = _as_box((report.get("boxes") or {}).get("subject"))
    if source_size is not None:
        fit = compose.picture_fit(
            source_size,
            (int(window[2] - window[0]), int(window[3] - window[1])),
            focus=focus,
        )
        metrics.fit_mode = fit.mode
        whole = _area((0.0, 0.0, float(source_size[0]), float(source_size[1])))
        metrics.photo_visible_share = min(1.0, _area(_as_box(fit.visible)) / max(1.0, whole))
        if placed is None and subject is not None:
            placed = subject_on_canvas(subject, fit, window)

    if placed is not None and _area(placed) > 0:
        shown = _overlap(placed, win_box)
        metrics.subject_cut_by_window = max(0.0, 1.0 - shown / _area(placed))
        # Only the part of the subject the window actually shows can be
        # covered by words; the part the panel cut off is already counted
        # against the crop, and counting it twice would send the repair
        # ladder after the wrong problem.
        seen: Box = (
            max(placed[0], win_box[0]),
            max(placed[1], win_box[1]),
            min(placed[2], win_box[2]),
            min(placed[3], win_box[3]),
        )
        if _area(seen) > 0:
            metrics.text_over_subject = min(
                1.0, sum(_overlap(ink, seen) for ink in inks) / _area(seen)
            )

    faults: list[str] = []
    repairs: list[str] = []
    notes: list[str] = []

    if metrics.subject_cut_by_window > SUBJECT_CUT_MAX:
        faults.append("subject_cut_by_window")
        notes.append(f"the window cuts {metrics.subject_cut_by_window:.0%} of the subject")
    if metrics.text_over_subject > TEXT_OVER_SUBJECT_MAX:
        faults.append("text_over_subject")
        notes.append(f"words cover {metrics.text_over_subject:.0%} of the subject")
    short = _contrast_short(metrics.contrast)
    if short:
        faults.append("contrast_below_bar")
        notes.append(", ".join(f"{cls} {ratio:.2f}:1" for cls, ratio in sorted(short.items())))
    if metrics.photo_visible_share < PHOTO_VISIBLE_MIN:
        faults.append("photo_mostly_cropped")
        notes.append(f"the layout shows {metrics.photo_visible_share:.0%} of the picture")
    if jpeg is not None:
        got = compose.image_size(jpeg)
        if got != (width, height):
            faults.append("export_size_wrong")
            notes.append(f"exported {got[0]}x{got[1]}, not {width}x{height}")

    if "headline" in metrics.at_floor:
        repairs.append("headline_at_floor")
    elif metrics.at_floor:
        repairs.append("type_shrunk")
    if metrics.scrim_saturated and template in compose.TYPE_OVER_PHOTO:
        repairs.append("scrim_saturated")

    assessment = Assessment(
        faults=faults,
        repairs=repairs,
        score=score_of(metrics),
        metrics=metrics,
        notes="; ".join(notes)[:300],
    )
    if faults or repairs:
        log.info(
            "composite_qa",
            template=template,
            faults=faults,
            repairs=repairs,
            score=assessment.score,
            **metrics.as_dict(),
        )
    return assessment


# How much each thing the owner can see is worth, out of 100. The order is
# their order: a subject the frame cut off is the worst thing on the card, the
# words sitting on it next, the picture cropped down next, and type that had
# to shrink last -- it is the only one of the four a reader cannot name.
WEIGHTS = {
    "subject_cut": 45.0,
    "text_over_subject": 30.0,
    "picture_cropped": 22.0,
    "type_shrunk": 18.0,
    "scrim_saturated": 8.0,
    "contrast": 40.0,
}


def score_of(metrics: Metrics) -> int:
    """0-100, for ranking variants of the same slide against each other.

    Never a pass mark: a frame scoring 96 with a fault does not ship, and a
    frame scoring 61 with none of them does. This orders the free variants the
    repair ladder builds, nothing else.
    """
    lost = 0.0
    lost += WEIGHTS["subject_cut"] * min(1.0, metrics.subject_cut_by_window / SUBJECT_CUT_MAX)
    lost += WEIGHTS["text_over_subject"] * min(
        1.0, metrics.text_over_subject / TEXT_OVER_SUBJECT_MAX
    )
    lost += WEIGHTS["picture_cropped"] * min(1.0, max(0.0, 1.0 - metrics.photo_visible_share))
    shrink = min([v for v in metrics.shrink.values()] or [1.0])
    lost += WEIGHTS["type_shrunk"] * min(1.0, max(0.0, 1.0 - shrink) / 0.4)
    if metrics.scrim_saturated:
        lost += WEIGHTS["scrim_saturated"]
    for cls, ratio in metrics.contrast.items():
        bar = legibility.bar_for(cls)
        if ratio < bar:
            lost += WEIGHTS["contrast"] * min(1.0, (bar - float(ratio)) / bar)
    return int(max(0, min(100, round(100 - lost))))


# --------------------------------------------------------------------------- #
# the free repair ladder
# --------------------------------------------------------------------------- #
# Layouts that set no word on the picture: the words go beside or below it, so
# a subject the words were sitting on is clear of them. pipeline's order.
WORDS_OFF_PICTURE = ("split_card", "top_band", "frame_card")
# Layouts that show the picture full-bleed: the window IS the canvas, so a
# subject a panel was cutting is whole again.
WORDS_ON_PICTURE = ("centered_overlay", "lower_third", "poster_stack")
# How many other templates one slide may be re-composed into. Each is about a
# second of Chromium and no vendor call at all; two is enough to get from any
# layout to one of the other kind, and keeps a six-slide carousel honest.
FREE_VARIANTS = 2


def order_for(template: str, assessment: Assessment) -> list[str]:
    """Which templates to try next, best first, for what is wrong with this one.

    The fault names the cure. Words sitting on the subject want a layout that
    sets words beside the picture; a subject the window cut off wants the
    layout that shows the whole frame. Anything else just wants the other kind
    of layout, because staying in the same family rarely changes the answer.
    """
    faults = set(assessment.faults) | set(assessment.repairs)
    if faults & {"text_over_subject", "scrim_saturated"}:
        first, second = WORDS_OFF_PICTURE, WORDS_ON_PICTURE
    elif faults & {"subject_cut_by_window", "photo_mostly_cropped"}:
        first, second = WORDS_ON_PICTURE, WORDS_OFF_PICTURE
    else:
        on_picture = template in WORDS_ON_PICTURE
        first, second = (
            (WORDS_OFF_PICTURE, WORDS_ON_PICTURE) if on_picture else
            (WORDS_ON_PICTURE, WORDS_OFF_PICTURE)
        )  # fmt: skip
    return [name for name in (*first, *second) if name != template]


@dataclass(slots=True)
class Variant:
    """One finished version of a slide, and what the final check made of it."""

    template: str
    png: bytes
    jpeg: bytes
    report: dict
    assessment: Assessment

    @property
    def score(self) -> int:
        return self.assessment.score

    @property
    def ok(self) -> bool:
        return self.assessment.ok


async def best_free_variant(first: Variant, render, *, limit: int = FREE_VARIANTS) -> Variant:
    """The best version of this slide that costs nothing to make.

    `render(template)` composes the slide again in another layout and comes
    back with a Variant, or None when that layout cannot take this slide --
    the copy does not fit it, or a picture generated for one window does not
    fill another's (only the three full-bleed layouts share a window, so a
    generated picture can move between those three for free and nowhere else).
    Every call is Chromium and PIL. None of them is a vendor call, which is
    the whole point: the owner pays for a second picture only when no
    arrangement of the one they already have is good enough.

    Stops at the first clean variant that beats what it came in with; a slide
    that is already clean is returned untouched.
    """
    if not first.assessment.worth_a_variant:
        return first
    best = first
    tried: list[str] = []
    for template in order_for(first.template, first.assessment)[:limit]:
        tried.append(template)
        variant = await render(template)
        if variant is None:
            continue
        if (variant.ok, variant.score) > (best.ok, best.score):
            best = variant
        if best.ok and best is not first:
            break
    log.info(
        "composite_repair",
        was=first.template,
        now=best.template,
        tried=tried,
        was_score=first.score,
        now_score=best.score,
        faults=best.assessment.faults,
        repaired=first.template != best.template,
    )
    return best
