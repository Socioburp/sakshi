"""The probabilistic guarantee: a generated picture is inspected, a rejected one
is regenerated with a corrected prompt, and one that never passes is never
delivered. Plus the delivery model that replaced the latency budget."""

from __future__ import annotations

import asyncio
import io
import types

import pytest
from PIL import Image

from app.config import Settings
from app.creative import bggate, pipeline
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, CreativeBrief
from app.creative.imagegen.base import ImageResult
from app.telemetry.stages import Trace

SIZE = (1600, 2000)


def _png(size=SIZE, shade=90) -> bytes:
    im = Image.new("RGB", size, (shade, 110, 100))
    px = im.load()
    for y in range(0, size[1], 9):
        for x in range(0, size[0], 11):
            px[x, y] = ((x + shade) % 255, y % 255, (x * y) % 255)
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


class FakeProvider:
    name = "fake"
    exact_size = True

    def __init__(self, sizes=None, delay=0.0):
        self.requests, self.sizes, self.delay = [], list(sizes or []), delay
        self.in_flight = self.peak = 0

    async def generate(self, req):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            self.requests.append(req)
            size = self.sizes.pop(0) if self.sizes else (req.width, req.height)
            return ImageResult(
                data=_png(size, shade=40 * len(self.requests) % 255),
                mime="image/png",
                provider=self.name,
                job_id=f"job-{len(self.requests)}",
                cost_micros=288_300,
                raw={"model": "gpt-image-2-2026-04-21", "size": "1600x2000", "quality": "high"},
            )
        finally:
            self.in_flight -= 1


def _ctx():
    return types.SimpleNamespace(trace=Trace("test"))


def _verdicts(monkeypatch, *scripted):
    queue = list(scripted)
    seen = []

    async def fake_inspect(image):
        seen.append(image)
        reasons = queue.pop(0) if queue else []
        if isinstance(reasons, Exception):
            raise reasons
        return bggate.Verdict(list(reasons), "scripted")

    monkeypatch.setattr(bggate, "inspect", fake_inspect)
    return seen


async def _run(provider, *, brief=None, position=1, register=None):
    brief = brief or CreativeBrief.model_validate(EXAMPLE)
    slide = brief.units()[position - 1]
    return await pipeline._generate_checked(
        _ctx(), provider, brief, slide, "creative-id", "a brass diya on marble", "text",
        SIZE, register, f"slide{position}",
    )  # fmt: skip


# --------------------------------------------------------------------------- #
# reject -> corrected prompt -> accept
# --------------------------------------------------------------------------- #
async def test_a_rejected_picture_is_regenerated_with_a_corrected_prompt(monkeypatch):
    _verdicts(monkeypatch, ["text_or_lettering", "pagination_dots"], [])
    provider = FakeProvider()
    res, cost, rejections = await _run(provider)

    first, second = provider.requests
    assert res.job_id == "job-2", "the picture that PASSED is the one used"
    assert rejections == [
        {"attempt": 1, "reasons": ["text_or_lettering", "pagination_dots"], "notes": "scripted"}
    ]
    assert first.prompt == "a brass diya on marble"
    assert bggate.CORRECTIONS["text_or_lettering"] in second.prompt
    assert bggate.CORRECTIONS["pagination_dots"] in second.prompt
    # the retry is the same call at the same settings -- never a cheaper one
    assert (second.width, second.height, second.negative) == (first.width, first.height, "text")
    assert second.seed != first.seed or first.seed is None
    assert cost == 2 * 288_300, "both attempts were paid for and both are on the ledger"


async def test_corrections_accumulate_across_attempts(monkeypatch):
    _verdicts(monkeypatch, ["watermark"], ["subject_cropped"], [])
    provider = FakeProvider()
    _, _, rejections = await _run(provider)
    assert [r["attempt"] for r in rejections] == [1, 2]
    third = provider.requests[2].prompt
    assert bggate.CORRECTIONS["watermark"] in third
    assert bggate.CORRECTIONS["subject_cropped"] in third


@pytest.mark.parametrize("reason", bggate.REASONS)
async def test_every_reason_on_the_list_rejects(monkeypatch, reason):
    _verdicts(monkeypatch, [reason], [])
    provider = FakeProvider()
    _, _, rejections = await _run(provider)
    assert rejections[0]["reasons"] == [reason] and len(provider.requests) == 2


def test_the_reject_list_is_the_one_that_was_asked_for():
    assert set(bggate.REASONS) == {
        "text_or_lettering",
        "pagination_dots",
        "slide_numbers",
        "ui_chrome",
        "border_or_frame",
        "watermark",
        "phone_frame",
        "visible_artefacts",
        "subject_cropped",
    }
    for reason in bggate.REASONS:
        assert reason in bggate.PROMPT and bggate.CORRECTIONS[reason]


# --------------------------------------------------------------------------- #
# exhaustion: fail, never deliver
# --------------------------------------------------------------------------- #
async def test_on_exhaustion_the_slide_fails_and_no_rejected_picture_is_returned(monkeypatch):
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_attempts", 4)
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_budget_micros", 0)  # count only
    _verdicts(monkeypatch, *[["text_or_lettering"]] * 4)
    provider = FakeProvider()
    with pytest.raises(pipeline.BackgroundRejected, match="4 attempts") as err:
        await _run(provider)
    assert len(provider.requests) == 4
    assert err.value.cost_micros == 4 * 288_300, "the spend is recorded even though nothing shipped"
    assert [r["attempt"] for r in err.value.rejections] == [1, 2, 3, 4]


async def test_the_gate_stops_at_its_dollar_cap_and_still_delivers_nothing(monkeypatch):
    """One credit in, unbounded vendor spend out, was the hole. The cap ends the
    RETRYING; every call made is still the full-quality call."""
    assert Settings.model_fields["imagegen_gate_budget_micros"].default == 900_000
    _verdicts(monkeypatch, *[["watermark"]] * 6)
    provider = FakeProvider()
    with pytest.raises(pipeline.BackgroundRejected, match="3 attempts") as err:
        await _run(provider)
    assert len(provider.requests) == 3 and err.value.cost_micros == 3 * 288_300 <= 900_000
    assert {(r.width, r.height) for r in provider.requests} == {SIZE}


async def test_a_pass_inside_the_cap_is_unaffected_by_it(monkeypatch):
    _verdicts(monkeypatch, ["watermark"], ["watermark"], [])
    provider = FakeProvider()
    res, cost, rejections = await _run(provider)
    assert res.job_id == "job-3" and cost == 3 * 288_300 and len(rejections) == 2


def test_the_retry_cap_means_never_not_twice():
    assert Settings.model_fields["imagegen_gate_attempts"].default >= 5


async def test_the_gate_fails_closed_when_the_inspector_is_unavailable(monkeypatch):
    _verdicts(monkeypatch, bggate.InspectionUnavailable("down"))
    with pytest.raises(bggate.InspectionUnavailable):
        await _run(FakeProvider())


async def test_without_a_vision_model_nothing_generated_can_pass(monkeypatch):
    monkeypatch.setattr(bggate.settings, "anthropic_api_key", "")
    assert bggate.available() is False
    with pytest.raises(bggate.InspectionUnavailable, match="cannot be inspected"):
        await bggate.inspect(_png((256, 320)))


async def test_a_picture_of_the_wrong_size_is_rejected_not_cropped(monkeypatch):
    seen = _verdicts(monkeypatch, [])
    provider = FakeProvider(sizes=[(1024, 1536)])
    res, _, rejections = await _run(provider)
    assert rejections[0]["reasons"] == ["wrong_size"] and "1024x1536" in rejections[0]["notes"]
    with Image.open(io.BytesIO(res.data)) as im:
        assert im.size == SIZE, "generation output is exactly the configured 4:5 size"
    assert len(seen) == 1, "a wrong-size picture is not worth an inspection call"


async def test_a_duplicate_of_an_earlier_slide_is_a_rejection_too(monkeypatch):
    _verdicts(monkeypatch, [], [])
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    register = pipeline.dedupe_register()
    calls = iter([True, False])

    async def fake_register(reg, slide, image, force=False):
        return next(calls)

    monkeypatch.setattr(pipeline, "_register_or_reroll", fake_register)
    provider = FakeProvider()
    _, _, rejections = await _run(provider, brief=brief, position=2, register=register)
    assert rejections[0]["reasons"] == ["duplicate_of_earlier_slide"]
    assert bggate.CORRECTIONS["duplicate_of_earlier_slide"] in provider.requests[1].prompt


async def test_the_mock_gradient_is_not_sent_to_the_inspector(monkeypatch):
    seen = _verdicts(monkeypatch)
    provider = FakeProvider()
    provider.name = "mock"
    await _run(provider)
    assert seen == []


# --------------------------------------------------------------------------- #
# the inspector's answer
# --------------------------------------------------------------------------- #
def _answer(**flags) -> str:
    import json

    body = {k: False for k in bggate.REASONS} | flags | {"notes": ""}
    return "Here you go:\n```json\n" + json.dumps(body) + "\n```"


def test_a_clean_verdict_passes_and_a_flag_rejects():
    assert bggate.parse(_answer()).ok
    verdict = bggate.parse(_answer(phone_frame=True, watermark=True))
    assert verdict.reasons == ["watermark", "phone_frame"] and not verdict.ok


@pytest.mark.parametrize("text", ["looks fine to me!", "{not json", '{"text_or_lettering": false}'])
def test_an_unusable_answer_is_not_a_pass(text):
    with pytest.raises(bggate.InspectionUnavailable):
        bggate.parse(text)


def test_a_non_boolean_flag_is_not_a_pass():
    with pytest.raises(bggate.InspectionUnavailable, match="missing"):
        bggate.parse(_answer(watermark="maybe"))


def test_corrections_are_added_once():
    once = bggate.corrected("a diya", ["watermark", "watermark"])
    assert once.count(bggate.CORRECTIONS["watermark"]) == 1
    assert bggate.corrected(once, ["watermark"]) == once
    assert bggate.corrected("a diya", []) == "a diya"


# --------------------------------------------------------------------------- #
# speed is solved by delivery
# --------------------------------------------------------------------------- #
async def test_vendor_calls_are_parallel_but_bounded(monkeypatch):
    monkeypatch.setattr(pipeline.settings, "imagegen_concurrency", 2)
    _verdicts(monkeypatch)
    brief = CreativeBrief.model_validate(
        {
            **EXAMPLE_CAROUSEL,
            "slides": [{**EXAMPLE_CAROUSEL["slides"][i % 3], "position": i + 1} for i in range(6)],
        }
    )
    provider, register = FakeProvider(delay=0.02), pipeline.dedupe_register()

    async def no_dupes(*a, **k):
        return False

    monkeypatch.setattr(pipeline, "_register_or_reroll", no_dupes)
    await asyncio.gather(
        *(_run(provider, brief=brief, position=p, register=register) for p in range(1, 7))
    )
    assert len(provider.requests) == 6 and provider.peak == 2


class _Chat:
    def __init__(self, fail_on=()):
        self.images, self.lines, self.fail_on = [], [], set(fail_on)

    async def show(self, url, caption=""):
        self.images.append((url, caption))
        return url not in self.fail_on

    show_video = show

    async def progress(self, text):
        self.lines.append(text)
        return True


async def test_as_ready_sends_slides_as_they_finish_and_says_where_they_belong(monkeypatch):
    monkeypatch.setattr(pipeline.settings, "carousel_delivery", "as_ready")
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    chat = _Chat()
    delivery = pipeline._Delivery(chat, brief, 3, "en")

    async def finish(position, after):
        await asyncio.sleep(after)
        await delivery.send(position, f"u{position}")

    await asyncio.gather(finish(1, 0.03), finish(2, 0.0), finish(3, 0.015))
    assert [u for u, _ in chat.images] == ["u2", "u3", "u1"], "not held for the slowest slide"
    assert dict(chat.images) == {
        "u1": f"1/3 · {brief.headline}",
        "u2": "2/3",
        "u3": "3/3",
    }
    assert delivery.sent == {1: True, 2: True, 3: True}


async def test_by_default_a_carousel_arrives_as_a_set_in_order():
    """Slides finish 2, 3, 1. The owner sees 1, 2, 3 -- a set they can forward."""
    assert Settings.model_fields["carousel_delivery"].default == "ordered"
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    chat = _Chat()
    delivery = pipeline._Delivery(chat, brief, 3, "en")

    async def finish(position, after):
        await asyncio.sleep(after)
        await delivery.send(position, f"u{position}")

    await asyncio.gather(finish(1, 0.03), finish(2, 0.0), finish(3, 0.015))
    assert chat.images == [] and sorted(delivery.ready) == [1, 2, 3], "held, and counted as ready"
    await delivery.flush()
    assert [u for u, _ in chat.images] == ["u1", "u2", "u3"]
    assert [c.split(" ")[0] for _, c in chat.images] == ["1/3", "2/3", "3/3"]
    await delivery.flush()
    assert len(chat.images) == 3, "flushing twice sends nothing twice"


async def test_a_missing_slide_does_not_hold_up_the_rest_of_the_set():
    chat = _Chat()
    delivery = pipeline._Delivery(chat, CreativeBrief.model_validate(EXAMPLE_CAROUSEL), 3, "en")
    await delivery.send(3, "u3")
    await delivery.send(1, "u1")
    await delivery.flush()
    assert [u for u, _ in chat.images] == ["u1", "u3"]


async def test_a_single_post_is_never_held():
    chat = _Chat()
    delivery = pipeline._Delivery(chat, CreativeBrief.model_validate(EXAMPLE), 1, "en")
    await delivery.send(1, "u1")
    assert [u for u, _ in chat.images] == ["u1"]


async def test_a_slow_job_says_so_in_the_chat_and_changes_nothing_else(monkeypatch):
    monkeypatch.setattr(pipeline.settings, "slow_notice_s", 0.02)
    chat = _Chat()
    delivery = pipeline._Delivery(chat, CreativeBrief.model_validate(EXAMPLE_CAROUSEL), 3, "en")
    delivery.start()
    await asyncio.sleep(0.03)
    await delivery.send(2, "u2")
    await asyncio.sleep(0.05)
    await delivery.stop()
    assert len(chat.lines) == 2 and chat.lines[0].startswith("Still working")
    assert "0 of 3" in chat.lines[0] and "1 of 3" in chat.lines[1]
    await asyncio.sleep(0.2)
    assert len(chat.lines) == 2, "stopped means stopped"


async def test_a_failed_send_is_reported_not_assumed():
    chat = _Chat(fail_on={"u1"})
    delivery = pipeline._Delivery(chat, CreativeBrief.model_validate(EXAMPLE), 1, "hi")
    assert await delivery.send(1, "u1") is False and delivery.sent == {1: False}


# --------------------------------------------------------------------------- #
# what the chat promises, and how the waiting ends
# --------------------------------------------------------------------------- #
def test_the_time_the_chat_promises_is_the_time_the_gate_actually_allows(monkeypatch):
    """working_line said "about 90 seconds" under a comment admitting the
    number had never been measured, while the gate above it bought up to three
    pictures of up to two minutes each. The owner waited ten minutes on a
    ninety-second promise and wrote "it's taking too much time"."""
    monkeypatch.setattr(pipeline.settings, "imagegen_provider", "openai")
    monkeypatch.setattr(pipeline.settings, "imagegen_cost_micros", 0)
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_attempts", 6)
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_budget_micros", 900_000)
    monkeypatch.setattr(pipeline.settings, "imagegen_concurrency", 4)

    # $0.90 of budget against $0.2883 a picture is three tries, not six.
    assert pipeline.paid_attempts() == 3
    low, high = pipeline.working_window(1)
    assert low == pipeline.VENDOR_CEILING_S + pipeline.COMPOSITING_S
    assert high == 3 * pipeline.VENDOR_CEILING_S + pipeline.COMPOSITING_S
    line = pipeline.working_line("en", 1)
    assert "90 second" not in line and "2-7 minutes" in line
    assert "{low}" not in pipeline.working_line("hi", 1) and "90" not in pipeline.working_line(
        "hi", 1
    )
    # A carousel wider than the lanes waits through more than one round of calls.
    assert pipeline.working_window(6)[1] == 2 * (high - pipeline.COMPOSITING_S) + (
        pipeline.COMPOSITING_S
    )


def test_a_smaller_budget_shortens_the_promise_and_never_the_work(monkeypatch):
    """The promise is derived, so it cannot drift away from the code. The only
    honest way to quote a shorter time is to allow fewer RETRIES; nothing here
    may make the picture worse to hit a number."""
    monkeypatch.setattr(pipeline.settings, "imagegen_provider", "openai")
    monkeypatch.setattr(pipeline.settings, "imagegen_cost_micros", 0)
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_attempts", 6)
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_budget_micros", 300_000)
    assert pipeline.paid_attempts() == 1 and pipeline.working_minutes(1) == (2, 3)
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_budget_micros", 0)
    assert pipeline.paid_attempts() == 6, "no cap means the attempt count is the whole truth"


def test_no_notice_says_what_the_last_one_already_said():
    """Five copies of "Still working on it... the high-quality picture takes a
    little longer" is not a progress report. Every line carries a number that
    has moved since the owner last read one."""
    delivery = pipeline._Delivery(_Chat(), CreativeBrief.model_validate(EXAMPLE), 1, "en")
    said = [delivery.notice_at(pipeline.settings.slow_notice_s * m) for m in pipeline._NOTICE_AT]
    assert len(set(said)) == len(said) == 2
    assert said[0].startswith("Still working") and "1 minute in" in said[0]
    assert "3 minutes in" in said[1]
    hindi = pipeline._Delivery(_Chat(), CreativeBrief.model_validate(EXAMPLE), 1, "hi")
    assert "minute ho gaye" in hindi.notice_at(60)


async def test_the_waiting_ends_with_one_honest_line_instead_of_going_silent(monkeypatch):
    """After the last notice the chat used to say nothing at all, for ever.
    The owner is told once that the job has run past the window it was quoted,
    and then left alone: from there it either arrives or says it failed."""
    monkeypatch.setattr(pipeline.settings, "slow_notice_s", 0.02)
    chat = _Chat()
    delivery = pipeline._Delivery(chat, CreativeBrief.model_validate(EXAMPLE), 1, "en")
    assert delivery.over_min == pipeline.working_minutes(1)[1], "the same number it quoted"
    delivery.over_s, delivery.over_min = 0.1, 7  # the quoted window, compressed
    delivery.start()
    await asyncio.sleep(0.16)
    await delivery.stop()

    assert len(chat.lines) == 3
    assert [line.startswith("Still working") for line in chat.lines] == [True, True, False]
    assert "past the 7 minutes I said" in chat.lines[2]
    assert "worse picture" in chat.lines[2] and "credit comes back" in chat.lines[2]
    await asyncio.sleep(0.15)
    assert len(chat.lines) == 3, "the wait ends; it does not roam in circles"


def test_there_is_no_tier_and_no_deadline_that_changes_the_output():
    import inspect as pyinspect

    from app.creative.imagegen import providers

    src = pyinspect.getsource(pipeline) + pyinspect.getsource(providers.OpenAIProvider)
    for word in ("tier_down", "step_down", "fallback_quality", '"medium"', '"low"', "wait_for("):
        assert word not in src, word
