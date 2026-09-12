"""The three FLUX vendors, each against its published contract, without money.

respx answers as the vendor would. What is asserted is the shape we SEND (the
thing a wrong guess breaks silently) and what we do with the reply: download
the picture, refuse a blank, retry a 429, poll until Ready.
"""

from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from app.creative.imagegen import providers as P
from app.creative.imagegen.base import ImageRequest


def _jpeg(w=1080, h=1350, *, blank=False) -> bytes:
    im = Image.new("RGB", (w, h), (120, 90, 60))
    if not blank:
        px = im.load()
        for y in range(h):
            for x in range(0, w, 7):
                px[x, y] = ((x * 3) % 255, (y * 2) % 255, (x + y) % 255)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


REQ = ImageRequest(
    prompt="a wooden table by a window with mangoes on it",
    negative="text, watermark, logo, 3d render, blurry",
    width=1080,
    height=1350,
    seed=7,
)


async def _no_sleep(_s):
    return None


# --------------------------------------------------------------------------- #
# prompt shaping
# --------------------------------------------------------------------------- #
def test_flux_prompt_folds_negatives_into_positives():
    p = P.flux_prompt(REQ.prompt, REQ.negative)
    assert p.startswith("a wooden table by a window with mangoes on it.")
    assert "unmarked surfaces" in p  # text/watermark/logo
    assert "real photograph" in p  # 3d render
    assert "tack-sharp" in p  # blurry
    # Nothing is phrased as a prohibition the model would latch onto, and no
    # replacement smuggles in a noun the brief never asked for.
    assert " no " not in f" {p.lower()} " and "without" not in p.lower()
    assert "packaging" not in p and "skin" not in p
    assert len(p) <= P.FLUX_PROMPT_MAX


def test_flux_prompt_without_negative_is_the_prompt():
    assert P.flux_prompt("  a plate of  biryani ", "") == "a plate of biryani."
    assert P.flux_prompt("x", None) == "x."


def test_folded_positives_sit_before_the_camera_clause():
    """photoreal appends 'Shot ...'; the positives go in front of it, so if the
    256-token window cuts anything it is the tail, not the replacements."""
    from app.creative import photoreal

    prompt, negative = photoreal.photographic(
        "A brass diya on a marble ledge, morning light from the left, copy space above",
        "text, logo, watermark",
        mood="warm, festive",
        category="jewellery",
    )
    p = P.flux_prompt(prompt, negative)
    assert p.index("Clean unmarked surfaces") < p.index(photoreal.CAMERA_MARKER)
    assert p.endswith(".") and p.count(photoreal.CAMERA_MARKER) == 1


def test_long_prompts_are_trimmed_at_a_clause_boundary():
    long = ", ".join(f"detail number {i} of the scene" for i in range(80))
    p = P.flux_prompt(long, "text")
    assert len(p) <= P.FLUX_PROMPT_MAX and p.endswith(".")
    assert not p.endswith("of.") and "number" in p  # no mid-word cut


def test_nearest_replicate_ratio():
    assert P.nearest_ratio(1080, 1080) == "1:1"
    assert P.nearest_ratio(1080, 1350) == "4:5"
    assert P.nearest_ratio(1080, 1920) == "9:16"
    assert P.nearest_ratio(1920, 1080) == "16:9"


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_gate_refuses_blank_tiny_and_garbage():
    with pytest.raises(P.BlankImageError):
        P.HttpImageProvider._gate(_jpeg(blank=True), "image/jpeg")
    # A flat pastel sweep (a dozen levels top to bottom) is a legitimate backdrop.
    im = Image.new("RGB", (1080, 1350), (230, 220, 210))
    px = im.load()
    for y in range(1350):
        for x in range(1080):
            px[x, y] = (230 - (y * 12) // 1350, 220, 210)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    assert P.HttpImageProvider._gate(buf.getvalue(), "image/jpeg")[1] == "image/jpeg"
    with pytest.raises(P.ImageGenError):
        P.HttpImageProvider._gate(_jpeg(100, 100), "image/jpeg")
    with pytest.raises(P.ImageGenError):
        P.HttpImageProvider._gate(b"not an image", "image/jpeg")
    data, mime = P.HttpImageProvider._gate(_jpeg(), "image/jpeg")
    assert mime == "image/jpeg" and data


def test_gate_converts_webp_to_jpeg():
    im = Image.open(io.BytesIO(_jpeg()))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=80)
    data, mime = P.HttpImageProvider._gate(buf.getvalue(), "image/webp")
    assert mime == "image/jpeg" and Image.open(io.BytesIO(data)).format == "JPEG"


# --------------------------------------------------------------------------- #
# fal
# --------------------------------------------------------------------------- #
@respx.mock
async def test_fal_sends_the_documented_shape_and_downloads(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "k")
    monkeypatch.setattr(P.settings, "imagegen_fal_model", "fal-ai/flux/schnell")
    seen = {}

    def submit(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "images": [
                    {
                        "url": "https://cdn.example/out.jpg",
                        "width": 1080,
                        "height": 1350,
                        "content_type": "image/jpeg",
                    }
                ],
                "seed": 7,
                "has_nsfw_concepts": [False],
                "prompt": "…",
            },
            headers={"x-fal-request-id": "req-123"},
        )

    respx.post("https://fal.run/fal-ai/flux/schnell").mock(side_effect=submit)
    respx.get("https://cdn.example/out.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})
    )
    res = await P.FalProvider().generate(REQ)
    assert seen["auth"] == "Key k"
    b = seen["body"]
    assert b["image_size"] == {"width": 1080, "height": 1350}
    assert b["num_inference_steps"] == 4 and b["seed"] == 7 and b["output_format"] == "jpeg"
    assert "negative" not in b and "negative_prompt" not in b
    assert "unmarked surfaces" in b["prompt"]
    assert res.provider == "fal" and res.mime == "image/jpeg" and res.seed == 7
    assert res.job_id == "req-123" and res.cost_micros == P.DEFAULT_COST_MICROS["fal"]
    assert Image.open(io.BytesIO(res.data)).size == (1080, 1350)


@respx.mock
async def test_fal_data_uri_reply_needs_no_download(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "k")
    import base64

    uri = "data:image/jpeg;base64," + base64.b64encode(_jpeg()).decode()
    respx.post("https://fal.run/fal-ai/flux/schnell").mock(
        return_value=httpx.Response(200, json={"images": [{"url": uri}], "seed": 1})
    )
    res = await P.FalProvider().generate(REQ)
    assert res.mime == "image/jpeg" and len(res.data) > 1000


@respx.mock
async def test_fal_retries_a_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "k")
    monkeypatch.setattr(P.FalProvider, "BACKOFF", (0.0, 0.0))
    route = respx.post("https://fal.run/fal-ai/flux/schnell")
    route.side_effect = [
        httpx.Response(429, json={"detail": "slow down"}),
        httpx.Response(200, json={"images": [{"url": "https://cdn.example/o.jpg"}], "seed": 1}),
    ]
    respx.get("https://cdn.example/o.jpg").mock(return_value=httpx.Response(200, content=_jpeg()))
    res = await P.FalProvider().generate(REQ)
    assert route.call_count == 2 and res.provider == "fal"


@respx.mock
async def test_fal_blank_picture_is_an_error_not_a_background(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "k")
    respx.post("https://fal.run/fal-ai/flux/schnell").mock(
        return_value=httpx.Response(200, json={"images": [{"url": "https://cdn.example/b.jpg"}]})
    )
    respx.get("https://cdn.example/b.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg(blank=True))
    )
    with pytest.raises(P.BlankImageError):
        await P.FalProvider().generate(REQ)


@respx.mock
async def test_a_download_blip_never_resubmits_the_generation(monkeypatch):
    """A 503 from the CDN after the vendor already generated (and billed)
    must retry the GET, not POST a second generation."""
    monkeypatch.setattr(P.settings, "fal_key", "k")
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    submit = respx.post("https://fal.run/fal-ai/flux/schnell").mock(
        return_value=httpx.Response(200, json={"images": [{"url": "https://cdn.example/d.jpg"}]})
    )
    dl = respx.get("https://cdn.example/d.jpg")
    dl.side_effect = [
        httpx.Response(503, text="cdn hiccup"),
        httpx.Response(200, content=_jpeg()),
    ]
    res = await P.FalProvider().generate(REQ)
    assert submit.call_count == 1 and dl.call_count == 2 and res.provider == "fal"


@respx.mock
async def test_download_follows_a_redirect_and_refuses_http(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "k")
    respx.post("https://fal.run/fal-ai/flux/schnell").mock(
        return_value=httpx.Response(200, json={"images": [{"url": "https://cdn.example/r.jpg"}]})
    )
    respx.get("https://cdn.example/r.jpg").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example/real.jpg"})
    )
    respx.get("https://cdn.example/real.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg())
    )
    assert (await P.FalProvider().generate(REQ)).provider == "fal"

    respx.post("https://fal.run/fal-ai/flux/schnell").mock(
        return_value=httpx.Response(200, json={"images": [{"url": "http://cdn.example/x.jpg"}]})
    )
    with pytest.raises(P.ImageGenError, match="https"):
        await P.FalProvider().generate(REQ)


@respx.mock
async def test_fal_400_is_not_retried(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "k")
    route = respx.post("https://fal.run/fal-ai/flux/schnell").mock(
        return_value=httpx.Response(422, json={"detail": "bad image_size"})
    )
    with pytest.raises(P.ImageGenError):
        await P.FalProvider().generate(REQ)
    assert route.call_count == 1


async def test_fal_without_a_key_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "")
    with pytest.raises(P.ImageGenError):
        await P.FalProvider().generate(REQ)


# --------------------------------------------------------------------------- #
# replicate
# --------------------------------------------------------------------------- #
@respx.mock
async def test_replicate_sync_prefer_wait_and_input_shape(monkeypatch):
    monkeypatch.setattr(P.settings, "replicate_api_token", "t")
    seen = {}

    def submit(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": "p1",
                "status": "succeeded",
                "output": ["https://replicate.delivery/x.jpg"],
                "urls": {"get": "https://api.replicate.com/v1/predictions/p1"},
                "metrics": {"predict_time": 1.2},
            },
        )

    respx.post(
        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"
    ).mock(side_effect=submit)
    respx.get("https://replicate.delivery/x.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg())
    )
    res = await P.ReplicateProvider().generate(REQ)
    assert seen["headers"]["authorization"] == "Bearer t"
    assert seen["headers"]["prefer"] == "wait=60"
    inp = seen["body"]["input"]
    assert inp["aspect_ratio"] == "4:5" and inp["megapixels"] == "1"
    assert inp["output_format"] == "jpg" and inp["go_fast"] is True and inp["seed"] == 7
    assert res.job_id == "p1" and res.provider == "replicate"


@respx.mock
async def test_replicate_polls_until_terminal(monkeypatch):
    monkeypatch.setattr(P.settings, "replicate_api_token", "t")
    monkeypatch.setattr(P.ReplicateProvider, "POLL_S", 0.0)
    respx.post(
        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "p2",
                "status": "processing",
                "urls": {"get": "https://api.replicate.com/v1/predictions/p2"},
            },
        )
    )
    poll = respx.get("https://api.replicate.com/v1/predictions/p2")
    poll.side_effect = [
        httpx.Response(200, json={"id": "p2", "status": "processing"}),
        httpx.Response(
            200,
            json={
                "id": "p2",
                "status": "succeeded",
                "output": ["https://replicate.delivery/y.jpg"],
            },
        ),
    ]
    respx.get("https://replicate.delivery/y.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg())
    )
    res = await P.ReplicateProvider().generate(REQ)
    assert poll.call_count == 2 and res.job_id == "p2"


@respx.mock
async def test_replicate_poll_blip_does_not_resubmit(monkeypatch):
    monkeypatch.setattr(P.settings, "replicate_api_token", "t")
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    submit = respx.post(
        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "p9",
                "status": "processing",
                "urls": {"get": "https://api.replicate.com/v1/predictions/p9"},
            },
        )
    )
    poll = respx.get("https://api.replicate.com/v1/predictions/p9")
    poll.side_effect = [
        httpx.Response(502, text="bad gateway"),
        httpx.Response(
            200,
            json={
                "id": "p9",
                "status": "succeeded",
                "output": ["https://replicate.delivery/z.jpg"],
            },
        ),
    ]
    respx.get("https://replicate.delivery/z.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg())
    )
    res = await P.ReplicateProvider().generate(REQ)
    assert submit.call_count == 1 and poll.call_count == 2 and res.job_id == "p9"


@respx.mock
async def test_replicate_failed_prediction_raises(monkeypatch):
    monkeypatch.setattr(P.settings, "replicate_api_token", "t")
    respx.post(
        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"
    ).mock(
        return_value=httpx.Response(
            201, json={"id": "p3", "status": "failed", "error": "NSFW content detected"}
        )
    )
    with pytest.raises(P.ImageGenError, match="failed"):
        await P.ReplicateProvider().generate(REQ)


# --------------------------------------------------------------------------- #
# bfl
# --------------------------------------------------------------------------- #
@respx.mock
async def test_bfl_submits_then_polls_to_ready(monkeypatch):
    monkeypatch.setattr(P.settings, "bfl_api_key", "b")
    monkeypatch.setattr(P.BflProvider, "POLL_S", 0.0)
    seen = {}

    def submit(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"id": "g1", "polling_url": "https://api.bfl.ai/v1/get_result?id=g1"}
        )

    respx.post("https://api.bfl.ai/v1/flux-2-klein-4b").mock(side_effect=submit)
    poll = respx.get("https://api.bfl.ai/v1/get_result?id=g1")
    poll.side_effect = [
        httpx.Response(200, json={"id": "g1", "status": "Pending"}),
        httpx.Response(
            200,
            json={
                "id": "g1",
                "status": "Ready",
                "result": {"sample": "https://delivery.bfl.ai/g1.jpg", "seed": 7},
            },
        ),
    ]
    respx.get("https://delivery.bfl.ai/g1.jpg").mock(
        return_value=httpx.Response(200, content=_jpeg())
    )

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    res = await P.BflProvider().generate(REQ)
    assert seen["headers"]["x-key"] == "b"
    b = seen["body"]
    assert b["width"] % 16 == 0 and b["height"] % 16 == 0
    assert abs(b["width"] - 1080) <= 8 and abs(b["height"] - 1350) <= 8
    assert b["safety_tolerance"] == 2 and b["output_format"] == "jpeg" and b["seed"] == 7
    assert poll.call_count == 2 and res.job_id == "g1" and res.seed == 7


@respx.mock
async def test_bfl_error_status_raises(monkeypatch):
    monkeypatch.setattr(P.settings, "bfl_api_key", "b")
    import asyncio

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    respx.post("https://api.bfl.ai/v1/flux-2-klein-4b").mock(
        return_value=httpx.Response(
            200, json={"id": "g2", "polling_url": "https://api.bfl.ai/v1/get_result?id=g2"}
        )
    )
    respx.get("https://api.bfl.ai/v1/get_result?id=g2").mock(
        return_value=httpx.Response(200, json={"status": "Content Moderated"})
    )
    with pytest.raises(P.ImageGenError, match="Moderated"):
        await P.BflProvider().generate(REQ)


@respx.mock
async def test_bfl_task_not_found_is_terminal(monkeypatch):
    monkeypatch.setattr(P.settings, "bfl_api_key", "b")
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    respx.post("https://api.bfl.ai/v1/flux-2-klein-4b").mock(
        return_value=httpx.Response(
            200, json={"id": "g3", "polling_url": "https://api.bfl.ai/v1/get_result?id=g3"}
        )
    )
    poll = respx.get("https://api.bfl.ai/v1/get_result?id=g3").mock(
        return_value=httpx.Response(200, json={"status": "Task not found"})
    )
    with pytest.raises(P.ImageGenError, match="Task not found"):
        await P.BflProvider().generate(REQ)
    assert poll.call_count == 1


async def test_the_budget_bounds_a_stalled_vendor(monkeypatch):
    monkeypatch.setattr(P.settings, "fal_key", "k")
    monkeypatch.setattr(P.FalProvider, "BUDGET_S", 0.05)

    async def stall(self, req):
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(P.FalProvider, "_generate", stall)
    with pytest.raises(P.ImageGenError, match="budget"):
        await P.FalProvider().generate(REQ)


def test_cost_override_applies_to_every_vendor(monkeypatch):
    monkeypatch.setattr(P.settings, "imagegen_cost_micros", 0)
    assert P.FalProvider().cost_micros_per_image == P.DEFAULT_COST_MICROS["fal"]
    assert P.BflProvider().cost_micros_per_image == P.DEFAULT_COST_MICROS["bfl"]
    monkeypatch.setattr(P.settings, "imagegen_cost_micros", 999)
    assert P.ReplicateProvider().cost_micros_per_image == 999


def test_snap16_keeps_under_4mp():
    p = P.BflProvider.__new__(P.BflProvider)
    p.model = "flux-2-klein-4b"
    body = p._payload(ImageRequest(prompt="x", width=2160, height=2700))
    assert body["width"] * body["height"] <= 4_000_000
    assert body["width"] % 16 == 0 and body["height"] % 16 == 0


def test_registry_has_no_placeholders():
    assert set(P.REGISTRY) == {"mock", "fal", "replicate", "bfl"}
    assert P.get_provider("mock").name == "mock"
