"""Post or Story: the owner's choice, two buttons, each composed for its own shape.

Nothing is ever made 9:16 because the bot thought it should be, and neither
shape is a crop of the other.
"""

from __future__ import annotations

import io
import json
import types

import pytest
from PIL import Image

from app.agent import buttons, prompts, tools
from app.creative import compose, grid
from app.creative.brief import EXAMPLE, POST_SIZE, STORY_SIZE, CreativeBrief
from app.integrations.instagram import client as ig


def _brief(kind: str, **over) -> CreativeBrief:
    payload = json.loads(json.dumps(EXAMPLE))
    payload["format"] = {"type": kind}
    payload.update(over)
    return CreativeBrief.model_validate(payload)


# --------------------------------------------------------------------------- #
# the two shapes
# --------------------------------------------------------------------------- #
def test_a_post_is_4_5_and_a_story_is_9_16_and_nothing_is_9_16_by_default():
    assert CreativeBrief.model_validate(EXAMPLE).pixel_size() == POST_SIZE == (1080, 1350)
    assert _brief("single").pixel_size() == POST_SIZE
    story = _brief("story")
    assert story.is_story() and not story.is_reel() and not story.is_carousel()
    assert story.pixel_size() == STORY_SIZE == (1080, 1920)
    assert story.format.aspect_ratio == "9:16" and story.format.slide_count == 1
    # asking for 9:16 on a POST does not make a story; the type is the choice
    assert _brief("single", format={"type": "single", "aspect_ratio": "9:16"}).pixel_size() == (
        1080,
        1350,
    )


def test_a_story_is_not_judged_against_the_grid_it_never_appears_on():
    fp = grid.Fingerprint(posts=9)
    fp.templates["split_card"] = 9
    assert grid.check(fp, _brief("single", template_id="lower_third")) is not None
    assert grid.check(fp, _brief("story", template_id="lower_third")) is None


# --------------------------------------------------------------------------- #
# the choice is theirs
# --------------------------------------------------------------------------- #
async def test_the_question_is_two_buttons_and_nothing_else(monkeypatch):
    monkeypatch.setattr(tools, "_locale", lambda ctx: "kn-IN")  # the real one reads the DB
    ctx = types.SimpleNamespace(account_id=None)
    res = await tools._ask_post_or_story(ctx, {})
    assert [b["id"] for b in res["buttons"]] == ["fmt:post", "fmt:story"]
    assert [b["title"] for b in res["buttons"]] == ["Post", "Story"]
    assert '"single"' in res["options"]["fmt:post"] and '"story"' in res["options"]["fmt:story"]
    assert "ask_post_or_story" in tools._HANDLERS
    assert any(t["name"] == "ask_post_or_story" for t in tools.TOOLS)


def test_the_button_words_are_the_ones_instagram_uses_in_every_language():
    for locale in ("hi-IN", "kn-IN", "ta-IN", "te-IN", "mr-IN", "ml-IN", "en-IN"):
        titles = [b.title for b in buttons.buttons(["fmt:post", "fmt:story"], locale)]
        assert titles == ["Post", "Story"], locale


def test_the_agent_is_told_never_to_choose_for_them_and_never_to_crop_one_into_the_other():
    text = " ".join(v for v in vars(prompts).values() if isinstance(v, str)).lower()
    assert "ask_post_or_story" in text
    assert "never decide for them" in text or "never \\\ndecide for them" in text
    assert "cropped into a story" in text
    tool = next(t for t in tools.TOOLS if t["name"] == "ask_post_or_story")
    for skip in ("carousel", "reel", "revising"):
        assert skip in tool["description"], f"the tool must say not to ask for a {skip}"


# --------------------------------------------------------------------------- #
# composed for its own shape
# --------------------------------------------------------------------------- #
def test_a_full_screen_frame_keeps_clear_of_the_apps_own_chrome():
    pad = compose.padding_for(*STORY_SIZE)
    assert pad["pad_top"] >= 250 and pad["pad_bottom"] >= 340, pad
    assert pad["pad_x"] >= compose.SAFE_PAD
    post = compose.padding_for(*POST_SIZE)
    assert post["pad_top"] < 150 and post["pad_bottom"] < 150, "a post is not padded like a story"


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
        name="Kadamba Naturals", logo_url=None, logo_src=None, logo_analysis={},
        palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
        fonts={"heading": "Poppins", "body": "Inter"}, template_prefs={"signature": "none"},
    )  # fmt: skip


@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_a_story_holds_every_guarantee_inside_the_story_safe_zone(chromium, template):
    brief = _brief(
        "story",
        template_id=template,
        headline="Cold-pressed groundnut oil, back in stock this weekend",
        subhead="Small batches, pressed on Tuesdays and Fridays in Indiranagar",
    )
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    assert report["violations"] == []
    for cls, box in report["boxes"].items():
        if cls == "rule":
            continue
        assert box["t"] >= 250 - 1, (template, cls, "under the account row")
        assert box["b"] <= 1920 - 340 + 1, (template, cls, "under the reply bar")


async def test_a_story_is_rendered_at_1080_by_1920_not_cropped_from_a_post(chromium):
    from app.creative.imagegen.base import generation_size

    brief = _brief("story")
    gw, gh = generation_size(brief.pixel_size())
    assert (gw, gh) == (1440, 2560), "generated natively tall, not a 4:5 picture cropped"
    buf = io.BytesIO()
    Image.new("RGB", (gw, gh), (90, 110, 100)).save(buf, "PNG")
    png = await compose.compose(brief, brief.units()[0], _brand(), buf.getvalue(), "image/png")
    jpg = compose.export_jpeg(png, brief.pixel_size())
    with Image.open(io.BytesIO(jpg)) as im:
        assert im.format == "JPEG" and im.size == (1080, 1920)
    with pytest.raises(ValueError, match="must be exactly"):
        compose.export_jpeg(png, POST_SIZE)


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
def test_a_story_publishes_as_one_full_screen_jpeg():
    ig.assert_publishable([("JPEG", 1080, 1920)], story=True)
    with pytest.raises(ig.PublishSpecError, match="4:5"):
        ig.assert_publishable([("JPEG", 1080, 1920)]), "a 9:16 still is not a feed post"
    with pytest.raises(ig.PublishSpecError, match="1080x1920"):
        ig.assert_publishable([("JPEG", 1080, 1350)], story=True), "a post is not a story"
    with pytest.raises(ig.PublishSpecError, match="JPEG only"):
        ig.assert_publishable([("PNG", 1080, 1920)], story=True)
    with pytest.raises(ig.PublishSpecError, match="one image"):
        ig.assert_publishable([("JPEG", 1080, 1920)] * 2, story=True)


async def test_the_story_container_is_media_type_stories_with_no_caption(monkeypatch):
    import httpx
    import respx

    monkeypatch.setattr(ig.settings, "instagram_mock", False)
    with respx.mock:
        route = respx.post(f"{ig.GRAPH}/178/media").mock(
            return_value=httpx.Response(200, json={"id": "c1"})
        )
        cid = await ig.create_media_container(
            ig_user_id="178", access_token="t", image_url="https://x/y.jpg",
            caption="must not be sent", alt_text="nor this", media_type="STORIES",
        )  # fmt: skip
    sent = dict(p.split("=", 1) for p in route.calls.last.request.content.decode().split("&"))
    assert cid == "c1" and sent["media_type"] == "STORIES"
    assert "caption" not in sent and "alt_text" not in sent
