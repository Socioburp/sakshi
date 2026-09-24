"""gpt-image-2 against its published contract, without money.

What is asserted is what we SEND -- the model pinned, native 4:5, "high", PNG,
and none of the parameters gpt-image-2 refuses -- and what we do with the
reply: the exact size back, and the cost measured from `usage`.
"""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from app.config import Settings
from app.creative import photoreal
from app.creative.brief import POST_SIZE, REEL_SIZE
from app.creative.imagegen import providers as P
from app.creative.imagegen.base import ImageRequest, generation_size

URL = "https://api.openai.com/v1/images/generations"


def _png(w: int, h: int) -> bytes:
    im = Image.new("RGB", (w, h), (120, 90, 60))
    px = im.load()
    for y in range(0, h, 5):
        for x in range(0, w, 7):
            px[x, y] = ((x * 3) % 255, (y * 2) % 255, (x + y) % 255)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _reply(w: int, h: int, usage: dict | None = None) -> dict:
    body = {"data": [{"b64_json": base64.b64encode(_png(w, h)).decode()}]}
    if usage is not None:
        body["usage"] = usage
    return body


@pytest.fixture
def openai(monkeypatch):
    monkeypatch.setattr(P.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(P.settings, "imagegen_openai_model", "gpt-image-2-2026-04-21")
    monkeypatch.setattr(P.settings, "imagegen_cost_micros", 0)
    return P.OpenAIProvider()


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_the_model_is_the_dated_snapshot_and_comes_from_the_environment(monkeypatch):
    assert Settings.model_fields["imagegen_openai_model"].default == "gpt-image-2-2026-04-21"
    monkeypatch.setenv("IMAGEGEN_OPENAI_MODEL", "gpt-image-2-2099-01-01")
    assert Settings().imagegen_openai_model == "gpt-image-2-2099-01-01"


def test_there_is_no_quality_setting_to_turn_down():
    assert P.OPENAI_QUALITY == "high"
    assert not [f for f in Settings.model_fields if "quality" in f or "tier" in f]


def test_the_generation_size_is_native_4_5_above_the_export_and_divisible_by_16():
    gw, gh = generation_size(POST_SIZE)
    assert (gw, gh) == (1600, 2000)
    assert gw * 5 == gh * 4 and gw % 16 == 0 and gh % 16 == 0
    assert gw >= POST_SIZE[0] and gw * gh <= P.OPENAI_EXPERIMENTAL_PIXELS
    assert P.openai_size(gw, gh) == "1600x2000"
    # a story's or reel's still: the same-ratio OVERSAMPLED frame, as a post
    # gets -- not the 1088x1920 that was trimmed 4px a side at delivery size
    assert generation_size(REEL_SIZE) == (1440, 2560)
    assert P.openai_size(1440, 2560) == "1440x2560"


def test_the_export_size_is_not_a_legal_generation_size():
    with pytest.raises(P.ImageGenError, match="multiples of 16"):
        P.openai_size(*POST_SIZE)


@pytest.mark.parametrize("bad", ["1080x1350", "1600x1600x3", "big", "1024x1280"])
def test_a_bad_imagegen_size_fails_loudly(monkeypatch, bad):
    monkeypatch.setattr(P.settings, "imagegen_size", bad)
    with pytest.raises(ValueError):
        generation_size(POST_SIZE)


def test_the_larger_size_is_flagged_experimental_not_refused(monkeypatch):
    assert 1728 * 2160 > P.OPENAI_EXPERIMENTAL_PIXELS
    assert P.openai_size(1728, 2160) == "1728x2160"


# --------------------------------------------------------------------------- #
# the wire
# --------------------------------------------------------------------------- #
@respx.mock
async def test_the_request_is_pinned_high_png_native_and_nothing_else(openai):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_reply(1600, 2000)))
    req = ImageRequest(
        prompt="a brass diya on a marble ledge", negative="text", width=1600, height=2000, seed=7
    )
    res = await openai.generate(req)

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "model": "gpt-image-2-2026-04-21",
        "prompt": sent["prompt"],
        "size": "1600x2000",
        "quality": "high",
        "output_format": "png",
        "n": 1,
    }
    # gpt-image-2 rejects background="transparent", does not allow
    # input_fidelity to be set, and PNG has no compression to set.
    for refused in ("background", "input_fidelity", "output_compression", "seed"):
        assert refused not in sent
    assert "transparent" not in json.dumps(sent).lower()
    assert res.mime == "image/png"
    with Image.open(io.BytesIO(res.data)) as im:
        assert im.size == (1600, 2000), "generated natively, not cropped from a preset"


def test_no_call_anywhere_passes_a_transparent_background():
    import pathlib

    for path in pathlib.Path(P.__file__).parent.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert '"background"' not in src.replace('("background",', ""), path.name
        assert 'background="transparent"' not in src and "'transparent'" not in src, path.name


@respx.mock
async def test_the_same_settings_for_every_slide_of_a_carousel(openai):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=_reply(1600, 2000)))
    for position in range(1, 7):
        prompt, negative = photoreal.photographic(
            "a kadai of oil on a stove", "text", position=position, slide_count=6
        )
        await openai.generate(
            ImageRequest(prompt=prompt, negative=negative, width=1600, height=2000)
        )
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert len(bodies) == 6
    assert {(b["model"], b["size"], b["quality"], b["output_format"]) for b in bodies} == {
        ("gpt-image-2-2026-04-21", "1600x2000", "high", "png")
    }


def test_the_prompt_never_names_the_destination_and_always_names_the_prohibitions():
    prompt, negative = photoreal.photographic(
        "a glass bottle of coconut oil on terracotta",
        "text, logo",
        position=2,
        slide_count=6,
        palette={"primary": "#123b2e", "accent": "#E4572E"},
    )
    sent = P.openai_prompt(prompt, negative).lower()
    for cue in ("carousel", "slide ", "social", "instagram", " post"):
        assert cue not in sent.replace("slide numbers", ""), cue
    for banned in ("no text", "pagination dots", "page numbers", "phone or device frames",
                   "borders", "watermarks", "user-interface"):  # fmt: skip
        assert banned in sent, banned
    assert "#123b2e" in sent and "#e4572e" in sent, "exact brand hex reaches the image model"
    assert "pagination dots" in negative and "phone frame" in negative


def test_flux_still_hears_it_as_a_positive():
    _, negative = photoreal.photographic("a plate of biryani", "text")
    p = P.flux_prompt("a plate of biryani", negative)
    assert "full-bleed photograph running edge to edge" in p
    assert " no " not in f" {p.lower()} "


def test_palette_clause_takes_only_real_hex_values():
    assert photoreal.palette_clause({"primary": "red", "accent": "#12"}) == ""
    assert photoreal.palette_clause(None) == ""
    clause = photoreal.palette_clause({"primary": "#123B2E", "accent": "#123b2e"})
    assert clause.count("#123B2E") == 1 and len(clause) <= photoreal.PALETTE_MAX


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #
@respx.mock
async def test_cost_is_measured_from_usage_not_assumed(openai):
    usage = {
        "input_tokens": 212,
        "output_tokens": 9610,
        "input_tokens_details": {"text_tokens": 212, "image_tokens": 0},
    }
    respx.post(URL).mock(return_value=httpx.Response(200, json=_reply(1600, 2000, usage)))
    res = await openai.generate(ImageRequest(prompt="a brass diya", width=1600, height=2000))
    assert res.cost_micros == 9610 * 30 + 212 * 5 == 289_360  # $0.28936
    assert res.raw["usage"] == usage and res.raw["quality"] == "high"


@respx.mock
async def test_without_usage_the_ledger_falls_back_to_the_list_price(openai):
    respx.post(URL).mock(return_value=httpx.Response(200, json=_reply(1600, 2000)))
    res = await openai.generate(ImageRequest(prompt="a brass diya", width=1600, height=2000))
    assert res.cost_micros == P.DEFAULT_COST_MICROS["openai"] == 288_300


def test_vendor_prices_have_been_checked_recently():
    """Goes red on purpose. When it does: open each vendor's pricing page, fix
    DEFAULT_COST_MICROS / OPENAI_MICROS_PER_* if they moved, then move the date."""
    from datetime import date

    age = (date.today() - date.fromisoformat(P.PRICES_CHECKED_ON)).days
    assert 0 <= age <= P.PRICES_MAX_AGE_DAYS, (
        f"vendor prices were last checked {age} days ago ({P.PRICES_CHECKED_ON}); "
        "re-verify them and update PRICES_CHECKED_ON"
    )


@respx.mock
async def test_a_rate_limit_is_waited_out_never_answered_with_a_cheaper_call(openai, monkeypatch):
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(P.asyncio, "sleep", no_sleep)
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "12"}, json={"error": {}}),
            httpx.Response(200, json=_reply(1600, 2000)),
        ]
    )
    await openai.generate(ImageRequest(prompt="a brass diya", width=1600, height=2000))
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert first == second and second["quality"] == "high" and second["size"] == "1600x2000"
