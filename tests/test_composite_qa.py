"""The final check on the finished frame: what the client actually receives.

Everything upstream checks a part -- the copy on a blank page, the picture as
the vendor returned it. These pin the check that looks at the composite: the
deterministic metrics, the faults they raise, the free repair ladder that
fixes what a template change can fix without spending a rupee, and the vendor
gate that looks at the exported JPEG.
"""

from __future__ import annotations

import io
import json
import types
import uuid

import pytest
from PIL import Image

from app.creative import bggate, compose, compositeqa, finalgate, legibility, pipeline
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief
from tests.test_pipeline_flow import build_world


async def _noop(*a, **k):
    """Retry backoff, skipped: these tests pin the refusal, not the waiting."""


# The real inspector, captured before any fixture fakes it, so one test can put
# it back and prove what a wholly mocked run does with no inspector to call.
_REAL_INSPECT = finalgate.inspect


CANVAS = [1080, 1350]
FULL = [0, 0, 1080, 1350]
# split_card's measured window at ordinary copy length (probe, 4:5 post).
PANEL = [0, 0, 1080, 842]
# ...and where its words sit: on the panel BELOW that window, never on it.
PANEL_BOXES = {
    "headline": {"l": 90, "t": 900, "r": 990, "b": 1050},
    "cta": {"l": 540, "t": 1160, "r": 990, "b": 1270},
}


def _box(left, top, right, bottom) -> dict:
    return {"l": left, "t": top, "r": right, "b": bottom}


def _report(
    *,
    window=None,
    boxes=None,
    sizes=None,
    contrast=None,
    plates=(),
    boost=1.0,
    canvas=None,
) -> dict:
    """A compose report, hand-built. Every field is one the compositor really
    returns; the values are the ones the case under test needs."""
    return {
        "canvas": list(canvas or CANVAS),
        "photo_window": list(window if window is not None else FULL),
        "violations": [],
        "sizes": sizes
        or {
            "headline": {"px": 96, "design": 96, "steps": 0},
            "subhead": {"px": 36, "design": 36, "steps": 0},
            "cta": {"px": 34, "design": 34, "steps": 0},
        },
        "boxes": boxes or {"headline": _box(90, 500, 990, 700)},
        "inks": [],
        "legibility": {
            "contrast": contrast or {"headline": 8.0, "subhead": 7.0, "cta": 5.0},
            "plates": list(plates),
            "mark_plate": None,
            "boost": boost,
        },
    }


# --------------------------------------------------------------------------- #
# the metrics
# --------------------------------------------------------------------------- #
def test_type_at_its_design_size_is_not_shrunk_and_not_at_the_floor():
    a = compositeqa.assess(_report(), template="centered_overlay")
    assert a.metrics.shrink == {"headline": 1.0, "subhead": 1.0, "cta": 1.0}
    assert a.metrics.at_floor == [] and a.ok and a.repairs == []
    assert a.score == 100


def test_the_headline_pinned_to_its_floor_is_reported_as_at_the_floor():
    floor = round(1080 * compose.HEADLINE_MIN)
    a = compositeqa.assess(
        _report(sizes={"headline": {"px": floor, "design": 120, "steps": 7}}),
        template="centered_overlay",
    )
    assert a.metrics.at_floor == ["headline"]
    assert a.metrics.shrink["headline"] == floor / 120
    # ...and it asks for a better template without refusing this one.
    assert a.repairs == ["headline_at_floor"] and a.ok and a.worth_a_variant


def test_a_headline_whose_design_size_IS_the_floor_is_not_called_shrunk():
    """A short headline on a small canvas can be set at the floor by design.
    Calling that 'shrunk to the floor' would send the repair ladder after a
    slide with nothing wrong with it."""
    floor = round(1080 * compose.HEADLINE_MIN)
    a = compositeqa.assess(
        _report(sizes={"headline": {"px": floor, "design": floor, "steps": 0}}),
        template="centered_overlay",
    )
    assert a.metrics.at_floor == [] and a.repairs == [] and a.ok


def test_a_window_smaller_than_the_picture_reports_how_much_it_shows():
    """split_card shows the top 62% of the canvas. A 4:5 photograph cover-fitted
    into that window keeps 62% of itself -- the rest is cropped away, which is
    the number nothing in the product could state before."""
    a = compositeqa.assess(
        _report(window=PANEL),
        template="split_card",
        source_size=(1600, 2000),
    )
    assert 0.60 < a.metrics.photo_visible_share < 0.64
    assert a.metrics.fit_mode == "crop"


def test_a_picture_made_for_its_window_loses_nothing():
    a = compositeqa.assess(_report(window=PANEL), template="split_card", source_size=(1080, 842))
    assert a.metrics.photo_visible_share == 1.0 and a.metrics.fit_mode == "whole"


def test_a_letterboxed_photograph_is_shown_whole():
    """A subject too big for any crop is letterboxed rather than cut. Nothing
    of the picture is lost, so nothing is reported as lost."""
    a = compositeqa.assess(
        _report(window=PANEL),
        template="split_card",
        source_size=(1600, 2000),
        subject=(100, 100, 1500, 1900),
        focus=(100, 100, 1500, 1900),
    )
    assert a.metrics.fit_mode == "letterbox"
    assert a.metrics.photo_visible_share == 1.0
    assert a.metrics.subject_cut_by_window == 0.0


def test_words_over_the_picture_are_measured_as_a_share_of_it():
    a = compositeqa.assess(
        _report(boxes={"headline": _box(0, 0, 540, 675)}),
        template="centered_overlay",
    )
    assert abs(a.metrics.text_over_photo_share - 0.25) < 0.01


# --------------------------------------------------------------------------- #
# the faults
# --------------------------------------------------------------------------- #
def test_a_subject_beheaded_by_the_panel_is_a_fault():
    """The hole this closes, in one case. A jar low in a 4:5 photograph, shown
    through split_card's window with a blind centre crop, loses 35% of itself
    to the panel -- and the layout gate, the background gate and the exporter
    all passed it. Measured on a real render before it was pinned here."""
    a = compositeqa.assess(
        _report(window=PANEL, boxes=PANEL_BOXES),
        template="split_card",
        source_size=(1600, 2000),
        subject=(480, 1100, 1120, 1900),
    )
    assert a.faults == ["subject_cut_by_window"] and not a.ok
    assert a.metrics.subject_cut_by_window > 0.3
    assert "subject" in a.notes


def test_the_same_photograph_cropped_around_its_subject_passes():
    """The free fix for the case above, and the proof the fault is not a
    blanket refusal of panel layouts: same picture, same window, same subject,
    cropped around it instead of blindly."""
    subject = (480, 1100, 1120, 1900)
    a = compositeqa.assess(
        _report(window=PANEL, boxes=PANEL_BOXES),
        template="split_card",
        source_size=(1600, 2000),
        subject=subject,
        focus=subject,
    )
    assert a.ok and a.metrics.subject_cut_by_window == 0.0


def test_a_subject_the_window_only_grazes_is_not_a_fault():
    """A crop that just fits leaves rounding, not a beheading. The bar has to
    clear the arithmetic or every panel layout refuses itself."""
    a = compositeqa.assess(
        _report(window=PANEL, boxes=PANEL_BOXES),
        template="split_card",
        source_size=(1600, 2000),
        subject=(480, 400, 1120, 1270),
    )
    assert a.metrics.subject_cut_by_window < compositeqa.SUBJECT_CUT_MAX
    assert a.ok


def test_words_across_the_subject_are_a_fault():
    """lower_third sets the headline and CTA over the lower third of the
    photograph. A jar that sits there is covered by them -- measured at 39% on
    a real render, where the scrim had also darkened it into the ground."""
    a = compositeqa.assess(
        _report(
            boxes={
                "headline": _box(90, 810, 700, 1030),
                "cta": _box(90, 1150, 545, 1260),
            }
        ),
        template="lower_third",
        source_size=(1080, 1350),
        subject=(325, 700, 760, 1290),
    )
    assert "text_over_subject" in a.faults and not a.ok
    assert a.metrics.text_over_subject > compositeqa.TEXT_OVER_SUBJECT_MAX


def test_a_word_grazing_the_corner_of_the_subject_is_not_a_fault():
    a = compositeqa.assess(
        _report(boxes={"headline": _box(90, 1240, 700, 1300)}),
        template="lower_third",
        source_size=(1080, 1350),
        subject=(325, 700, 760, 1290),
    )
    assert a.metrics.text_over_subject < compositeqa.TEXT_OVER_SUBJECT_MAX and a.ok


def test_only_the_visible_part_of_a_subject_counts_as_covered():
    """A subject half cut off by the window must not also be reported as
    half covered by the words that sit over what is left: the ladder would
    chase the wrong problem, and moving the words would not fix the crop."""
    a = compositeqa.assess(
        _report(window=PANEL, boxes={"headline": _box(0, 0, 1080, 200)}),
        template="split_card",
        source_size=(1080, 1350),
        subject=(0, 0, 1080, 1350),
    )
    assert a.metrics.text_over_subject < 0.30, "measured against the visible part"


def test_a_measured_contrast_under_the_bar_is_a_fault():
    a = compositeqa.assess(
        _report(contrast={"headline": 2.78, "subhead": 7.0}),
        template="centered_overlay",
    )
    assert a.faults == ["contrast_below_bar"] and "2.78:1" in a.notes


def test_the_logo_is_held_to_its_own_bar_not_the_text_bar():
    a = compositeqa.assess(
        _report(contrast={"headline": 8.0, "logo": 3.4}), template="centered_overlay"
    )
    assert a.ok, "3.4:1 clears the 3:1 a graphical object is held to"
    assert compositeqa.assess(
        _report(contrast={"logo": 2.2}), template="centered_overlay"
    ).faults == ["contrast_below_bar"]


def test_the_contrast_bar_carries_the_compositors_measurement_slack():
    """The compositor accepts 4.48 read off 8-bit pixels as 4.5. If this check
    used the bare bar it would refuse frames the compositor had just passed."""
    bar = legibility.TEXT_CONTRAST - legibility.MEASURE_SLACK
    assert compositeqa.assess(_report(contrast={"headline": bar}), template="centered_overlay").ok


def test_a_picture_cropped_down_to_a_detail_is_a_fault():
    a = compositeqa.assess(
        _report(window=[0, 0, 1080, 300]),
        template="top_band",
        source_size=(1600, 2000),
    )
    assert "photo_mostly_cropped" in a.faults
    assert a.metrics.photo_visible_share < compositeqa.PHOTO_VISIBLE_MIN


def test_an_export_that_is_not_the_post_size_is_a_fault():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1080, 1080), (12, 12, 12)).save(buf, "JPEG")
    a = compositeqa.assess(_report(), template="centered_overlay", jpeg=buf.getvalue())
    assert a.faults == ["export_size_wrong"] and "1080x1080" in a.notes


# --------------------------------------------------------------------------- #
# the repairs: worth another template, never a refusal on their own
# --------------------------------------------------------------------------- #
def test_a_gradient_standing_at_its_cap_is_reported_but_never_re_renders_the_slide():
    """It ships, and on its own it does not even buy a second look at itself.
    A scrim at its cap is on nearly every ordinary frame that sets words over a
    photograph -- an ordinary centred product photo reports it on poster_stack
    and on lower_third with no fault at all -- and a variant is a full render,
    3.2-5.6s. Two of those on a deliverable card, for a gradient that is ugly
    rather than wrong, is latency the owner pays for nothing."""
    a = compositeqa.assess(_report(boost=legibility.MAX_BOOST), template="centered_overlay")
    assert a.metrics.scrim_saturated and a.repairs == ["scrim_saturated"]
    assert a.ok and not a.worth_a_variant
    # ...but it still costs the frame points, so a variant built for some other
    # reason can beat it on that, and the inspector can still name it.
    assert a.score < 100
    assert compositeqa.order_for("centered_overlay", {"scrim_saturated"})[0] == "split_card"


def test_a_plate_at_its_cap_counts_as_saturated_too():
    a = compositeqa.assess(
        _report(plates=[{"anchor": "headline", "alpha": legibility.LOCAL_MAX}]),
        template="lower_third",
    )
    assert a.metrics.scrim_saturated


def test_a_saturated_scrim_on_a_layout_that_has_no_scrim_is_not_reported():
    """split_card sets its words on a panel. A plate there says nothing about
    the photograph, and the ladder has nothing to move the words onto."""
    a = compositeqa.assess(_report(window=PANEL, boost=legibility.MAX_BOOST), template="split_card")
    assert a.repairs == []


def test_a_repair_alone_never_refuses_the_frame():
    """The owner's demand cuts both ways: a check that refuses a valid job is
    as much a bug as one that passes a broken frame."""
    floor = round(1080 * compose.HEADLINE_MIN)
    a = compositeqa.assess(
        _report(
            sizes={"headline": {"px": floor, "design": 120, "steps": 9}},
            boost=legibility.MAX_BOOST,
        ),
        template="centered_overlay",
    )
    assert set(a.repairs) == {"headline_at_floor", "scrim_saturated"}
    assert a.ok is True and a.faults == []


# --------------------------------------------------------------------------- #
# the score: for ranking variants, never a pass mark
# --------------------------------------------------------------------------- #
def test_a_clean_frame_scores_full_marks():
    assert compositeqa.assess(_report(), template="centered_overlay").score == 100


def test_the_worse_frame_scores_lower():
    subject = (480, 1100, 1120, 1900)
    blind = compositeqa.assess(
        _report(window=PANEL, boxes=PANEL_BOXES), template="split_card",
        source_size=(1600, 2000), subject=subject,
    )  # fmt: skip
    around = compositeqa.assess(
        _report(window=PANEL, boxes=PANEL_BOXES), template="split_card",
        source_size=(1600, 2000), subject=subject, focus=subject,
    )  # fmt: skip
    assert blind.score < around.score
    assert not blind.ok and around.ok


def test_the_score_is_never_a_pass_mark():
    """A high score with a fault does not ship; a lower score with none does.
    Ranking and refusing are different questions and stay separate."""
    high = compositeqa.assess(_report(contrast={"headline": 4.4}), template="centered_overlay")
    low = compositeqa.assess(
        _report(
            window=PANEL,
            sizes={"headline": {"px": 60, "design": 120, "steps": 6}},
            boost=legibility.MAX_BOOST,
        ),
        template="split_card",
        source_size=(1600, 2000),
    )
    assert high.score > low.score and not high.ok and low.ok


def test_the_vocabulary_is_codes_not_severities():
    """tests/test_background_gate.py bans severity words from the pipeline, and
    is right to: a quality knob named in the pipeline is one somebody turns
    down under load. The codes carry the meaning instead."""
    every = set(compositeqa.FAULTS) | set(compositeqa.REPAIRS)
    assert not every & {"low", "medium", "high", "minor", "major", "severity"}
    assert all(code.replace("_", "").isalpha() for code in every)


# --------------------------------------------------------------------------- #
# the same thing, on a real render: a frame that ships today
# --------------------------------------------------------------------------- #
@pytest.fixture
async def chromium():
    try:
        await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium: {exc}")
    yield
    await compose.shutdown()


def _brand():
    return types.SimpleNamespace(
        name="Kadamba Naturals",
        category="food",
        palette={"primary": "#123B2E", "accent": "#E4572E"},
        fonts={"heading": "Poppins", "body": "Inter"},
        logo_analysis={},
        logo_url=None,
        logo_src=None,
        never_say=[],
        template_prefs={"signature": "none"},
    )


def _brief(template: str) -> CreativeBrief:
    payload = json.loads(json.dumps(EXAMPLE))
    payload.update(template_id=template)
    return CreativeBrief.model_validate(payload)


SUBJECT = (480, 1100, 1120, 1900)


def _photo(size=(1600, 2000)) -> bytes:
    """A picture with an unmistakable subject: a dark jar low in the frame."""
    from PIL import Image, ImageDraw

    width, height = size
    im = Image.new("RGB", size, (232, 224, 208))
    d = ImageDraw.Draw(im)
    for y in range(0, height, 3):
        t = y / height
        d.line([(0, y), (width, y)], fill=(int(232 - 40 * t), int(224 - 30 * t), 208))
    d.rounded_rectangle(SUBJECT, radius=int(width * 0.05), fill=(38, 54, 44))
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


async def _composite(template: str, *, focus=None):
    brief = _brief(template)
    slide = brief.units()[0]
    size = brief.pixel_size()
    png, report = await compose.compose_with_report(
        brief, slide, _brand(), _photo(), "image/png", generated=False, focus=focus
    )
    final = compose.export_jpeg(png, size)
    return compositeqa.assess(
        report,
        template=template,
        jpeg=final,
        source_size=(1600, 2000),
        subject=SUBJECT,
        focus=focus,
    )


async def test_a_real_render_with_the_subject_cut_by_the_panel_is_caught(chromium):
    """Not a hand-built report: Chromium renders it, export_jpeg passes it, and
    every guarantee the product had before this said yes. The jar's bottom
    third is behind split_card's panel and the client would have received it."""
    blind = await _composite("split_card")
    assert "subject_cut_by_window" in blind.faults
    assert blind.metrics.subject_cut_by_window > 0.3


async def test_the_same_real_render_cropped_around_the_subject_passes(chromium):
    """...and the free fix costs nothing: the same picture, the same layout,
    cropped around the jar instead of through it."""
    around = await _composite("split_card", focus=SUBJECT)
    assert around.ok and around.metrics.subject_cut_by_window == 0.0
    assert around.score > 85


async def test_a_real_render_with_the_words_across_the_subject_is_caught(chromium):
    """lower_third sets the headline and the CTA over the lower third of the
    photograph, which is where this jar stands. Measured, not guessed."""
    over = await _composite("lower_third", focus=SUBJECT)
    assert "text_over_subject" in over.faults
    assert over.metrics.text_over_subject > 0.3


# Where an ordinary photograph's subject sits when the layout is the right one
# for it, as fractions of the frame's height. The three type-over-photo layouts
# each leave a different band clear (shotplan.TYPE_ZONES says which); the three
# panel layouts set their words beside or below the picture, so any subject the
# crop is made around is clear of them. No single placement suits all six --
# that is exactly why the repair ladder exists, and why _choose_photo_layouts
# moves a photo slide off a layout whose words would land on its subject.
CLEAR_BAND = {
    "centered_overlay": (0.03, 0.27),
    "lower_third": (0.05, 0.50),
    "poster_stack": (0.55, 0.96),
    "split_card": (0.30, 0.70),
    "top_band": (0.30, 0.70),
    "frame_card": (0.30, 0.70),
}


@pytest.mark.parametrize("kind", ["single", "story"])
@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_ordinary_copy_on_an_ordinary_photograph_passes(chromium, template, kind):
    """The other half of the guarantee: a check that refuses valid work is a
    bug. An ordinary jar, an ordinary headline, the layout that suits it --
    every one of the six, post and story, passes with no fault at all."""
    payload = json.loads(json.dumps(EXAMPLE))
    payload.update(template_id=template, format={"type": kind})
    brief = CreativeBrief.model_validate(payload)
    slide = brief.units()[0]
    top, bottom = CLEAR_BAND[template]
    subject = (480, round(2000 * top), 1120, round(2000 * bottom))
    png, report = await compose.compose_with_report(
        brief, slide, _brand(), _photo(), "image/png", generated=False, focus=subject
    )
    a = compositeqa.assess(
        report,
        template=template,
        jpeg=compose.export_jpeg(png, brief.pixel_size()),
        source_size=(1600, 2000),
        subject=subject,
        focus=subject,
    )
    assert a.ok, f"{template}/{kind}: {a.faults} {a.notes}"


# --------------------------------------------------------------------------- #
# the vision gate on the exported JPEG: strict, or there is no verdict
# --------------------------------------------------------------------------- #
def _answer(score=88, notes="", **flags) -> str:
    """The inspector's reply, in the shape finalgate demands. Copied from
    tests/test_background_gate.py's _answer so the two gates are pinned the
    same way -- they share a client, retries and a refusal to guess."""
    body = {k: False for k in finalgate.KEYS} | flags
    body |= {"score": score, "notes": notes}
    return "Here you go:\n```json\n" + json.dumps(body) + "\n```"


def test_a_clean_composite_passes_and_carries_its_score():
    verdict = bggate.parse(_answer(score=91), finalgate.KEYS, scored=True)
    assert verdict.ok and verdict.score == 91


def test_a_flag_rejects_the_composite():
    verdict = bggate.parse(_answer(text_cut_off=True, artefacts=True), finalgate.KEYS, scored=True)
    assert verdict.reasons == ["text_cut_off", "artefacts"] and not verdict.ok


def test_a_non_boolean_flag_is_not_a_pass():
    with pytest.raises(bggate.InspectionUnavailable, match="missing"):
        bggate.parse(_answer(logo_problem="maybe"), finalgate.KEYS, scored=True)


def test_a_missing_key_is_not_a_pass():
    body = {k: False for k in finalgate.KEYS if k != "looks_unfinished"}
    body |= {"score": 90, "notes": ""}
    with pytest.raises(bggate.InspectionUnavailable, match="looks_unfinished"):
        bggate.parse(json.dumps(body), finalgate.KEYS, scored=True)


@pytest.mark.parametrize("score", [-1, 101, "high", None, 88.5, True])
def test_a_score_that_is_not_an_integer_0_to_100_is_not_a_pass(score):
    """A judge that answers "score": true would otherwise pass as 1 -- bool is
    an int in Python, and that is exactly the kind of shrug a fail-closed gate
    exists to refuse."""
    body = {k: False for k in finalgate.KEYS} | {"score": score, "notes": ""}
    with pytest.raises(bggate.InspectionUnavailable, match="score"):
        bggate.parse(json.dumps(body), finalgate.KEYS, scored=True)


def test_a_missing_score_is_not_a_pass():
    body = {k: False for k in finalgate.KEYS} | {"notes": ""}
    with pytest.raises(bggate.InspectionUnavailable, match="score"):
        bggate.parse(json.dumps(body), finalgate.KEYS, scored=True)


def test_the_background_gates_own_rubric_still_needs_no_score():
    """The refactor is meant to be invisible to the gate that was already
    there: same call, same defaults, same verdict."""
    body = {k: False for k in bggate.REASONS} | {"watermark": True, "notes": "a logo"}
    verdict = bggate.parse(json.dumps(body))
    assert verdict.reasons == ["watermark"] and verdict.score is None


def test_the_composite_rubric_is_the_one_that_was_asked_for():
    assert set(finalgate.KEYS) == {
        "text_cut_off",
        "text_hard_to_read",
        "text_covers_subject",
        "subject_cut_off",
        "logo_problem",
        "stray_text_in_photo",
        "artefacts",
        "looks_unfinished",
    }
    for key in finalgate.KEYS:
        assert key in finalgate.prompt_for()
    assert finalgate.PICTURE_REASONS < set(finalgate.KEYS)


def test_the_supplied_copy_reaches_the_inspector():
    """Without it the inspector cannot tell the headline it is meant to see
    from lettering the image model invented, which is the one fault no
    measurement can catch."""
    prompt = finalgate.prompt_for("Weekend Sale", "ghar jaisa shudh", "Order now", "Kadamba")
    for word in ("Weekend Sale", "ghar jaisa shudh", "Order now", "Kadamba"):
        assert word in prompt
    assert "not the supplied copy" in prompt


def test_a_brand_with_no_logo_is_never_told_its_logo_is_absent():
    """The client's card that started this: no logo image, so the mark is the
    brand NAME set as letterspaced type -- legible, clean, exactly what the
    compositor is designed to do. The inspector was shown a rubric that says
    'missing where one is expected', saw no logo, and said so on every layout
    and every picture until the credit was refunded."""
    none = finalgate.prompt_for("Weekend Sale", brand="Vajas Sunny", has_logo=False)
    assert "missing" not in none, "nothing in the prompt invites the answer"
    assert "NO logo image" in none and "set as type" in none
    # ...and it is still judged, as a wordmark, on the things that ARE faults.
    for word in ("clipped", "illegible", "clashes"):
        assert word in none


def test_a_brand_that_has_a_logo_is_asked_about_it_exactly_as_before():
    have = finalgate.prompt_for("Weekend Sale", brand="Kadamba", has_logo=True)
    assert "missing where one is expected" in have
    assert finalgate.prompt_for("Weekend Sale", brand="Kadamba") == have, "the default"


def test_the_mark_the_card_carries_is_the_mark_the_inspector_is_told_about():
    """One rule, one owner. The compositor decides what the mark IS; a second
    reading of the brand in the gate is how the two drift apart."""
    brand = types.SimpleNamespace(logo_src=None, logo_url=None)
    assert compose.logo_image(brand) is None
    brand.logo_url = "https://cdn.test/mark.png"
    assert compose.logo_image(brand) == "https://cdn.test/mark.png"
    brand.logo_src = "data:image/png;base64,AAA"
    assert compose.logo_image(brand) == "data:image/png;base64,AAA", "the inlined one wins"


def test_a_regeneration_prompt_keeps_the_picture_wording_and_adds_the_layout():
    """One vocabulary: the picture faults reuse the background gate's own
    sentences, and the layout sentence is the one the first prompt used."""
    out = finalgate.corrected(
        "a brass diya on marble",
        ["artefacts", "text_covers_subject"],
        template="lower_third",
    )
    assert bggate.CORRECTIONS["visible_artefacts"] in out
    assert finalgate.CORRECTIONS["text_covers_subject"] in out
    assert out != "a brass diya on marble" and len(out) > 100


def test_a_correction_is_added_once():
    once = finalgate.corrected("a diya", ["artefacts", "artefacts"])
    assert once.count(finalgate.CORRECTIONS["artefacts"]) == 1
    assert finalgate.corrected(once, ["artefacts"]) == once
    assert finalgate.corrected("a diya", []) == "a diya"


def test_the_gate_fails_closed_without_a_vision_model(monkeypatch):
    monkeypatch.setattr(bggate.settings, "anthropic_api_key", "")
    assert finalgate.available() is False


async def test_an_inspector_that_cannot_be_reached_fails_closed(monkeypatch):
    monkeypatch.setattr(bggate.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(bggate.settings, "anthropic_model", "m")
    monkeypatch.setattr(bggate, "INSPECT_ATTEMPTS", 1)

    async def down(image, prompt=""):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(bggate, "_ask", down)
    monkeypatch.setattr(bggate.asyncio, "sleep", _noop)
    buf = io.BytesIO()
    Image.new("RGB", (1080, 1350), (20, 20, 20)).save(buf, "JPEG")
    with pytest.raises(bggate.InspectionUnavailable, match="failed"):
        await finalgate.inspect(buf.getvalue())


# --------------------------------------------------------------------------- #
# what the inspector charged is on the ledger
# --------------------------------------------------------------------------- #
class _Usage:
    def __init__(self, read, wrote):
        self.input_tokens, self.output_tokens = read, wrote


def test_an_inspection_is_priced_from_the_replys_own_usage():
    """bggate read resp.usage and dropped it, so a creative's cost_micros told
    the owner the picture cost $0.29 when the gates around it had spent more."""
    cost = bggate.cost_micros(_Usage(1200, 60))
    assert cost == round(1200 * 3000 / 1000 + 60 * 15000 / 1000) == 4500


def test_a_reply_with_no_usage_costs_nothing_rather_than_a_guess():
    assert bggate.cost_micros(None) == 0


async def test_the_spend_of_every_attempt_rides_home_on_the_verdict(monkeypatch):
    """A retry the model answered cost money even though the answer was
    unusable. Charging only for the attempt that worked loses the rest."""
    monkeypatch.setattr(bggate.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(bggate.settings, "anthropic_model", "m")
    monkeypatch.setattr(bggate.asyncio, "sleep", _noop)
    replies = iter([("not json at all", 1_000), (_answer(score=80), 4_500)])

    async def ask(image, prompt=""):
        return next(replies)

    monkeypatch.setattr(bggate, "_ask", ask)
    buf = io.BytesIO()
    Image.new("RGB", (1080, 1350), (20, 20, 20)).save(buf, "JPEG")
    verdict = await finalgate.inspect(buf.getvalue(), headline="Weekend Sale")
    assert verdict.ok and verdict.score == 80
    assert verdict.cost_micros == 5_500, "both calls are on the ledger"


async def test_an_inspector_that_never_answers_still_charged_for_trying(monkeypatch):
    """The same money, on the path where there is no verdict to carry it home.
    An outage used to throw the spend away with the answer, so a slide whose
    inspector fell over reported a cost of zero for three real calls."""
    monkeypatch.setattr(bggate.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(bggate.settings, "anthropic_model", "m")
    monkeypatch.setattr(bggate.asyncio, "sleep", _noop)

    async def ask(image, prompt=""):
        return "not json at all", 1_000

    monkeypatch.setattr(bggate, "_ask", ask)
    buf = io.BytesIO()
    Image.new("RGB", (1080, 1350), (20, 20, 20)).save(buf, "JPEG")
    with pytest.raises(bggate.InspectionUnavailable) as err:
        await finalgate.inspect(buf.getvalue(), headline="Weekend Sale")
    assert err.value.cost_micros == 3 * 1_000, "every attempt the model answered"


# --------------------------------------------------------------------------- #
# the free repair ladder: another layout costs a second of Chromium, not a rupee
# --------------------------------------------------------------------------- #
def _variant(template, score, ok=True) -> compositeqa.Variant:
    assessment = compositeqa.Assessment(faults=[] if ok else ["text_over_subject"], score=score)
    return compositeqa.Variant(template, b"png", b"jpeg", {}, assessment)


def _ladder(available: dict[str, compositeqa.Variant]):
    """A renderer that can make some templates and not others. Records what it
    was asked for, so 'how many renders did that cost' is checkable."""
    asked: list[str] = []

    async def render(template):
        asked.append(template)
        return available.get(template)

    return render, asked


async def test_a_clean_slide_is_never_re_composed():
    """A rung is a full render, 3.2-5.6s. A slide with nothing wrong with it
    does not pay for one."""
    render, asked = _ladder({})
    first = _variant("centered_overlay", 100)
    out = await compositeqa.best_free_variant(first, render)
    assert out is first and asked == []


async def test_a_deliverable_slide_with_a_saturated_scrim_is_not_re_rendered():
    """The ordinary frame, and the reason this matters: an ordinary owner
    photograph on poster_stack or lower_third reports a scrim at its cap with
    no fault at all. That used to open the ladder, so every such slide paid ~9s
    of Chromium to be told the frame it already had was the best one."""
    clean_but_grey = compositeqa.Assessment(repairs=["scrim_saturated"], score=92)
    first = compositeqa.Variant("poster_stack", b"png", b"jpeg", {}, clean_but_grey)
    render, asked = _ladder({"split_card": _variant("split_card", 95)})
    out = await compositeqa.best_free_variant(first, render)
    assert out is first and asked == [], "nothing was re-rendered"


async def test_type_driven_to_its_floor_still_opens_the_ladder():
    """The repair that IS worth a render: the owner can read small type off the
    card, and another layout usually sets it bigger."""
    shrunk = compositeqa.Assessment(repairs=["headline_at_floor"], score=78)
    first = compositeqa.Variant("centered_overlay", b"png", b"jpeg", {}, shrunk)
    render, asked = _ladder({"split_card": _variant("split_card", 95)})
    out = await compositeqa.best_free_variant(first, render)
    assert out.template == "split_card" and asked


async def test_a_fault_a_layout_change_fixes_is_fixed_by_a_layout_change():
    render, asked = _ladder({"split_card": _variant("split_card", 92)})
    first = _variant("centered_overlay", 62, ok=False)
    out = await compositeqa.best_free_variant(first, render)
    assert out.template == "split_card" and out.ok
    assert asked == ["split_card"], "stopped at the first clean variant"


async def test_words_on_the_subject_are_taken_to_a_layout_that_sets_words_beside_it():
    """The fault names the cure: if the words are on the jar, move the words
    off the picture -- do not shuffle between layouts that all put them on it."""
    order = compositeqa.order_for("centered_overlay", {"text_over_subject"})
    assert order[:3] == list(compositeqa.WORDS_OFF_PICTURE)


async def test_a_subject_the_panel_cut_is_taken_to_a_layout_that_shows_the_whole_frame():
    order = compositeqa.order_for("split_card", {"subject_cut_by_window"})
    assert order[:3] == list(compositeqa.WORDS_ON_PICTURE)
    assert "split_card" not in order, "the layout that caused it is not a cure for it"


async def test_a_layout_that_cannot_take_this_slide_is_skipped_not_failed():
    """A picture generated for one window does not fill another's, and long
    copy does not fit every layout. That is one fewer free option, not a
    failure -- the ladder moves on."""
    render, asked = _ladder({"frame_card": _variant("frame_card", 88)})
    first = _variant("split_card", 40, ok=False)
    out = await compositeqa.best_free_variant(first, render, limit=3)
    assert asked[0] == "top_band" and out.template == "frame_card"
    assert asked == ["top_band", "frame_card"], "asked for the one it wanted first, then moved on"


async def test_when_nothing_renders_the_slide_it_came_in_as_is_kept():
    render, _ = _ladder({})
    first = _variant("centered_overlay", 62, ok=False)
    out = await compositeqa.best_free_variant(first, render)
    assert out is first, "a refusal is the caller's decision, not the ladder's"


async def test_the_best_scoring_variant_wins_when_none_is_clean():
    render, _ = _ladder({
        "split_card": _variant("split_card", 55, ok=False),
        "top_band": _variant("top_band", 71, ok=False),
    })  # fmt: skip
    first = _variant("centered_overlay", 40, ok=False)
    out = await compositeqa.best_free_variant(first, render, limit=3)
    assert out.template == "top_band" and out.score == 71


async def test_the_ladder_is_capped():
    """Two rungs, however many layouts exist -- and a rung is a variant that
    composed, which is what the cap is about: two more frames of Chromium."""
    render, asked = _ladder({t: _variant(t, 50, ok=False) for t in compose.TEMPLATES})
    await compositeqa.best_free_variant(_variant("centered_overlay", 10, ok=False), render, limit=2)
    assert len(asked) == 2


async def test_the_cap_counts_variants_that_composed_not_layouts_attempted():
    """The cap used to count layouts ATTEMPTED, and that made the free repair a
    no-op on the only lane that can spend. A picture generated for the
    full-bleed window fits none of the three panel layouts, and those are the
    three the ladder leads with when the words are on the subject -- so both
    rungs were spent on renders that must fail, and the slide went on to buy a
    second picture while two layouts that compose cleanly were never tried."""
    render, asked = _ladder({"lower_third": _variant("lower_third", 88)})
    out = await compositeqa.best_free_variant(
        _variant("centered_overlay", 62, ok=False), render, limit=2
    )
    assert asked[:3] == list(compositeqa.WORDS_OFF_PICTURE), "all three refused the picture"
    assert out.template == "lower_third" and out.ok, "and the fourth took it, for nothing"


async def test_a_slide_no_layout_can_take_tries_them_all_and_still_spends_nothing():
    """The other side of that: when nothing composes, the ladder is bounded by
    the layouts that exist -- five renders, no vendor call -- and hands back
    the frame it came in with for the caller to refuse."""
    render, asked = _ladder({})
    first = _variant("centered_overlay", 10, ok=False)
    out = await compositeqa.best_free_variant(first, render, limit=2)
    assert out is first
    assert asked == compositeqa.order_for("centered_overlay", {"text_over_subject"})


async def test_the_inspector_can_steer_the_ladder_even_when_the_measurements_are_happy():
    """The inspector saw something no measurement could -- a tofu glyph, a word
    baked into the tablecloth. Keeping the frame it just rejected because the
    numbers like it is exactly the shrug this package exists to remove."""
    render, asked = _ladder({"split_card": _variant("split_card", 70)})
    first = _variant("centered_overlay", 99)  # deterministically clean
    out = await compositeqa.best_free_variant(
        first,
        render,
        codes=finalgate.as_codes(["text_covers_subject"]),
        must_change=True,
    )
    assert out.template == "split_card" and asked


def test_the_inspectors_reasons_translate_into_the_ladders_vocabulary():
    assert finalgate.as_codes(["text_covers_subject"]) == {"text_over_subject"}
    assert finalgate.as_codes(["subject_cut_off"]) == {"subject_cut_by_window"}
    # ...and the two the picture is to blame for map to no layout at all.
    assert finalgate.as_codes(["artefacts", "stray_text_in_photo"]) == set()
    assert finalgate.picture_faults(["artefacts", "text_cut_off"]) == ["artefacts"]


def test_the_reasons_nothing_can_change_are_written_down_not_inferred():
    """Which verdicts are final decides whether the owner's money is spent, so
    it is a named set with a reason beside it, like PICTURE_REASONS."""
    assert finalgate.INVARIANT_REASONS == {"logo_problem"}
    assert finalgate.INVARIANT_REASONS < set(finalgate.KEYS)
    # A reason cannot be both: PICTURE_REASONS is the set worth buying a
    # picture for, INVARIANT_REASONS the set worth buying nothing for.
    assert not finalgate.INVARIANT_REASONS & finalgate.PICTURE_REASONS
    assert finalgate.settled(["text_covers_subject", "logo_problem"]) == ["logo_problem"]
    assert finalgate.settled(["artefacts", "text_cut_off"]) == []


# --------------------------------------------------------------------------- #
# wired into the pipeline: who pays for a repair, and who never does
# --------------------------------------------------------------------------- #
@pytest.fixture
async def world(monkeypatch):
    w = await build_world(monkeypatch)
    _quiet(monkeypatch)
    yield w
    await compose.shutdown()


def _quiet(monkeypatch):
    """The background gate passes everything; this file is about the gate that
    comes after it."""

    async def clean(image, **kw):
        return bggate.Verdict([], "")

    monkeypatch.setattr(bggate, "inspect", clean)


def _fault_once(monkeypatch, *, faults, on=1):
    """Make the deterministic check report `faults` for the first `on`
    assessments of the ORIGINAL layout, and nothing for any other layout -- so
    a template change is what clears it."""
    real = compositeqa.assess
    seen = {"n": 0}

    def fake(report, *, template, **kw):
        out = real(report, template=template, **kw)
        if template == "centered_overlay" and seen["n"] < on:
            seen["n"] += 1
            return compositeqa.Assessment(
                faults=list(faults), score=40, metrics=out.metrics, notes="scripted"
            )
        return out

    monkeypatch.setattr(pipeline.compositeqa, "assess", fake)
    return seen


def test_moving_a_single_post_to_another_layout_moves_the_slide_too():
    """units() builds a single post's slide detached from the brief and stamps
    the brief's template onto it, and template_for() reads the slide's first.
    Setting only the brief's left the compositor seeing the OLD layout: every
    rung of the repair ladder re-rendered the layout it was trying to leave,
    scored that frame and returned it under the new name."""
    brief = CreativeBrief.model_validate(EXAMPLE)
    slide = brief.units()[0]
    assert brief.template_for(slide) == "centered_overlay"
    pipeline._set_template(brief, slide, "split_card")
    assert brief.template_for(slide) == "split_card"


async def test_the_frame_that_ships_is_composed_in_the_layout_the_row_records(world, monkeypatch):
    """The row's template is what the owner is told the card is, and what a
    revision re-composes from. It used to be able to name a layout the picture
    was never set in, because the repair only relabelled the frame."""
    composed: list[str] = []
    real = compose.compose_with_report

    async def counted(brief, slide, *a, **k):
        composed.append(brief.template_for(slide))
        return await real(brief, slide, *a, **k)

    monkeypatch.setattr(compose, "compose_with_report", counted)
    _fault_once(monkeypatch, faults=["text_over_subject"], on=99)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))

    assert res["ok"]
    (row,) = world["rows"].values()
    assert row.status == "ready" and row.template != "centered_overlay"
    assert row.template in composed, "the delivered frame was set in the layout recorded"
    assert composed[0] == "centered_overlay", "...and it started somewhere else"


async def test_a_fault_a_template_change_fixes_costs_no_vendor_call(world, monkeypatch):
    """The whole point of repairing before retrying. The layout is free to
    change and the picture is not, so the picture is the last thing touched."""
    _fault_once(monkeypatch, faults=["text_over_subject"], on=99)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"] and res["slides_ok"] == 1
    assert len(world["provider"].requests) == 1, "one picture bought, not two"
    (row,) = world["rows"].values()
    assert row.status == "ready" and row.template != "centered_overlay"
    assert row.cost_micros == 288_300 + 4_500, "the picture and the look, nothing more"


async def test_the_look_at_the_picture_is_on_the_ledger_like_the_look_at_the_card(
    world, monkeypatch
):
    """bggate priced its inspections and _generate_checked dropped the number
    on the floor. The final check's looks were on the row and the background
    gate's were not, so the ledger disagreed with itself about which
    inspections the owner had paid for."""

    async def priced(image, **kw):
        return bggate.Verdict([], "", cost_micros=7_000)

    monkeypatch.setattr(bggate, "inspect", priced)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"]
    (row,) = world["rows"].values()
    assert row.cost_micros == 288_300 + 7_000 + 4_500, "the picture, the look at it, the look after"


async def test_a_picture_the_background_gate_threw_away_still_paid_for_its_look(world, monkeypatch):
    """The worst case for the old arithmetic: the rejected attempts are exactly
    the ones whose inspections were invisible, so a slide that cost the owner
    two pictures and two looks reported one look's worth less than it was."""
    looks = {"n": 0}

    async def picky(image, **kw):
        looks["n"] += 1
        return bggate.Verdict(["watermark"] if looks["n"] == 1 else [], "", cost_micros=7_000)

    monkeypatch.setattr(bggate, "inspect", picky)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"] and len(world["provider"].requests) == 2
    (row,) = world["rows"].values()
    assert row.cost_micros == 2 * 288_300 + 2 * 7_000 + 4_500


async def test_a_picture_fault_on_the_generated_lane_costs_exactly_one_more(world, monkeypatch):
    """No layout can cure a word baked into the tablecloth. That -- and only
    that -- is worth buying a second picture for, and only ever one."""
    world["final_verdicts"].extend([["artefacts"], []])
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"] and res["slides_ok"] == 1
    assert len(world["provider"].requests) == 2, "exactly one more picture"
    (row,) = world["rows"].values()
    assert row.status == "ready"
    assert row.cost_micros == 2 * 288_300 + 2 * 4_500, "both pictures and both looks"


async def test_a_picture_fault_does_not_waste_renders_on_other_layouts(world, monkeypatch):
    """No arrangement of a photograph with a melted hand in it is the right
    arrangement. Trying three of them costs three seconds and risks shipping
    the one the inspector happens to pass."""
    rendered = []
    real = compose.compose_with_report

    async def counted(*a, **k):
        rendered.append(1)
        return await real(*a, **k)

    monkeypatch.setattr(compose, "compose_with_report", counted)
    world["final_verdicts"].extend([["artefacts"], []])
    await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert len(rendered) == 2, "the first frame and the one over the bought picture"


async def test_the_corrected_prompt_says_what_was_wrong_with_the_last_one(world, monkeypatch):
    world["final_verdicts"].extend([["artefacts"], []])
    await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    first, second = world["provider"].requests
    assert finalgate.CORRECTIONS["artefacts"] in second.prompt
    assert finalgate.CORRECTIONS["artefacts"] not in first.prompt


def _count_renders(monkeypatch) -> list[int]:
    rendered: list[int] = []
    real = compose.compose_with_report

    async def counted(*a, **k):
        rendered.append(1)
        return await real(*a, **k)

    monkeypatch.setattr(compose, "compose_with_report", counted)
    return rendered


async def test_a_verdict_about_the_brands_mark_is_paid_for_once_and_never_again(world, monkeypatch):
    """A real client's card, trace bf6cd37906824f65. The brand has no logo
    image, so the mark is their name set as type. The inspector called that
    logo_problem -- on the layout it came in as, on all four the repair ladder
    then swept, and again on both pictures the pipeline bought to answer it.
    Six refusals of one unchanging thing: 62 cents, five and a half minutes, a
    refunded credit and nothing delivered. The card is still refused; it is
    refused once."""
    rendered = _count_renders(monkeypatch)
    world["final_verdicts"].append(["text_covers_subject", "logo_problem"])
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))

    assert res["ok"] is False and res["reason"] == "composite_quality"
    assert len(world["provider"].requests) == 1, "no picture bought to argue with the mark"
    assert len(rendered) == 1, "and no layout re-rendered to argue with it either"
    assert len(world["inspected"]) == 1, "one look, one refusal"
    (row,) = world["rows"].values()
    assert row.status == "failed"
    assert row.cost_micros == 288_300 + 4_500, "the one picture and the one look"


async def test_a_verdict_a_layout_can_still_cure_keeps_its_whole_free_ladder(world, monkeypatch):
    """The other half, and the one that would be quietly lost by being too
    clever: a reason that a template change really does cure still gets every
    free rung it had before."""
    rendered = _count_renders(monkeypatch)
    world["final_verdicts"].extend([["text_covers_subject"], []])
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))

    assert res["ok"] and res["slides_ok"] == 1
    assert len(rendered) > 1, "the ladder set the same picture another way"
    assert len(world["provider"].requests) == 1, "and cured it without buying anything"
    (row,) = world["rows"].values()
    assert row.status == "ready" and row.template != "centered_overlay"


async def test_the_gate_is_told_there_is_no_logo_when_the_brand_has_none(world, monkeypatch):
    """The fact has to travel: compose picks the mark, and the gate several
    hundred lines away has to know which of the two it is looking at."""
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"]
    ((_image, copy),) = world["inspected"]
    assert copy["has_logo"] is False, "this brand's snapshot carries no logo at all"
    assert copy["brand"] == "Kadamba Naturals", "and the name that is set in its place"


async def _keep_first(first, render, **kw):
    """No layout helps: the ladder comes back with what it was given."""
    return first


async def _final_check_on(world, monkeypatch, *, lane, buy):
    """Drive _final_check directly on a real render, with the ladder blocked so
    the refusal path is what is under test."""
    monkeypatch.setattr(pipeline.compositeqa, "best_free_variant", _keep_first)
    _fault_once(monkeypatch, faults=["text_over_subject"], on=99)
    brief = CreativeBrief.model_validate(EXAMPLE)
    slide = brief.units()[0]
    brand = pipeline._snapshot(None, world["brand"])
    png, report = await compose.compose_with_report(
        brief, slide, brand, _photo(), "image/png", generated=False
    )
    final = compose.export_jpeg(png, brief.pixel_size())
    return await pipeline._final_check(
        world["ctx"], brief, slide, brand, uuid.uuid4(),
        image=_photo(), mime="image/png", png=png, final=final, report=report,
        generated=False, focus=None, subject=None, stage="slide1", lane=lane, buy=buy,
    )  # fmt: skip


async def test_a_lane_that_may_not_spend_refuses_with_something_the_agent_can_act_on(
    world, monkeypatch
):
    """The owner sent this photograph. We do not quietly replace it with a
    bought one because our own check did not like the card -- we say so, and
    we say what would fix it."""
    with pytest.raises(pipeline.CompositeRejected) as err:
        await _final_check_on(world, monkeypatch, lane="brand_asset", buy=None)
    assert err.value.faults == ["text_over_subject"]
    assert "another photo" in err.value.hint and "DOCUMENT" in err.value.hint
    assert "different layout" in err.value.hint
    assert len(world["provider"].requests) == 0, "a free lane stays free, even refusing"
    assert err.value.cost_micros == 4_500, "the looks it took are still on the ledger"


async def test_a_lane_that_may_spend_is_offered_the_purchase_and_refuses_if_it_declines(
    world, monkeypatch
):
    """The generated lane gets one corrected regeneration. When even that is
    not available -- the per-slide cap is spent -- it refuses like the rest."""
    offered = []

    async def broke(faults, reasons, template):
        # The shape the real one uses: no picture, and what the attempt cost.
        offered.append((faults, reasons, template))
        return None, "", 12_000

    with pytest.raises(pipeline.CompositeRejected) as err:
        await _final_check_on(world, monkeypatch, lane="fake", buy=broke)
    assert offered and offered[0][0] == ["text_over_subject"]
    assert "refunded" in err.value.hint
    assert err.value.cost_micros == 4_500 + 12_000, (
        "an attempt that bought nothing was still paid for, and says so"
    )


async def test_exhaustion_delivers_nothing_stores_nothing_and_refunds(world, monkeypatch):
    """Refusing to render beats shipping a flawed frame -- and a refusal that
    still charged, still uploaded and still sent would be the worst of both."""
    _fault_once(monkeypatch, faults=["contrast_below_bar"], on=99)
    monkeypatch.setattr(pipeline.compositeqa, "best_free_variant", _keep_first)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))

    # "generation_failed" with a stack trace under it is not something anybody
    # can act on, and it was all the first build ever said: the hint the final
    # check raises reached the agent on revisions only.
    assert res["ok"] is False and res["reason"] == "composite_quality"
    assert "final check" in res["errors"][0]
    assert "refunded" in res["hint"]
    assert world["refunded"] == 1
    assert not [k for k in world["blobs"] if k.endswith("composed.jpg")]
    assert world["images"] == []
    (row,) = world["rows"].values()
    assert row.status == "failed" and row.cost_micros > 0, "the spend is still on the ledger"


async def test_a_refused_owner_photo_asks_for_another_photo_not_another_generation(
    world, monkeypatch
):
    """A first build, not a revision. The owner's own photograph cannot be
    replaced by a bought one, so "try that slide again with regenerate_image"
    is the one thing the agent must not offer -- and it was the only thing the
    tool result ever said, because generate() dropped the refusal's hint."""
    real = pipeline._final_check

    async def refuse_slide_two(ctx, brief, slide, *a, **k):
        if slide.position != 2:
            return await real(ctx, brief, slide, *a, **k)
        raise pipeline.CompositeRejected(
            "the finished slide did not pass the final check (text_over_subject)",
            faults=["text_over_subject"],
            hint=pipeline._OWNER_PHOTO_REFUSAL,
        )

    monkeypatch.setattr(pipeline, "_final_check", refuse_slide_two)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE_CAROUSEL))

    assert res["ok"] and res["slides_ok"] == 2 and res["slides_failed"] == 1
    assert "another photo" in res["hint"] and "DOCUMENT" in res["hint"]
    assert "regenerate_image" not in res["note"]


async def test_a_retry_that_was_paid_for_and_then_refused_is_still_on_the_ledger(
    world, monkeypatch
):
    """The slide buys a second picture, the check refuses that one too, and
    nothing ships. Both pictures were still bought. Losing the second from the
    ledger would tell the owner the failure was half as expensive as it was --
    and the refund is computed from credits, not from this, so the number would
    simply have been wrong for ever."""
    world["final_verdicts"].extend([["artefacts"], ["artefacts"]])
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))

    assert res["ok"] is False and res["reason"] == "composite_quality"
    assert len(world["provider"].requests) == 2, "one retry, as allowed"
    assert world["refunded"] == 1
    assert not [k for k in world["blobs"] if k.endswith("composed.jpg")]
    (row,) = world["rows"].values()
    assert row.status == "failed"
    assert row.cost_micros == 2 * 288_300 + 2 * 4_500, "both pictures and both looks"


async def test_the_quality_event_records_the_verdict_that_was_kept_not_the_measurement(
    world, monkeypatch
):
    """quality_score keeps the inspector's verdict, and the event row beside it
    is the only thing a later calibration against the owner's answer has to
    correlate. The row used to carry the DETERMINISTIC score under the same
    key, because the QA dict was splatted after it and has a "score" of its
    own -- so the column said one number and the row said another."""
    recorded: list[dict] = []
    monkeypatch.setattr(pipeline.events, "record", lambda db, **kw: recorded.append(kw))

    async def strict(image, **copy):
        world["inspected"].append((image, copy))
        return bggate.Verdict([], "a little empty on the left", cost_micros=4_500, score=1)

    monkeypatch.setattr(finalgate, "inspect", strict)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"]

    (row,) = world["rows"].values()
    (quality,) = [r for r in recorded if r["kind"] == "quality"]
    assert row.quality_score == 1
    assert quality["meta"]["score"] == 1, "the verdict the column kept"
    assert quality["meta"]["measured_score"] > 1, "and the measurement, under its own name"
    assert quality["meta"]["inspector_notes"] == "a little empty on the left"
    assert quality["meta"]["faults"] == [] and quality["meta"]["reasons"] == []


async def test_an_inspector_outage_does_not_lose_the_picture_it_had_already_bought(
    world, monkeypatch
):
    """The slide buys its picture, then the inspector cannot be reached. Nothing
    ships and the credit comes back, but the vendor was still paid -- and this
    was the one refusal path that recorded nothing: _build_one caught only
    CompositeRejected, and InspectionUnavailable carried no figure to catch."""
    world["final_verdicts"].append(
        finalgate.InspectionUnavailable("inspector down", cost_micros=1_500)
    )
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))

    assert res["ok"] is False
    assert world["refunded"] == 1
    assert not [k for k in world["blobs"] if k.endswith("composed.jpg")]
    (row,) = world["rows"].values()
    assert row.status == "failed"
    assert row.cost_micros == 288_300 + 1_500, "the picture, and the looks that never answered"


async def test_a_revision_is_looked_at_as_hard_as_a_first_version(world, monkeypatch):
    """recompose had no picture check of ANY kind -- not the background gate,
    not anything -- and it is the lane the owner already had to ask twice for."""
    first = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert first["ok"]
    before = len(world["inspected"])
    res = await pipeline.recompose(
        world["ctx"],
        brief_id=uuid.UUID(first["brief_id"]),
        changes={"cta": "Order today"},
        owner_request="change the button",
    )
    assert res["ok"] and len(world["inspected"]) == before + 1
    assert len(world["provider"].requests) == 1, "a revision is still free"


async def test_the_switch_turns_the_whole_check_off(world, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "composite_gate_enabled", False)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"] and world["inspected"] == []
    (row,) = world["rows"].values()
    assert row.quality_score is None


async def test_the_mock_provider_is_not_sent_to_the_inspector(world, monkeypatch):
    """Exactly as the background gate exempts it: the mock draws a gradient for
    local development and there is no model output to judge."""
    monkeypatch.setattr(pipeline.settings, "imagegen_provider", "mock")
    world["provider"].name = "mock"
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"] and world["inspected"] == []


async def test_a_mocked_run_can_still_make_a_revision(world, monkeypatch):
    """The whole stack mocked and the REAL inspector in place: CI, and every
    developer with IMAGEGEN_PROVIDER=mock and no ANTHROPIC_API_KEY.

    The exemption used to be keyed on the lane string, and a revision's lane is
    "recomposed" -- never a provider name -- so it was never exempt: the slide
    asked an inspector that is not configured, InspectionUnavailable came back,
    nothing caught it, and every revision returned ok=False. The owner-photo,
    product-studio and reused lanes died the same way, for the same reason."""
    monkeypatch.setattr(pipeline.settings, "imagegen_provider", "mock")
    monkeypatch.setattr(pipeline.settings, "anthropic_api_key", "")
    monkeypatch.setattr(finalgate, "inspect", _REAL_INSPECT)
    world["provider"].name = "mock"
    assert not finalgate.available(), "the point of this test: no inspector to call"

    first = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert first["ok"] and first["slides_ok"] == 1
    res = await pipeline.recompose(
        world["ctx"],
        brief_id=uuid.UUID(first["brief_id"]),
        changes={"cta": "Order today"},
        owner_request="change the button",
    )
    assert res["ok"], res
    assert world["inspected"] == [], "nobody was asked, because nobody is there"


async def test_the_looks_at_a_carousel_happen_at_the_same_time(world, monkeypatch):
    """One vision call per delivered slide, ~2-5s each. Slides are already
    built concurrently, so a six-slide carousel waits for one of them rather
    than six -- but only if the call is made inside the per-slide work, which
    is what this pins.

    The check is a meeting, not a stopwatch. An earlier version gave the fake
    inspector a short sleep and asserted that two calls overlapped, which is a
    race the machine decides: the compositor renders under one global lock, so
    a slide's look is finished long before the next slide's render is, and on
    a slower runner no two ever overlapped. Here every slide but the last
    WAITS for the others, so the three can only meet if they are genuinely in
    flight together. A sequential pass after the renders leaves the first call
    waiting alone and the timeout fails the test instead of hanging it."""
    import asyncio

    slides = len(EXAMPLE_CAROUSEL["slides"])
    arrived, everyone = [], asyncio.Event()

    async def meet(image, **copy):
        arrived.append(1)
        if len(arrived) == slides:
            everyone.set()
        await asyncio.wait_for(everyone.wait(), timeout=30)
        return bggate.Verdict([], "", cost_micros=4_500, score=90)

    monkeypatch.setattr(finalgate, "inspect", meet)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    assert res["ok"] and res["slides_ok"] == 3
    assert len(arrived) == slides, "every delivered slide is looked at"


def test_one_look_per_slide_is_what_the_settings_are_priced_for():
    """The figure quoted in config.py, recomputed here so the two cannot drift:
    a 819x1024 JPEG at the documented ~(w*h)/750 plus the rubric, answered in
    JSON. Under 3% of what the picture it is checking costs."""
    image_tokens = (819 * 1024) / 750
    rubric_tokens = len(finalgate.prompt_for("Weekend Sale", "", "Order now", "Kadamba")) / 4
    per_slide = bggate.cost_micros(_Usage(round(image_tokens + rubric_tokens), 60))
    assert 5_500 <= per_slide <= 6_200, per_slide
    assert per_slide < 0.025 * 288_300, "a look is a rounding error against a picture"
    assert 6 * per_slide < 40_000, "a six-slide carousel adds well under four cents"
    capped = bggate.cost_micros(
        _Usage(round(image_tokens + rubric_tokens), bggate.INSPECT_MAX_TOKENS)
    )
    assert capped < 10_000, "even a chatty inspector cannot run the bill up"


async def test_a_revision_the_final_check_refuses_tells_the_agent_what_to_do(world, monkeypatch):
    """A revision never buys a picture, so a refusal is the end of the line for
    that arrangement -- and the agent can do something about it only if it is
    told what. A stack trace under "generation_failed" is not actionable."""
    first = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert first["ok"]
    world["images"].clear()
    _fault_once(monkeypatch, faults=["text_over_subject"], on=99)
    monkeypatch.setattr(pipeline.compositeqa, "best_free_variant", _keep_first)

    res = await pipeline.recompose(
        world["ctx"],
        brief_id=uuid.UUID(first["brief_id"]),
        changes={"cta": "Order today"},
        owner_request="change the button",
    )
    assert res["ok"] is False and res["reason"] == "composite_quality"
    assert "another photo" in res["hint"] or "different layout" in res["hint"]
    assert world["images"] == [], "nothing of a refused revision reaches the owner"
    assert len(world["provider"].requests) == 1, "and it never bought its way out"


async def test_the_inspector_is_shown_the_bytes_the_client_receives(world, monkeypatch):
    """Not the PNG before export, and not a re-render: the exported JPEG
    itself. A check on a proxy for the deliverable cannot see the faults the
    export introduces, and this package exists because nothing had ever looked
    at what the client actually gets."""
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"]
    ((shown, copy),) = world["inspected"]
    stored = [v for k, v in world["blobs"].items() if k.endswith("composed.jpg")]
    assert len(stored) == 1
    assert shown == stored[0][0], "the same bytes, not a second render of them"
    with Image.open(io.BytesIO(shown)) as im:
        assert im.format == "JPEG"
    # ...and it was told what the card is supposed to say.
    assert copy["headline"] == EXAMPLE["headline"]
    assert copy["cta"] == EXAMPLE["cta"] and copy["brand"] == "Kadamba Naturals"


def test_the_inspector_sees_a_1024px_copy_and_the_deliverable_is_untouched():
    """Scaled for the inspector only. The 1080x1350 JPEG that ships is never
    the thing that was resized."""
    buf = io.BytesIO()
    Image.new("RGB", (1080, 1350), (90, 110, 100)).save(buf, "JPEG", quality=93)
    original = buf.getvalue()
    small = bggate._thumbnail(original)
    with Image.open(io.BytesIO(small)) as im:
        assert max(im.size) == bggate.INSPECT_LONG_EDGE == 1024
        assert im.size == (819, 1024), "the 4:5 post, long edge 1024"
        assert im.format == "JPEG"
    assert len(small) < len(original)


def test_a_measured_fault_corrects_the_prompt_too():
    """A regeneration is asked for by whichever half refused the frame. A
    prompt that answered only the inspector would buy a second picture with
    the measured fault still in it."""
    out = finalgate.corrected("a brass diya on marble", ["contrast_below_bar"])
    assert out != "a brass diya on marble"
    assert finalgate.CORRECTIONS["text_hard_to_read"] in out
    for code in ("contrast_below_bar", "scrim_saturated", "text_over_subject"):
        assert code in finalgate.CORRECTIONS, code
