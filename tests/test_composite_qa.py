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

import pytest
from PIL import Image

from app.creative import bggate, compose, compositeqa, finalgate, legibility
from app.creative.brief import EXAMPLE, CreativeBrief


async def _noop(*a, **k):
    """Retry backoff, skipped: these tests pin the refusal, not the waiting."""


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
def test_a_gradient_standing_at_its_cap_asks_for_a_variant_but_ships():
    a = compositeqa.assess(_report(boost=legibility.MAX_BOOST), template="centered_overlay")
    assert a.metrics.scrim_saturated and a.repairs == ["scrim_saturated"]
    assert a.ok and a.worth_a_variant


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
