"""One post size, one lossy encode, and nothing Instagram would reject or crop."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.creative import compose
from app.creative.brief import (
    EXAMPLE,
    EXAMPLE_CAROUSEL,
    IG_MAX_RATIO,
    IG_MIN_RATIO,
    POST_SIZE,
    REEL_SIZE,
    CreativeBrief,
)
from app.integrations.instagram import client as ig


def _png(size) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 90, 60)).save(buf, "PNG")
    return buf.getvalue()


def test_the_post_size_is_one_constant_and_it_is_1080_by_1350():
    assert POST_SIZE == (1080, 1350)
    assert POST_SIZE[0] / POST_SIZE[1] == IG_MIN_RATIO == 0.8


@pytest.mark.parametrize("asked", ["1:1", "4:5", "9:16"])
@pytest.mark.parametrize("kind", ["single", "carousel"])
def test_every_still_is_the_post_size_whatever_the_brief_asked_for(kind, asked):
    base = EXAMPLE if kind == "single" else EXAMPLE_CAROUSEL
    brief = CreativeBrief.model_validate(
        {**base, "format": {**base["format"], "type": kind, "aspect_ratio": asked}}
    )
    assert brief.format.aspect_ratio == "4:5"
    assert brief.pixel_size() == POST_SIZE


def test_a_reel_keeps_its_own_size():
    reel = CreativeBrief.model_validate({**EXAMPLE, "format": {"type": "reel"}})
    assert reel.pixel_size() == REEL_SIZE


def test_the_ratio_lives_on_the_brief_so_carousel_slides_cannot_differ():
    brief = CreativeBrief.model_validate(EXAMPLE_CAROUSEL)
    assert "aspect_ratio" not in type(brief.slides[0]).model_fields
    assert {brief.pixel_size() for _ in brief.units()} == {POST_SIZE}


def test_export_is_a_jpeg_of_exactly_the_post_size():
    out = compose.export_jpeg(_png(POST_SIZE), POST_SIZE)
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "JPEG" and im.size == (1080, 1350)


@pytest.mark.parametrize("wrong", [(1080, 1080), (1080, 1440), (1081, 1350), (1600, 2000)])
def test_export_refuses_any_other_size(wrong):
    with pytest.raises(ValueError, match="must be exactly"):
        compose.export_jpeg(_png(wrong), POST_SIZE)


# --------------------------------------------------------------------------- #
# the upload payload
# --------------------------------------------------------------------------- #
def test_the_post_size_is_inside_what_the_publishing_api_accepts():
    w, h = POST_SIZE
    assert IG_MIN_RATIO <= w / h <= IG_MAX_RATIO
    ig.assert_publishable([("JPEG", w, h)])
    ig.assert_publishable([("JPEG", w, h)] * 6)


@pytest.mark.parametrize(
    "size",
    [
        (1080, 1440),  # 3:4 -- posts by hand, refused by the API
        (1080, 1920),  # 9:16 still
        (2160, 1080),  # 2:1, wider than 1.91:1
    ],
)
def test_a_ratio_outside_4_5_to_1_91_fails_before_upload(size):
    with pytest.raises(ig.PublishSpecError, match="4:5"):
        ig.assert_publishable([("JPEG", *size)])


def test_a_png_fails_before_upload():
    with pytest.raises(ig.PublishSpecError, match="JPEG only"):
        ig.assert_publishable([("PNG", *POST_SIZE)])


def test_carousel_slides_of_different_sizes_fail_before_upload():
    with pytest.raises(ig.PublishSpecError, match="differ in size"):
        ig.assert_publishable([("JPEG", 1080, 1350), ("JPEG", 1080, 1080)])


def test_publish_reads_the_real_bytes_not_the_row(monkeypatch):
    from app.agent import tools

    store = {"a": compose.export_jpeg(_png(POST_SIZE), POST_SIZE), "b": _png((1080, 1080))}
    monkeypatch.setattr(tools.r2, "get", lambda key: store[key])
    assert tools._image_facts(["a"]) == [("JPEG", 1080, 1350)]
    with pytest.raises(ig.PublishSpecError):
        ig.assert_publishable(tools._image_facts(["a", "b"]))
    with pytest.raises(ValueError, match="no composed image"):
        tools._image_facts([None])
