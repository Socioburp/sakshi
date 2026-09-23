"""The picture is never cropped, covered, or placed where the layout cannot show it.

Every test here pins a failure reproduced with the real compositor before it
was fixed: a bottle generated for the whole 4:5 frame and cut to a stripe by
frame_card's window, a 4:5 story picture trimmed and shipped without
oversampling. The measurements are taken on the rendered frame -- the photo
window FIT_JS reports, the pixels that land inside it -- never on the page's
own word for it.
"""

from __future__ import annotations

import io
import json
import types

import pytest
from PIL import Image, ImageDraw

from app.creative import compose
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL, POST_SIZE, REEL_SIZE, CreativeBrief
from app.creative.imagegen import base as gen

WINDOWED = ("split_card", "frame_card", "top_band")
FULL_BLEED = ("centered_overlay", "lower_third", "poster_stack")
LONG_HEADLINE = "Cold-pressed groundnut oil is back in stock from this Friday"
LONG_SUBHEAD = (
    "Small batches pressed on Tuesdays and Fridays in Indiranagar, bottled the same evening, fresh"
)


def _brand(**over):
    brand = types.SimpleNamespace(
        name="Kadamba Naturals",
        palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"},
        fonts={"heading": "Poppins", "body": "Inter"},
        logo_analysis={},
        logo_url=None,
        logo_src=None,
        template_prefs={"signature": "none"},
    )
    for k, v in over.items():
        setattr(brand, k, v)
    return brand


def _brief(template: str, kind: str = "single", *, long: bool = False, **copy) -> CreativeBrief:
    payload = json.loads(json.dumps(EXAMPLE))
    payload.update(template_id=template)
    if long:
        payload.update(headline=LONG_HEADLINE, subhead=LONG_SUBHEAD, cta="Order on WhatsApp now")
    payload.update(copy)
    payload["format"] = {"type": kind}
    return CreativeBrief.model_validate(payload)


def _bottle(size: tuple[int, int]) -> bytes:
    """A 'generated' picture whose subject spans nearly the whole height: a
    green bottle with a black cap at the top and its base near the bottom.
    Anything that crops the frame loses the cap or the base."""
    w, h = size
    im = Image.new("RGB", (w, h), (214, 196, 170))
    d = ImageDraw.Draw(im)
    body = [w * 0.36, h * 0.12, w * 0.64, h * 0.90]
    d.rounded_rectangle(body, radius=w // 20, fill=(60, 100, 60))
    cap = [w * 0.44, h * 0.05, w * 0.56, h * 0.14]
    d.rounded_rectangle(cap, radius=w // 80, fill=(30, 30, 30))
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


@pytest.fixture
async def chromium():
    try:
        await compose.get_browser()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no chromium: {exc}")
    yield
    await compose.shutdown()


def _window(report: dict, brief: CreativeBrief) -> tuple[int, int, int, int]:
    return compose.photo_window(report, *brief.pixel_size())


def _near(px, rgb, tol=28) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(px, rgb, strict=True))


def _pixels(im: Image.Image) -> list[tuple[int, int, int]]:
    raw = im.convert("RGB").tobytes()
    return [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]


# --------------------------------------------------------------------------- #
# 1. the picture is generated for the window the layout shows
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["single", "story"])
@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_the_layout_reports_the_window_it_shows_the_picture_through(chromium, template, kind):
    """FIT_JS measures the visible photo box after reflow: the whole canvas
    on a full-bleed layout, less than that under a panel, a band or inside
    a frame -- and less again when the copy grows."""
    brief = _brief(template, kind)
    w, h = brief.pixel_size()
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    box = report["photo_box"]
    assert box and 0 <= box["l"] < box["r"] <= w and 0 <= box["t"] < box["b"] <= h
    share = (box["b"] - box["t"]) / h
    if template in FULL_BLEED:
        assert _window(report, brief) == (0, 0, w, h)
    else:
        assert share < 0.75, (template, kind, share)
        longer = _brief(template, kind, long=True)
        grown = await compose.check_layout(longer, longer.units()[0], _brand())
        assert grown["photo_box"]["b"] - grown["photo_box"]["t"] < box["b"] - box["t"] - 20


def test_the_generation_frame_matches_the_window_within_two_pixels_and_every_limit():
    """Same ratio as the window as nearly as multiples of 16 allow, above the
    window, inside every vendor's limits; the exact frame wherever one exists."""
    assert gen.generation_size(POST_SIZE) == (1600, 2000)
    assert gen.generation_size(REEL_SIZE) == (1440, 2560), "9:16 exact, oversampled, no trim"
    assert gen.generation_size_for_window(POST_SIZE, POST_SIZE) == (1600, 2000)
    for window in [(892, 652), (892, 554), (1080, 842), (1080, 577), (1080, 945), (1080, 1184)]:
        gw, gh = gen.generation_size_for_window(window, POST_SIZE)
        assert gw % 16 == 0 and gh % 16 == 0
        assert gw >= window[0] * gen.OVERSAMPLE_MIN and gh >= window[1] * gen.OVERSAMPLE_MIN
        assert gen.GEN_MIN_PIXELS <= gw * gh <= gen.GEN_MAX_PIXELS
        assert gen.crop_for((gw, gh), window) <= gen.CROP_TOLERANCE, (window, (gw, gh))
    assert gen.crop_for((1600, 2000), (892, 652)) > 400, "the old full-frame picture lost 463px"


@pytest.mark.parametrize("kind", ["single", "story"])
@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_a_picture_generated_for_the_window_lands_in_it_whole(chromium, template, kind):
    """frame_card cut a bottle generated for the full 4:5 frame to a green
    stripe: cap and base gone. Generated for the measured window, the cap is
    at the top of the window and the base at its bottom, on a post and on a
    story, and the export is exactly the delivery size."""
    brief = _brief(template, kind)
    w, h = brief.pixel_size()
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    win = _window(report, brief)
    size = gen.generation_size_for_window((win[2] - win[0], win[3] - win[1]), (w, h))
    png, out = await compose.compose_with_report(
        brief, brief.units()[0], _brand(), _bottle(size), "image/png", layout=report, generated=True
    )
    assert out["photo_window"] == list(win)
    im = Image.open(io.BytesIO(png)).convert("RGB")
    assert im.size == (w, h)
    assert compose.image_size(compose.export_jpeg(png, (w, h))) == (w, h)
    if template in FULL_BLEED:
        # The window is the canvas and the frame its exact ratio: nothing to
        # crop. The pixels under a full-bleed layout's own scrim (a .76 black
        # foot on lower_third) are the layout's, so the probe stops here.
        assert win == (0, 0, w, h) and gen.crop_for(size, (w, h)) == 0.0
        return
    x = (win[0] + win[2]) // 2
    wh = win[3] - win[1]
    assert _near(im.getpixel((x, win[1] + int(wh * 0.09))), (30, 30, 30)), "the cap is visible"
    base = im.getpixel((x, win[3] - int(wh * 0.12)))
    assert base[1] > base[0] + 10 and base[1] > base[2] + 10, ("the base is visible", base)
    # The beige backdrop shows above the cap and below the base (a layout's
    # own scrim may darken it, so the hue is checked: beige is R > G > B).
    for py in (win[1] + int(wh * 0.02), win[3] - int(wh * 0.04)):
        px = im.getpixel((x, py))
        assert px[0] > px[1] > px[2] and not _near(px, (60, 100, 60), 40), ("margin", py, px)


async def test_a_generated_picture_of_another_shape_is_refused_not_cropped(chromium):
    """The old behaviour, now a refusal: a 4:5 frame handed to frame_card as
    'generated' would lose 463px of its height. PictureMismatch, never a trim;
    a 1px rounding difference is not a mismatch; an enlargement is."""
    brief = _brief("frame_card")
    with pytest.raises(compose.PictureMismatch) as err:
        await compose.compose(
            brief, brief.units()[0], _brand(), _bottle((1600, 2000)), "image/png", generated=True
        )
    assert err.value.crop > 400 and err.value.scale < 1
    fitted = compose.fit_background(_bottle((1248, 913)), 892, 652, generated=True)
    assert compose.image_size(fitted) == (892, 652)
    with pytest.raises(compose.PictureMismatch, match="scale 1.2"):
        compose.fit_background(_bottle((720, 900)), 892, 652, generated=True)


async def test_copy_that_would_shrink_the_window_the_picture_was_made_for_is_refused(
    chromium, monkeypatch
):
    """reflow() gives the panel what the words need and the picture what is
    left. When a stale window is handed in -- taller than this copy leaves --
    the render is refused as photo_window, not silently cover-cropped."""
    short = _brief("split_card")
    long = _brief("split_card", long=True)
    stale = await compose.check_layout(short, short.units()[0], _brand())
    tall = _window(stale, short)
    size = gen.generation_size_for_window((tall[2] - tall[0], tall[3] - tall[1]), POST_SIZE)
    picture = _bottle(size)
    with pytest.raises(compose.LayoutError) as err:
        await compose.compose(
            long, long.units()[0], _brand(), picture, "image/png", layout=stale, generated=True
        )
    assert err.value.violations == ["photo_window:changed"]


async def test_the_measured_type_block_becomes_the_copy_space_clause(chromium):
    """The clause names where the fitted copy really sits, as fractions of
    the photo window: lower_third's words at the foot, poster_stack's at the
    top-left; a panel layout has no words on the picture at all."""
    from app.creative import pipeline

    for template, kind in (("lower_third", "single"), ("poster_stack", "story")):
        brief = _brief(template, kind, cta="Order now")
        report = await compose.check_layout(brief, brief.units()[0], _brand())
        win = _window(report, brief)
        box = pipeline._text_box_in_window(report, win)
        assert box is not None and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1
        if template == "lower_third":
            assert box[1] > 0.5, "the words sit in the lower part of the window"
        else:
            assert box[3] < 0.6 and box[0] < 0.15, "top-left"
    brief = _brief("split_card")
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    assert pipeline._text_box_in_window(report, _window(report, brief)) is None


async def test_the_pipeline_asks_the_vendor_for_the_window_not_the_canvas(monkeypatch):
    """End to end without a database: a split_card post is generated at the
    frame that fills its measured 1080x842 window, a story at 1440x2560, a
    full-bleed post at the configured 1600x2000."""
    from app.creative import pipeline
    from tests import test_pipeline_flow as flow

    world = await flow.build_world(monkeypatch)
    try:
        flow._clean(monkeypatch)
        for template, kind, want in (
            ("split_card", "single", None),
            ("centered_overlay", "story", (1440, 2560)),
            ("lower_third", "single", (1600, 2000)),
        ):
            brief = _brief(template, kind)
            world["provider"].requests.clear()
            res = await pipeline.generate(world["ctx"], brief)
            assert res["ok"], res
            (req,) = world["provider"].requests
            # ...and the words' place on THIS layout reaches the prompt.
            if template in WINDOWED:
                assert "No words will be set on this picture" in req.prompt
            else:
                assert "Keep the band from" in req.prompt and "calm" in req.prompt
            if want is None:
                report = await compose.check_layout(brief, brief.units()[0], _brand())
                win = _window(report, brief)
                assert win == (0, 0, 1080, 842)
                want = gen.generation_size_for_window((win[2], win[3]), POST_SIZE)
            assert (req.width, req.height) == want, (template, kind)
    finally:
        await compose.shutdown()


# --------------------------------------------------------------------------- #
# 3. owner photos are never blind-cropped
# --------------------------------------------------------------------------- #
def _photo(w: int, h: int, subject, *, fmt="JPEG", orientation: int | None = None) -> bytes:
    """An owner's photo: a beige wall, a darker floor, one blue subject."""
    im = Image.new("RGB", (w, h), (200, 190, 170))
    d = ImageDraw.Draw(im)
    d.rectangle([0, h * 0.8, w, h], fill=(120, 100, 80))
    d.rounded_rectangle(subject, radius=min(30, w // 40), fill=(40, 90, 160))
    buf = io.BytesIO()
    if orientation:
        exif = im.getexif()
        exif[0x0112] = orientation
        im.save(buf, fmt, quality=92, exif=exif.tobytes())
    else:
        im.save(buf, fmt, quality=92)
    return buf.getvalue()


def _blue(png: bytes) -> tuple[int, int, int, int] | None:
    """The subject's box on the fitted picture, by colour."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    px = im.load()
    xs = [x for y in range(0, im.height, 2) for x in range(0, im.width, 2)
          if px[x, y][2] > 120 and px[x, y][0] < 80]  # fmt: skip
    ys = [y for y in range(0, im.height, 2) for x in range(0, im.width, 2)
          if px[x, y][2] > 120 and px[x, y][0] < 80]  # fmt: skip
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def test_a_landscape_photo_is_cropped_around_its_subject_not_its_centre():
    """A 4:3 shopfront with the subject on the left lost most of it to the
    blind centre crop (only x 0..314 of it survived on the post). Cropped
    around the subject box, the whole of it is inside the window with a
    margin, and the crop is still a cover crop: no letterbox, no upscale."""
    src = _photo(2400, 1800, [200, 500, 900, 1440])
    blind = _blue(compose.fit_background(src, 1080, 1350))
    assert blind is not None and blind[0] == 0 and blind[2] < 400, "the old crop cut it"
    aware = _blue(compose.fit_background(src, 1080, 1350, focus=(200, 500, 900, 1440)))
    assert aware is not None and aware[0] >= 30 and aware[2] - aware[0] > 500, aware
    # The crop over the source is 1440x1800 (scale 0.75): centred, it starts
    # at x=480; around a subject on the left it starts at 0, on the right at
    # 960 -- clamped to the picture, never off the subject.
    assert compose._focus_crop((2400, 1800), (1440, 1800), None) == (480, 0)
    assert compose._focus_crop((2400, 1800), (1440, 1800), (200, 500, 900, 1440)) == (0, 0)
    assert compose._focus_crop((2400, 1800), (1440, 1800), (1900, 500, 2300, 1440)) == (960, 0)
    assert compose._focus_crop((2400, 1800), (1440, 1800), (1000, 500, 1400, 1440)) == (480, 0)


def test_a_subject_that_cannot_fit_the_window_is_letterboxed_not_cropped():
    """A 9:16 portrait whose subject spans 1600 of 1920 rows on a 1080x1350
    post: no crop offset keeps it whole, so the photograph is contained on a
    blurred extension of itself. The subject is whole, the export exact."""
    src = _photo(1080, 1920, [300, 150, 780, 1750])
    assert compose._focus_crop((1080, 1920), (1080, 1350), (300, 150, 780, 1750)) is None
    out = compose.fit_background(src, 1080, 1350, focus=(300, 150, 780, 1750))
    assert compose.image_size(out) == (1080, 1350)
    box = _blue(out)
    assert box is not None and box[1] > 60 and box[3] < 1290, box
    assert (box[3] - box[1]) / (box[2] - box[0]) == pytest.approx(1600 / 480, rel=0.05)
    # The extension is the photo itself, blurred and darkened -- not black bars.
    im = Image.open(io.BytesIO(out)).convert("RGB")
    edge = im.getpixel((20, 300))
    assert 60 < sum(edge) / 3 < 200 and edge[0] > edge[2], edge


def test_a_photo_is_never_enlarged_past_the_cap_but_fills_a_smaller_window():
    """WhatsApp's 1280x960 would be enlarged 1.41x to cover a post: refused.
    The same photo fills frame_card's 892x652 window at 0.70x, so a windowed
    layout is the way to use it."""
    small = _photo(1280, 960, [100, 100, 500, 800])
    with pytest.raises(compose.PhotoTooSmall) as err:
        compose.fit_background(small, 1080, 1350)
    assert err.value.scale == pytest.approx(1.406, abs=0.001)
    assert compose.MAX_PHOTO_UPSCALE == 1.15
    fitted = compose.fit_background(small, 892, 652, focus=(100, 100, 500, 800))
    assert compose.image_size(fitted) == (892, 652) and _blue(fitted) is not None


def test_an_exif_rotated_jpeg_is_uprighted_before_it_is_cropped():
    """A phone stores a portrait as landscape pixels plus a rotation flag;
    used whole it shipped sideways and was cropped on the unrotated pixels."""
    rotated = _photo(2400, 1800, [200, 500, 900, 1440], orientation=6)
    with Image.open(io.BytesIO(rotated)) as raw:
        assert raw.size == (2400, 1800), "stored landscape"
    out = compose.fit_background(rotated, 1080, 1350)
    assert compose.image_size(out) == (1080, 1350)
    box = _blue(out)
    # Rotated 90 CW the subject (x 200..900, y 500..1440 of 2400x1800) becomes
    # a wide bar near the top: wider than tall, in the upper half.
    assert box is not None and box[2] - box[0] > box[3] - box[1] and box[3] < 675, box


def test_the_subject_box_comes_from_the_mask_even_when_the_cut_is_refused(monkeypatch):
    from app.creative import product
    from tests.test_product_lane import _box_mask, _fake_remove

    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    box, trusted = product.subject_box(_photo(1200, 1500, [360, 300, 840, 1200]))
    assert trusted and box is not None
    assert abs(box[0] - 360) < 20 and abs(box[2] - 840) < 20 and abs(box[1] - 300) < 20
    # Refused for touching two edges: the cut is not used, the box still is.
    _fake_remove(monkeypatch, _box_mask(0.6, 0.6, offset=(0.3, 0.3)))
    assert not product.cutout(_photo(1200, 1500, [0, 0, 10, 10])).ok
    box, trusted = product.subject_box(_photo(1200, 1500, [0, 0, 10, 10]))
    assert trusted and box is not None and box[2] == 1200
    # A mask that kept the whole backdrop says nothing about where the subject is.
    _fake_remove(monkeypatch, _box_mask(0.98, 0.98))
    box, trusted = product.subject_box(_photo(1200, 1500, [0, 0, 10, 10]))
    assert box is not None and not trusted
    monkeypatch.setattr(product.settings, "cutout_enabled", False)
    assert product.subject_box(_photo(1200, 1500, [0, 0, 10, 10])) == (None, False)


def test_a_rotated_phone_photo_is_uprighted_once_at_ingest():
    """Ingest stored the raw bytes and the unrotated size; everything after it
    read a landscape file that was a portrait photo."""
    from app.creative import photo_quality

    rotated = _photo(2400, 1800, [200, 500, 900, 1440], orientation=6)
    data, mime, size = photo_quality.upright(rotated, "image/jpeg")
    assert size == (1800, 2400) and mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (1800, 2400) and im.getexif().get(0x0112, 1) == 1
    plain = _photo(1200, 900, [100, 100, 500, 800])
    assert photo_quality.upright(plain, "image/jpeg") == (plain, "image/jpeg", (1200, 900))
    assert photo_quality.upright(b"not an image", None)[2] is None


def test_a_photo_sent_as_a_document_is_a_photo():
    """The quality note asks owners for the original 'as a document', and on
    Meta and Gupshup a document was never ingested as a photo."""
    from datetime import UTC, datetime

    from app.channels.base import InboundMessage, MediaRef
    from app.channels.whatsapp import ingest

    def msg(kind, mime):
        return InboundMessage(
            provider="meta", provider_message_id="m1", wa_id="91", kind=kind,
            timestamp=datetime.now(UTC), media=MediaRef(id="x", url=None, mime=mime),
        )  # fmt: skip

    assert ingest.is_picture(msg("document", "image/jpeg"))
    assert ingest.is_picture(msg("image", "image/jpeg"))
    assert not ingest.is_picture(msg("document", "application/pdf"))
    assert not ingest.is_picture(msg("document", None))


def test_an_explicit_reference_must_pass_the_same_floor_as_an_automatic_match():
    """reference_asset_id skipped photoref.is_usable: a referenced 640px photo
    was enlarged to the canvas and shipped soft."""
    from app.creative import pipeline

    tiny = types.SimpleNamespace(id="11111111-1111-1111-1111-111111111111", kind="product",
                                 label="coconut oil", width=640, height=480)  # fmt: skip
    big = types.SimpleNamespace(id="22222222-2222-2222-2222-222222222222", kind="product",
                                label="coconut oil", width=2400, height=1800)  # fmt: skip
    db = types.SimpleNamespace(scalars=lambda stmt: [tiny, big])
    for asset, want in ((tiny, {}), (big, {1: big.id})):
        brief = _brief("lower_third", headline="Weekend Sale")
        # No word shared with the labels, so nothing is matched automatically.
        brief.visual_direction.prompt = "a plain wooden table in soft morning light"
        brief.visual_direction.reference_asset_id = asset.id
        assert pipeline._resolve_photos(db, "b", brief, brief.units()) == want


async def _photo_world(monkeypatch, photo: bytes, kind: str):
    """The flow harness with one owner photograph resolved for slide 1."""
    from app.creative import pipeline
    from tests import test_pipeline_flow as flow

    world = await flow.build_world(monkeypatch)
    flow._clean(monkeypatch)
    asset_id = "33333333-3333-3333-3333-333333333333"
    snap = pipeline.BrandAssetSnapshot(asset_id, "assets/photo.jpg", "image/jpeg", kind, "shop")
    monkeypatch.setattr(pipeline, "_resolve_photos", lambda *a, **k: {1: asset_id})
    monkeypatch.setattr(pipeline, "_load_assets", lambda *a, **k: {asset_id: snap})
    monkeypatch.setattr(pipeline.r2, "get", lambda key: photo)
    return world


async def test_a_whole_photo_whose_subject_sits_under_the_type_moves_to_a_panel(monkeypatch):
    """A shop photo with its subject in the middle, asked for as
    centered_overlay: the words would sit on the subject and a dark plate
    with them. The slide is set on a panel layout instead -- no words on the
    picture -- decided per slide before the charge, and the subject lands
    whole inside the measured window."""
    from app.creative import pipeline
    from tests.test_product_lane import _box_mask, _fake_remove

    _fake_remove(monkeypatch, _box_mask(0.3, 0.5))
    photo = _photo(2400, 1800, [840, 450, 1560, 1350])
    world = await _photo_world(monkeypatch, photo, "shop")
    try:
        brief = _brief("centered_overlay")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"] and res["credits_charged"] == 0 and world["provider"].requests == []
        assert res["template_switches"] == {
            "1": {"from": "centered_overlay", "to": "split_card", "why": "subject_under_type"}
        }
        assert brief.template_id == "split_card"
        (row,) = world["rows"].values()
        assert row.template == "split_card" and row.imagegen_provider == "brand_asset"
        final = next(v for k, v in world["blobs"].items() if k.endswith("composed.jpg"))[0]
        report = await compose.check_layout(brief, brief.units()[0], _brand())
        win = _window(report, brief)
        box = _blue(final)
        assert box is not None and box[1] >= win[1] and box[3] <= win[3] + 2, (box, win)
        assert box[0] > win[0] + 40 and box[2] < win[2] - 40, "whole, with room either side"
    finally:
        await compose.shutdown()


async def test_a_small_photo_takes_a_smaller_window_and_a_tiny_one_is_refused(monkeypatch):
    """WhatsApp's 1280x960 cannot fill a 1080x1350 post without a 1.41x
    enlargement: the slide moves to split_card, whose 1080x842 window it
    covers at 0.88x. A 700x520 file fills no window at all and is refused
    before the charge, with the ask that gets the original."""
    from app.creative import pipeline
    from tests.test_product_lane import _box_mask, _fake_remove

    # The subject sits high in the frame, clear of lower_third's words: the
    # only reason to move this slide is its pixels.
    _fake_remove(monkeypatch, _box_mask(0.3, 0.4, offset=(0.0, -0.25)))
    world = await _photo_world(monkeypatch, _photo(1280, 960, [450, 50, 830, 430]), "shop")
    try:
        brief = _brief("lower_third", headline="Weekend Sale")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"] and res["credits_charged"] == 0, res
        assert res["template_switches"]["1"] == {
            "from": "lower_third", "to": "split_card", "why": "photo_too_small_for_window"
        }  # fmt: skip
        (row,) = world["rows"].values()
        assert row.template == "split_card" and row.status == "ready"
    finally:
        await compose.shutdown()
    world = await _photo_world(monkeypatch, _photo(700, 520, [245, 26, 455, 234]), "shop")
    try:
        res = await pipeline.generate(world["ctx"], _brief("lower_third", headline="Weekend Sale"))
        assert res["ok"] is False and res["reason"] == "photo_too_small" and res["charged"] == 0
        assert res["slides"][0]["pixels"] == "700x520" and "DOCUMENT" in res["hint"]
        assert world["rows"] == {} and world["images"] == [] and world["charged"] == 0
    finally:
        await compose.shutdown()


# --------------------------------------------------------------------------- #
# 4. the product is placed where the layout shows it, and nothing sits on it
# --------------------------------------------------------------------------- #
def _rect_hit(a, b, tol=1) -> bool:
    return a[0] < b[2] - tol and b[0] < a[2] - tol and a[1] < b[3] - tol and b[1] < a[3] - tol


@pytest.mark.parametrize("kind", ["single", "story"])
@pytest.mark.parametrize("template", sorted(compose.TEMPLATES))
async def test_the_product_stands_inside_the_window_clear_of_every_word_and_mark(
    monkeypatch, template, kind
):
    """frame_card cut 27% off the top of a tall product, top_band stood it
    under the logo and the CTA, split_card lost its top as the panel grew.
    Now the studio is built for the measured window and the product stands
    in the free rectangle: its box (a FIT_JS element) is inside the window,
    inside the safe zone, and intersects no word, no pill, no mark -- on
    every layout, post and story, with long copy on the type-over-photo
    layouts. The cut-out is stored beside the studio."""
    from app.creative import pipeline, product
    from tests.test_product_lane import _box_mask, _fake_remove, _striped_product

    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    world = await _photo_world(monkeypatch, _striped_product(), "product")
    seen: dict = {}
    real = compose.compose_with_report

    async def spy(*args, **kwargs):
        png, report = await real(*args, **kwargs)
        seen.update(report=report, subject=kwargs.get("subject"))
        return png, report

    monkeypatch.setattr(compose, "compose_with_report", spy)
    try:
        brief = _brief(template, kind, long=template in FULL_BLEED)
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"] and res["credits_charged"] == 0, res
        (row,) = world["rows"].values()
        assert row.imagegen_provider == "product_studio"
        report, subject = seen["report"], seen["subject"]
        assert report["violations"] == [] and subject is not None
        w, h = brief.pixel_size()
        win = compose.photo_window(report, w, h)
        assert win[0] <= subject[0] < subject[2] <= win[2], (subject, win)
        assert win[1] <= subject[1] < subject[3] <= win[3], (subject, win)
        pad = compose.padding_for(w, h)
        assert subject[0] >= pad["pad_x"] and subject[2] <= w - pad["pad_x"]
        assert subject[1] >= pad["pad_top"] and subject[3] <= h - pad["pad_bottom"]
        for cls, box in report["boxes"].items():
            if cls in ("subject", "rule"):
                continue
            assert not _rect_hit(subject, (box["l"], box["t"], box["r"], box["b"])), (cls, box)
        got = report["boxes"]["subject"]
        assert (got["l"], got["t"], got["r"], got["b"]) == tuple(subject)
        # and the delivered pixels agree: the product's colours are in its box
        final = next(v for k, v in world["blobs"].items() if k.endswith("composed.jpg"))[0]
        im = Image.open(io.BytesIO(final)).convert("RGB")
        assert im.size == (w, h)
        px = _pixels(im.crop(subject).resize((32, 48)))
        red = sum(1 for r, g, b in px if r > 2 * g and r > 2 * b) / len(px)
        blue = sum(1 for r, g, b in px if b > 2 * r and b > g) / len(px)
        assert red > 0.15 and blue > 0.35, (red, blue)
        stored = next(v for k, v in world["blobs"].items() if k.endswith("cutout.png"))
        assert stored[1] == "image/png"
        assert product.cutout_from_png(stored[0])[1] == "33333333-3333-3333-3333-333333333333"
        assert next(v for k, v in world["blobs"].items() if "-bg." in k)[1] == "image/png"
    finally:
        await compose.shutdown()


async def test_type_on_the_product_is_a_violation_the_compositor_refuses(chromium):
    """The subject is an element: a box under the headline is
    overlap:headline+subject, one outside the photo window is
    subject_clipped, one in the grid's trim is subject_unsafe."""
    brief = _brief("lower_third", headline="Weekend Sale", cta="Order now")
    report = await compose.check_layout(brief, brief.units()[0], _brand())
    hl = report["boxes"]["headline"]
    flat = io.BytesIO()
    Image.new("RGB", (1600, 2000), (120, 130, 110)).save(flat, "PNG")
    # Over the whole block of words: no size the fit search can shrink to
    # gets the type off the product (a 40px overlap it can, by giving way).
    under_words = (hl["l"], hl["t"] - 200, hl["r"], 1350 - 60)
    with pytest.raises(compose.LayoutError) as err:
        await compose.compose(
            brief, brief.units()[0], _brand(), flat.getvalue(), "image/png",
            layout=report, generated=True, subject=under_words,
        )  # fmt: skip
    assert "overlap:headline+subject" in err.value.violations
    assert "overlap:cta+subject" in err.value.violations
    with pytest.raises(compose.LayoutError) as err:
        await compose.compose(
            brief, brief.units()[0], _brand(), flat.getvalue(), "image/png",
            layout=report, generated=True, subject=(300, 20, 700, 400),
        )  # fmt: skip
    assert "subject_unsafe" in err.value.violations
    framed = _brief("frame_card", headline="Weekend Sale")
    report = await compose.check_layout(framed, framed.units()[0], _brand())
    win = _window(report, framed)
    size = gen.generation_size_for_window((win[2] - win[0], win[3] - win[1]), POST_SIZE)
    over_the_edge = (win[0] + 100, win[3] - 100, win[0] + 400, win[3] + 60)
    with pytest.raises(compose.LayoutError) as err:
        await compose.compose(
            framed, framed.units()[0], _brand(), _bottle(size), "image/png",
            layout=report, generated=True, subject=over_the_edge,
        )  # fmt: skip
    assert "subject_clipped" in err.value.violations
    inside = (win[0] + 100, win[1] + 100, win[0] + 400, win[3] - 60)
    png, out = await compose.compose_with_report(
        framed, framed.units()[0], _brand(), _bottle(size), "image/png",
        layout=report, generated=True, subject=inside,
    )  # fmt: skip
    assert out["violations"] == [] and "subject" in out["boxes"]


def test_a_plate_never_reaches_the_product():
    """The plate under the words feathers 10% of the width past them; it
    stops at the subject's box. It was a black slab over the jar."""
    keep = (300.0, 200.0, 700.0, 600.0)
    assert compose._kept_off([0.0, 650.0, 1080.0, 900.0], keep) == [0.0, 650.0, 1080.0, 900.0]
    assert compose._kept_off([0.0, 560.0, 1080.0, 900.0], keep) == [0.0, 600.0, 1080.0, 900.0]
    assert compose._kept_off([650.0, 100.0, 1080.0, 700.0], keep) == [700.0, 100.0, 1080.0, 700.0]
    assert compose._kept_off([0.0, 0.0, 1080.0, 250.0], keep) == [0.0, 0.0, 1080.0, 200.0]


async def test_the_free_rectangle_is_the_window_less_the_words_and_the_mark(chromium):
    from app.creative import pipeline

    cases = (("lower_third", "single"), ("top_band", "story"), ("frame_card", "single"))
    for template, kind in cases:
        brief = _brief(template, kind, cta="Order now")
        report = await compose.check_layout(brief, brief.units()[0], _brand())
        w, h = brief.pixel_size()
        win = compose.photo_window(report, w, h)
        free = pipeline._free_rect(report, win, w, h)
        ww, wh = win[2] - win[0], win[3] - win[1]
        assert 0 <= free[0] < free[2] <= ww and 0 <= free[1] < free[3] <= wh
        on_canvas = (free[0] + win[0], free[1] + win[1], free[2] + win[0], free[3] + win[1])
        for cls, box in report["boxes"].items():
            if cls != "rule":
                assert not _rect_hit(on_canvas, (box["l"], box["t"], box["r"], box["b"])), cls
        assert (free[2] - free[0]) * (free[3] - free[1]) > 0.25 * ww * wh


async def test_a_refused_cut_still_moves_the_words_off_the_product(monkeypatch):
    """The switch used to happen before the cut was attempted and only at
    brief level. A product on two edges keeps its photo whole -- and its
    box, from the refused mask, moves the words to a panel."""
    from app.creative import pipeline
    from tests.test_product_lane import _box_mask, _fake_remove

    _fake_remove(monkeypatch, _box_mask(0.6, 0.6, offset=(0.3, 0.3)))
    world = await _photo_world(monkeypatch, _photo(1200, 1500, [600, 600, 1200, 1500]), "product")
    try:
        brief = _brief("centered_overlay")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"], res
        assert res["template_switches"]["1"]["why"] == "subject_under_type"
        assert res["template_switches"]["1"]["to"] in pipeline.PANEL_TEMPLATES
        (row,) = world["rows"].values()
        assert row.imagegen_provider == "brand_asset", "the photo whole, no studio"
        assert not any("cutout" in k for k in world["blobs"])
    finally:
        await compose.shutdown()


# --------------------------------------------------------------------------- #
# 5. a revision never reuses a picture made for another shape or layout
# --------------------------------------------------------------------------- #
def _revisable(world, monkeypatch, res: dict, brief: CreativeBrief):
    """Make the creative `generate` just made revisable in the faked world:
    the stored brief row, its creative rows, and the blobs it uploaded."""
    import uuid

    from app.creative import pipeline

    brief_id = uuid.UUID(res["brief_id"])
    rows = [r for r in world["rows"].values() if getattr(r, "brief_id", None) == brief_id]
    world["rows"][brief_id] = types.SimpleNamespace(
        id=brief_id, payload=brief.model_dump(mode="json")
    )
    monkeypatch.setattr(
        pipeline.repo, "creatives_for_brief", lambda db, bid: rows if bid == brief_id else []
    )
    fallback = pipeline.r2.get
    monkeypatch.setattr(
        pipeline.r2,
        "get",
        lambda key: world["blobs"][key][0] if key in world["blobs"] else fallback(key),
    )
    return brief_id


async def test_a_post_is_never_cropped_into_a_story_by_a_revision(monkeypatch):
    """revise_creative could change the format; the 4:5 background was then
    cover-cropped to 9:16 (30% of its width gone) and enlarged 1.42x."""
    from app.creative import pipeline
    from tests import test_pipeline_flow as flow

    world = await flow.build_world(monkeypatch)
    try:
        flow._clean(monkeypatch)
        brief = _brief("lower_third")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"]
        brief_id = _revisable(world, monkeypatch, res, brief)
        out = await pipeline.recompose(
            world["ctx"], brief_id=brief_id, changes={"format": {"type": "story"}}
        )
        assert out["ok"] is False and out["reason"] == "format_change_needs_new_picture"
        assert out["charged"] == 0 and "create_creative" in out["hint"]
        assert len(world["images"]) == 1, "nothing new was sent"
    finally:
        await compose.shutdown()


async def test_a_layout_change_rebuilds_the_studio_from_the_stored_cutout(monkeypatch):
    """The product-studio background was baked for the OLD layout's window;
    revised to frame_card it lost the top of the product. The cut-out is
    stored beside it, so the product is stood in the new window for free."""
    from app.creative import pipeline
    from tests.test_product_lane import _box_mask, _fake_remove, _striped_product

    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    world = await _photo_world(monkeypatch, _striped_product(), "product")
    try:
        brief = _brief("lower_third")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"]
        brief_id = _revisable(world, monkeypatch, res, brief)
        (old_row,) = [r for r in world["rows"].values() if getattr(r, "brief_id", None) == brief_id]
        out = await pipeline.recompose(
            world["ctx"], brief_id=brief_id, changes={"template_id": "frame_card"}
        )
        assert out["ok"] and out["credits_charged"] == 0, out
        new_row = world["rows"][uuid_of(out["creative_ids"][0])]
        assert new_row.template == "frame_card"
        assert new_row.background_key != old_row.background_key, "a studio for the new window"
        assert any(k.endswith("cutout.png") and uuid_of(out["creative_ids"][0]).hex[:8] in k
                   for k in world["blobs"]), "the cut-out travels with the revision"  # fmt: skip
        final = world["blobs"][new_row.composed_key][0]
        framed = _brief("frame_card")
        report = await compose.check_layout(framed, framed.units()[0], _brand())
        win = _window(report, framed)
        px = _pixels(Image.open(io.BytesIO(final)).convert("RGB").crop(win).resize((64, 48)))
        assert sum(1 for r, g, b in px if b > 2 * r and b > g) / len(px) > 0.1, "inside the frame"
        below = Image.open(io.BytesIO(final)).convert("RGB").crop((0, win[3] + 10, 1080, 1350))
        outside = _pixels(below.resize((64, 32)))
        assert not any(b > 2 * r and b > g for r, g, b in outside), "nothing of it below the frame"
    finally:
        await compose.shutdown()


def uuid_of(text: str):
    import uuid

    return uuid.UUID(text)


async def test_a_generated_picture_is_not_reused_under_a_layout_that_cuts_or_covers_it(monkeypatch):
    """Revised from lower_third to split_card the full-frame picture would be
    cover-cropped to 62% of its height; to poster_stack the words would sit
    on its subject. Both are refused with the paid path named."""
    from app.creative import pipeline
    from tests import test_pipeline_flow as flow
    from tests.test_product_lane import _box_mask, _fake_remove

    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    world = await flow.build_world(monkeypatch)
    try:
        flow._clean(monkeypatch)
        brief = _brief("lower_third")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"]
        brief_id = _revisable(world, monkeypatch, res, brief)
        for to, why in (("split_card", "another window"), ("poster_stack", "under the words")):
            out = await pipeline.recompose(
                world["ctx"], brief_id=brief_id, changes={"template_id": to}
            )
            assert out["ok"] is False and out["reason"] == "picture_made_for_other_layout", (
                to,
                out,
            )
            assert why in out["slides"][0]["why"] and "regenerate_image" in out["hint"]
    finally:
        await compose.shutdown()


def _carousel(template: str, **copy) -> CreativeBrief:
    payload = json.loads(json.dumps(EXAMPLE_CAROUSEL))
    payload.update(template_id=template)
    payload.update(copy)
    return CreativeBrief.model_validate(payload)


async def test_a_kept_picture_keeps_the_lane_it_was_made_in(monkeypatch):
    """Slide 2 shows the owner's photograph. Redoing slide 1 kept slide 2's
    picture and stored its lane as "reused" -- the owner's next free copy
    change then held a 2400x1800 photograph to the GENERATED picture's
    contract, PictureMismatch refused the slide, and one refused slide
    refuses the version: nothing at all for a free, valid change."""
    from app.creative import pipeline
    from tests import test_pipeline_flow as flow
    from tests.test_product_lane import _box_mask, _fake_remove

    _fake_remove(monkeypatch, _box_mask(0.3, 0.5))
    world = await flow.build_world(monkeypatch)
    try:
        flow._clean(monkeypatch)
        asset_id = "44444444-4444-4444-4444-444444444444"
        snap = pipeline.BrandAssetSnapshot(asset_id, "assets/p.jpg", "image/jpeg", "shop", "shop")
        photo = _photo(2400, 1800, [840, 450, 1560, 1350])
        blobs = world["blobs"]
        monkeypatch.setattr(pipeline, "_resolve_photos", lambda *a, **k: {2: asset_id})
        monkeypatch.setattr(pipeline, "_load_assets", lambda *a, **k: {asset_id: snap})
        monkeypatch.setattr(pipeline.r2, "get", lambda k: blobs[k][0] if k in blobs else photo)

        brief = _carousel("lower_third")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"] and res["credits_charged"] == 2, res
        brief_id = _revisable(world, monkeypatch, res, brief)
        redo = await pipeline.regenerate_image(
            world["ctx"], brief_id=brief_id, new_prompt="a new scene", slide_position=1
        )
        assert redo["ok"], redo
        rows = {
            r.slide_position: r.imagegen_provider
            for r in world["rows"].values()
            if getattr(r, "brief_id", None) == uuid_of(redo["brief_id"])
        }
        assert rows == {1: "fake", 2: "brand_asset", 3: "fake"}, rows
        _revisable(world, monkeypatch, redo, brief)
        out = await pipeline.recompose(
            world["ctx"], brief_id=uuid_of(redo["brief_id"]), changes={"cta": "Order today"}
        )
        assert out["ok"] is True and out["credits_charged"] == 0, out
    finally:
        await compose.shutdown()


async def test_regenerating_a_slide_that_shows_the_owners_photo_says_so(monkeypatch):
    """regenerate_image re-ran the free lane and handed back the same photo,
    a revision spent on nothing."""
    from app.creative import pipeline
    from tests.test_product_lane import _box_mask, _fake_remove, _striped_product

    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    world = await _photo_world(monkeypatch, _striped_product(), "product")
    try:
        brief = _brief("lower_third")
        res = await pipeline.generate(world["ctx"], brief)
        brief_id = _revisable(world, monkeypatch, res, brief)
        out = await pipeline.regenerate_image(
            world["ctx"], brief_id=brief_id, new_prompt="a new scene"
        )
        assert out["ok"] is False and out["reason"] == "picture_is_the_owners_photo"
        assert out["slides"] == [{"slide": 1, "lane": "product_studio"}] and out["charged"] == 0
        assert world["provider"].requests == [] and len(world["images"]) == 1
    finally:
        await compose.shutdown()


# --------------------------------------------------------------------------- #
# 6. non-exact providers never crop or upscale silently
# --------------------------------------------------------------------------- #
def test_every_vendor_is_held_to_the_frame_it_was_asked_for():
    from app.creative import pipeline

    exact = types.SimpleNamespace(exact_size=True)
    loose = types.SimpleNamespace(exact_size=False)
    assert pipeline._size_fault(exact, (1600, 2000), (1600, 2000), (1080, 1350)) == ""
    assert "got 1584x2000" in pipeline._size_fault(exact, (1584, 2000), (1600, 2000), None)
    # the 896x1088 that replicate rendered for "4:5": 2.9% off, silently cropped
    assert "ratio" in pipeline._size_fault(loose, (896, 1088), (1600, 2000), (1080, 1350))
    assert pipeline._size_fault(loose, (1592, 1990), (1600, 2000), (1080, 1350)) == ""
    assert "below" in pipeline._size_fault(loose, (1000, 1250), (1600, 2000), (1080, 1350))
    assert pipeline.SIZE_RATIO_TOLERANCE == 0.005


async def test_a_vendor_that_picks_its_own_size_is_rejected_until_it_covers_the_window(monkeypatch):
    from app.creative import pipeline
    from tests import test_pipeline_flow as flow

    world = await flow.build_world(monkeypatch)
    try:
        flow._clean(monkeypatch)
        real = world["provider"].generate
        sizes = [(896, 1088), (1000, 1250), (1600, 2000)]

        class Loose:
            name = "loose"
            exact_size = False
            requests = world["provider"].requests

            async def generate(self, req):
                w, h = sizes.pop(0)
                res = await real(types.SimpleNamespace(width=w, height=h, prompt=req.prompt))
                return res

        monkeypatch.setattr(pipeline, "get_provider", lambda: Loose())
        res = await pipeline.generate(world["ctx"], _brief("lower_third"))
        assert res["ok"] and sizes == [], "two wrong frames rejected, the third accepted"
        (row,) = world["rows"].values()
        assert row.status == "ready"
        bg = next(v for k, v in world["blobs"].items() if "-bg." in k)[0]
        assert compose.image_size(bg) == (1600, 2000)
    finally:
        await compose.shutdown()


# --------------------------------------------------------------------------- #
# 7. the reel shows the approved card
# --------------------------------------------------------------------------- #
async def test_a_reel_is_rendered_from_the_compositors_rasters_inside_the_window(monkeypatch):
    """The reel used to be built from the raw picture and the 1x card: a
    blind cover-crop, bilinear frames, a jump at the cross-fade on a panel
    layout and a hold that never showed the still. It now gets the 2x frame
    without its words, the 2x finished frame and the photo window from the
    compositor, and the MP4 is the delivery size for the whole seven seconds."""
    from app.creative import pipeline, reel
    from tests import test_pipeline_flow as flow

    if not reel.available():
        pytest.skip("no ffmpeg")
    world = await flow.build_world(monkeypatch)
    handed: dict = {}
    real = reel.render

    def spy(photo, card, **kw):
        handed.update(photo=photo, card=card, **kw)
        return real(photo, card, **kw)

    monkeypatch.setattr(reel, "render", spy)
    try:
        flow._clean(monkeypatch)
        brief = _brief("split_card", "reel")
        res = await pipeline.generate(world["ctx"], brief)
        assert res["ok"] and res["format"] == "reel", res
        assert compose.image_size(handed["card"]) == (2160, 3840), "the 2x raster"
        assert compose.image_size(handed["photo"]) == (2160, 3840)
        report = await compose.check_layout(brief, brief.units()[0], _brand())
        assert tuple(handed["photo_box"]) == _window(report, brief)
        assert handed["photo_box"][3] < 1920, "only the panel's window moves"
        mp4 = next(v for k, v in world["blobs"].items() if k.endswith("reel.mp4"))[0]
        info = reel.probe(mp4)
        assert (info["width"], info["height"], info["duration_s"]) == (1080, 1920, 7.0)
        (row,) = world["rows"].values()
        assert row.video_key and row.status == "ready"
    finally:
        await compose.shutdown()


# --------------------------------------------------------------------------- #
# 8. the logo is never tiny, off-centre or a white rectangle
# --------------------------------------------------------------------------- #
def _jpeg_logo_on_white(w=900, h=300) -> bytes:
    """The ordinary WhatsApp logo: a JPEG on white, with an enclosed counter."""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    # four "letters" with gaps between them, the first with a counter
    for x0, x1 in ((100, 240), (275, 415), (450, 590), (625, 800)):
        d.rounded_rectangle([x0, 110, x1, 190], radius=30, fill=(24, 92, 62))
    d.ellipse([140, 130, 200, 170], fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _tall_emblem() -> bytes:
    im = Image.new("RGBA", (200, 600), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([40, 40, 160, 560], fill=(200, 30, 30, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_a_logo_on_white_is_stored_as_the_mark_alone():
    """A JPEG on white shipped as a white rectangle with a drop shadow. At
    ingest the ground is removed, the mark trimmed to its ink, the enclosed
    counter kept, and its true aspect and ink tone recorded."""
    from app.creative import logo

    png, info = logo.prepare(_jpeg_logo_on_white(), "image/jpeg")
    im = Image.open(io.BytesIO(png))
    assert im.mode == "RGBA" and info["ground_removed"] and info["trimmed"]
    assert info["has_transparency"] and 6.2 < info["aspect"] < 6.7 and info["ink_luminance"] < 0.2
    assert im.getchannel("A").getextrema() == (0, 255)
    assert im.getpixel((2, 2))[3] == 0, "the ground is gone"
    assert im.getpixel((im.width // 2, im.height // 2))[3] == 255, "the ink is not"
    margin = round(logo.TRIM_MARGIN * 700)
    assert im.getpixel((170 - 100 + margin, 150 - 110 + margin))[3] == 255, "the counter stays"
    # A transparent tall emblem: nothing to remove, trimmed to its ink.
    png, info = logo.prepare(_tall_emblem(), "image/png")
    assert not info["ground_removed"] and info["trimmed"] and 0.25 < info["aspect"] < 0.28
    # A mark on a photograph is not a uniform ground: left alone.
    busy = Image.effect_noise((300, 300), 80).convert("RGB")
    buf = io.BytesIO()
    busy.save(buf, "PNG")
    _, info = logo.prepare(buf.getvalue(), "image/png")
    assert not info["ground_removed"] and not info["trimmed"]
    assert logo.prepare(b"not an image", None) == (b"not an image", {})


def test_the_mark_is_sized_by_area_inside_the_caps_and_a_wide_mark_is_a_wordmark():
    """A 1:3 emblem was 47x140 inside a 119px box, indented 36px; an 8:1
    mark with no vision flag was sized as an emblem, 119x15."""
    assert compose.logo_box(1.0, 1080, False) == (119, 119), "an emblem keeps its cap"
    assert compose.logo_box(3.0, 1080, True) == (265, 88)
    assert compose.logo_box(8.0, 1080, True) == (324, 40), "the width cap wins"
    w, h = compose.logo_box(1 / 3, 1080, False)
    assert h == round(compose.LOGO_MAX_HEIGHT * 1080) and abs(w - h / 3) <= 1
    for aspect in (0.5, 1.0, 2.0, 4.0):
        bw, bh = compose.logo_box(aspect, 1080, aspect >= 2.2)
        assert min(bw, bh) >= compose.LOGO_MIN_SHORT * 1080 - 1, aspect
    ctx = compose._brand_context(_brand(logo_analysis={"aspect": 6.43}))
    assert ctx["logo_wordmark"] is True, "derived from the shape when vision gave none"
    ctx = compose._brand_context(_brand(logo_analysis={"aspect": 0.26, "has_wordmark": None}))
    assert ctx["logo_wordmark"] is False
    ctx = compose._brand_context(_brand(logo_analysis={"aspect": 4.0, "has_wordmark": False}))
    assert ctx["logo_wordmark"] is True, "a wide mark is sized as a wide thing"
    ctx = compose._brand_context(_brand(logo_analysis={"has_wordmark": True}))
    assert ctx["logo_wordmark"] is True and ctx["logo_aspect"] is None


@pytest.mark.parametrize("template", ["lower_third", "split_card", "top_band"])
async def test_a_tall_emblem_is_drawn_at_its_own_box_flush_with_the_margin(chromium, template):
    from app.creative import logo

    png, info = logo.prepare(_tall_emblem(), "image/png")
    brand = _brand(
        logo_src=compose.as_data_uri(png, "image/png"),
        logo_analysis={"aspect": info["aspect"], "ink_luminance": info["ink_luminance"]},
    )
    brief = _brief(template, headline="Weekend Sale")
    report = await compose.check_layout(brief, brief.units()[0], brand)
    box = report["boxes"]["logo"]
    want = compose.logo_box(info["aspect"], 1080, False)
    assert (round(box["r"] - box["l"]), round(box["b"] - box["t"])) == want
    assert report["violations"] == []
    flat = io.BytesIO()
    Image.new("RGB", (1600, 2000), (120, 130, 110)).save(flat, "PNG")
    out, rep = await compose.compose_with_report(
        brief, brief.units()[0], brand, flat.getvalue(), "image/png", layout=report
    )
    im = Image.open(io.BytesIO(out)).convert("RGB")
    lb = rep["boxes"]["logo"]
    reds = [x for x in range(int(lb["l"]) - 5, int(lb["r"]) + 5)
            if im.getpixel((x, int((lb["t"] + lb["b"]) / 2)))[0] > 150]  # fmt: skip
    assert reds and abs(min(reds) - lb["l"]) <= 3, "the ink starts where the box starts"


async def test_a_prepared_jpeg_logo_is_not_a_white_rectangle_on_the_panel(chromium):
    """split_card and frame_card set the mark on the brand's panel with no
    shadow; a JPEG on white shipped there as a white box. Prepared at
    ingest, what sits on the panel is the ink alone, and a mark that reads
    on the panel (over 3:1) gets no card."""
    from app.creative import logo

    png, info = logo.prepare(_jpeg_logo_on_white(), "image/jpeg")
    brand = _brand(
        logo_src=compose.as_data_uri(png, "image/png"),
        logo_analysis={"aspect": info["aspect"], "ink_luminance": info["ink_luminance"]},
        palette={"primary": "#F2EDE4", "accent": "#E4572E", "ink": "#1A1A1A"},
    )
    brief = _brief("split_card", headline="Weekend Sale")
    report = await compose.check_layout(brief, brief.units()[0], brand)
    win = _window(report, brief)
    size = gen.generation_size_for_window((win[2] - win[0], win[3] - win[1]), POST_SIZE)
    out, rep = await compose.compose_with_report(
        brief, brief.units()[0], brand, _bottle(size), "image/png", layout=report, generated=True
    )
    assert rep["legibility"]["mark_plate"] is None, "no card was needed under it"
    assert rep["legibility"]["contrast"]["logo"] >= 3.0
    im = Image.open(io.BytesIO(out)).convert("RGB")
    lb = rep["boxes"]["logo"]
    corner = im.getpixel((int(lb["l"]) + 3, int(lb["t"]) + 3))
    assert _near(corner, (242, 237, 228), 12), ("the panel shows through the box", corner)
    ink = im.getpixel((int((lb["l"] + lb["r"]) / 2), int((lb["t"] + lb["b"]) / 2)))
    assert ink[1] > ink[0] + 20 and ink[1] < 140, ("the green word is there", ink)
    # As tall as the floor allows under the width cap: a 6.4:1 mark at 0.30W.
    tallest = min(compose.LOGO_MIN_SHORT * 1080, compose.LOGO_WIDTH["wordmark"] * 1080 / 6.43)
    assert (lb["b"] - lb["t"]) >= tallest - 1, "never tiny"
