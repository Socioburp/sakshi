"""pipeline.generate end to end WITHOUT a database.

test_pipeline_db.py / test_ops_db.py cover this against real Postgres and skip
where there is none. This runs the same orchestration -- layout gate, charge,
bounded parallel generation, the background gate, compositing, export, and
slide-by-slide delivery -- with only the storage layer faked, so the control
flow is exercised on every machine.
"""

from __future__ import annotations

import io
import types
import uuid
from contextlib import contextmanager

import pytest
from PIL import Image

from app.creative import bggate, compose, pipeline
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, POST_SIZE, CreativeBrief
from app.creative.imagegen.base import ImageResult
from app.telemetry.stages import Trace


class _Db:
    def __init__(self, world):
        self.world = world

    def get(self, model, key):
        name = getattr(model, "__name__", str(model))
        if name == "Brand":
            return self.world["brand"]
        if name == "Account":
            return types.SimpleNamespace(locale="en", credits_balance=self.world["balance"])
        if name == "Brief":
            return self.world["briefs"].get(key)
        return self.world["rows"].get(key)

    def add(self, row):
        row.id = uuid.uuid4()
        self.world["rows"][row.id] = row

    def flush(self):
        pass


class _Provider:
    name = "fake"
    exact_size = True

    def __init__(self):
        self.requests = []

    async def generate(self, req):
        self.requests.append(req)
        n = len(self.requests)
        # Structurally different every call, or the duplicate register (rightly)
        # rejects slide 2 as a copy of slide 1.
        import random

        from PIL import ImageDraw

        rnd = random.Random(n)
        im = Image.new("RGB", (req.width, req.height), (70, 90, 80))
        d = ImageDraw.Draw(im)
        for _ in range(9):
            x, y = rnd.randint(0, req.width), rnd.randint(0, req.height)
            r = rnd.randint(150, 500)
            d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(rnd.randint(20, 235) for _ in "rgb"))
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        return ImageResult(
            data=buf.getvalue(), mime="image/png", provider=self.name, job_id=f"job-{n}",
            cost_micros=288_300, raw={"model": "gpt-image-2-2026-04-21", "quality": "high"},
        )  # fmt: skip


@pytest.fixture
async def world(monkeypatch):
    try:
        await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium: {exc}")
    w = {
        "rows": {},
        "briefs": {},
        "blobs": {},
        "balance": 10,
        "charged": 0,
        "refunded": 0,
        "images": [],
        "lines": [],
        "provider": _Provider(),
        "brand": types.SimpleNamespace(
            id=uuid.uuid4(),
            name="Kadamba Naturals",
            category="food",
            never_say=[],
            template_prefs={"signature": "none"},
            palette={"primary": "#123B2E", "accent": "#E4572E"},
        ),  # fmt: skip
    }

    @contextmanager
    def scope():
        yield _Db(w)

    def charge(db, *, units, **kw):
        w["charged"] += units

    def refund(db, *, amount, **kw):
        w["refunded"] += amount

    monkeypatch.setattr(pipeline, "session_scope", scope)

    def save_brief(db, **kw):
        parent = kw.get("parent")
        bid = uuid.uuid4()
        row = types.SimpleNamespace(
            id=bid,
            payload=kw["payload"],
            status="draft",
            parent_brief_id=parent.id if parent else None,
            version=(parent.version + 1) if parent else 1,
            root_brief_id=(parent.root_brief_id if parent else bid),
        )
        w["briefs"][bid] = row  # so a revision can db.get(Brief, ...) its parent
        return row

    def creatives_for_brief(db, brief_id):
        rows = [r for r in w["rows"].values() if getattr(r, "brief_id", None) == brief_id]
        return sorted(rows, key=lambda r: r.slide_position)

    monkeypatch.setattr(pipeline.repo, "save_brief", save_brief)
    monkeypatch.setattr(pipeline.repo, "creatives_for_brief", creatives_for_brief)
    monkeypatch.setattr(pipeline.events, "record", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.credits, "charge", charge)
    monkeypatch.setattr(pipeline.credits, "refund", refund)
    monkeypatch.setattr(pipeline.credits, "cost_of", lambda action: 1)
    monkeypatch.setattr(pipeline, "_resolve_photos", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_assets", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_schedule_daily_nudge", lambda ctx: None)
    monkeypatch.setattr(pipeline, "get_provider", lambda: w["provider"])
    monkeypatch.setattr(pipeline, "_snapshot", lambda db, brand: types.SimpleNamespace(
        name=brand.name, category=brand.category, logo_url=None, logo_src=None, logo_analysis={},
        palette=brand.palette, fonts={"heading": "Poppins", "body": "Inter"}, never_say=[],
        template_prefs=brand.template_prefs))  # fmt: skip
    monkeypatch.setattr(pipeline.r2, "key_for", lambda b, c, suffix, **k: f"drafts/{c}-{suffix}")
    monkeypatch.setattr(pipeline.r2, "put", lambda key, data, ct=None: (
        w["blobs"].__setitem__(key, (data, ct)) or f"https://cdn.test/{key}"))  # fmt: skip
    monkeypatch.setattr(pipeline.r2, "public_url", lambda key: f"https://cdn.test/{key}")
    monkeypatch.setattr(pipeline.r2, "get", lambda key: w["blobs"][key][0])

    async def say(text, **kw):
        w["lines"].append(text)
        return True

    async def show(url, caption=""):
        w["images"].append((url, caption))
        return True

    w["ctx"] = types.SimpleNamespace(
        account_id=uuid.uuid4(), brand_id=w["brand"].id, message_id=None, wa_id="9198",
        trace=Trace("flow"), say=say, progress=say, show=show, show_video=show,
    )  # fmt: skip
    yield w
    await compose.shutdown()


def _clean(monkeypatch, *scripted):
    queue = list(scripted)

    async def fake_inspect(image):
        return bggate.Verdict(list(queue.pop(0)) if queue else [], "")

    monkeypatch.setattr(bggate, "inspect", fake_inspect)


async def test_a_carousel_goes_out_whole_at_one_size_as_jpeg(world, monkeypatch):
    _clean(monkeypatch)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE_CAROUSEL))

    assert res["ok"] and res["slides_ok"] == 3 and res["slides_failed"] == 0
    assert res["shown_to_user"] is True and res["credits_charged"] == 3 == world["charged"]
    assert world["lines"][0].startswith("Making it"), "acknowledged before the work starts"
    assert sorted(c for _, c in world["images"]) == sorted(
        ["1/3 · 3 ways to use cold-pressed oil", "2/3", "3/3"]
    )
    # every request: native 4:5 at the configured size, identical settings
    assert {(r.width, r.height) for r in world["provider"].requests} == {(1600, 2000)}
    # every export: a JPEG of exactly the post size -- identical across the set
    finals = [v for k, v in world["blobs"].items() if k.endswith("composed.jpg")]
    assert len(finals) == 3
    sizes = set()
    for data, content_type in finals:
        with Image.open(io.BytesIO(data)) as im:
            assert im.format == "JPEG" and content_type == "image/jpeg"
            sizes.add(im.size)
    assert sizes == {POST_SIZE}
    # the generated source is kept lossless
    assert all(ct == "image/png" for k, (_, ct) in world["blobs"].items() if "-bg." in k)
    rows = list(world["rows"].values())
    assert all(r.status == "ready" and r.cost_micros == 288_300 for r in rows)
    assert all((r.width, r.height) == POST_SIZE for r in rows)


async def test_a_slide_that_never_passes_is_refunded_and_never_sent(world, monkeypatch):
    monkeypatch.setattr(pipeline.settings, "imagegen_gate_attempts", 2)

    async def picky(image):
        # slide 2's shot is the macro "detail" rung; fail every picture of it
        return bggate.Verdict([], "")

    monkeypatch.setattr(bggate, "inspect", picky)
    real = pipeline._generate_checked

    async def fail_slide_two(ctx, provider, brief, slide, *a, **k):
        if slide.position == 2:
            raise pipeline.BackgroundRejected(
                "no acceptable picture in 2 attempts (text_or_lettering)", cost_micros=576_600
            )
        return await real(ctx, provider, brief, slide, *a, **k)

    monkeypatch.setattr(pipeline, "_generate_checked", fail_slide_two)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE_CAROUSEL))

    assert res["ok"] and res["slides_ok"] == 2 and res["slides_failed"] == 1
    assert res["credits_charged"] == 2 and world["refunded"] == 1
    assert "slide 2" in res["failed_slides"][0] and "refunded" in res["note"]
    assert sorted(c.split(" ")[0] for _, c in world["images"]) == ["1/3", "3/3"]
    failed = [r for r in world["rows"].values() if r.status == "failed"]
    assert len(failed) == 1 and failed[0].cost_micros == 576_600, "the spend is still on the ledger"


async def test_copy_that_cannot_be_set_costs_nothing_and_makes_nothing(world, monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setattr(compose, "SAFE_PAD", 420)
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"] is False and res["reason"] == "copy_does_not_fit" and res["charged"] == 0
    assert world["charged"] == 0 and world["provider"].requests == [] and world["images"] == []
    assert world["rows"] == {} and world["lines"] == []


async def test_a_single_post_uses_the_same_settings_as_a_carousel_slide(world, monkeypatch):
    _clean(monkeypatch, ["watermark"], [])
    res = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert res["ok"] and res["credits_charged"] == 1
    first, second = world["provider"].requests
    assert (first.width, first.height) == (second.width, second.height) == (1600, 2000)
    assert bggate.CORRECTIONS["watermark"] in second.prompt
    assert "#123B2E" in first.prompt, "the brand's exact hex reaches the image prompt"
    # ...and the agent is handed what was known and used, to say out loud.
    assert [f["kind"] for f in res["remembered"]] == ["brand_colours"]
    assert "never add a memory" in res["remembered_hint"].lower()
    (row,) = world["rows"].values()
    assert row.cost_micros == 2 * 288_300
    assert world["images"] == [(res["image_urls"][0], EXAMPLE["headline"])]


async def test_a_revision_refused_on_the_rendered_frame_is_contained(world, monkeypatch):
    """The layout gate measures on a blank background; legibility is measured
    on the real photograph, inside compose. A revision that fails there used
    to escape recompose as a raw exception and leave the new rows 'composing'
    for ever: nothing told the agent, and the owner heard nothing."""
    _clean(monkeypatch)
    first = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE_CAROUSEL))
    assert first["ok"] and first["slides_ok"] == 3
    world["images"].clear()
    real = compose.compose

    async def refuse_slide_two(brief, slide, *a, **k):
        if slide.position == 2:
            raise compose.LegibilityError({"headline": 2.9}, position=2)
        return await real(brief, slide, *a, **k)

    monkeypatch.setattr(compose, "compose", refuse_slide_two)
    res = await pipeline.recompose(
        world["ctx"],
        brief_id=uuid.UUID(first["brief_id"]),
        changes={"cta": "Order today"},
        owner_request="change the button",
    )

    assert res["ok"] is False and res["reason"] == "legibility", res
    assert len(res["errors"]) == 1 and res["errors"][0].startswith("slide 2:")
    assert "headline 2.9:1" in res["errors"][0]
    assert "regenerate_image" in res["hint"] and "credits_charged" not in res
    assert world["images"] == [], "nothing of a refused revision reaches the owner"
    revised = [
        r for r in world["rows"].values()
        if getattr(r, "brief_id", None) not in (None, uuid.UUID(first["brief_id"]))
    ]  # fmt: skip
    assert len(revised) == 3 and not any(r.status == "composing" for r in revised)
    (failed,) = [r for r in revised if r.status == "failed"]
    assert failed.slide_position == 2 and "legibility guarantee" in failed.error
    kept = [
        r for r in world["rows"].values()
        if getattr(r, "brief_id", None) == uuid.UUID(first["brief_id"])
    ]  # fmt: skip
    assert all(r.status == "ready" for r in kept), "the version they already have is untouched"


async def test_a_revision_that_breaks_for_any_other_reason_is_generation_failed(world, monkeypatch):
    _clean(monkeypatch)
    first = await pipeline.generate(world["ctx"], CreativeBrief.model_validate(EXAMPLE))
    assert first["ok"]
    world["images"].clear()

    async def broken(brief, slide, *a, **k):
        raise RuntimeError("chromium went away")

    monkeypatch.setattr(compose, "compose", broken)
    res = await pipeline.recompose(
        world["ctx"],
        brief_id=uuid.UUID(first["brief_id"]),
        changes={"headline": "Aaj hi lein"},
        owner_request="Hindi headline",
    )
    assert res["ok"] is False and res["reason"] == "generation_failed"
    assert res["errors"] == ["slide 1: chromium went away"] and "hint" not in res
    assert world["images"] == []
    statuses = sorted(r.status for r in world["rows"].values() if hasattr(r, "slide_position"))
    assert statuses == ["failed", "ready"]
