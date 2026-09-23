"""Photo-to-reel: the encoder's output, the brief contract, and the grid guard.

The pure tests need ffmpeg (imageio-ffmpeg ships one) but no database and no
Chromium, so they run in CI. The end-to-end reel through the pipeline is with
the other Chromium/DB pipeline tests and skips without them.
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from app.creative import reel
from app.creative.brief import EXAMPLE, CreativeBrief


def _photo(w: int = 900, h: int = 1400) -> bytes:
    im = Image.new("RGB", (w, h), (120, 90, 60))
    d = ImageDraw.Draw(im)
    d.ellipse((w * 0.2, h * 0.25, w * 0.8, h * 0.6), fill=(235, 205, 120))
    b = BytesIO()
    im.save(b, "JPEG")
    return b.getvalue()


def _card(w: int, h: int, *, opaque: bool = True) -> bytes:
    im = Image.new("RGBA", (w, h), (0, 0, 0, 255) if opaque else (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((w * 0.08, h * 0.68, w * 0.92, h * 0.93), fill=(20, 60, 40, 255))
    d.rectangle((w * 0.12, h * 0.71, w * 0.85, h * 0.75), fill=(255, 255, 255, 255))
    b = BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


requires_ffmpeg = pytest.mark.skipif(not reel.available(), reason="no ffmpeg")


def test_frame_count_and_easing():
    plan = reel.Plan(width=100, height=100, fps=30, seconds=7)
    assert plan.frames == 210
    # A three-second floor even if a shorter clip is asked for.
    assert reel.Plan(100, 100, 30, 1).frames == 90
    assert reel._ease(0.0) == 0.0 and reel._ease(1.0) == 1.0
    assert reel._ease(0.5) == 0.5 and 0 < reel._ease(0.25) < 0.25


def test_cover_fills_without_distortion():
    tall = Image.new("RGB", (400, 1200))
    out = reel._cover(tall, 200, 300)
    assert out.size == (200, 300)
    wide = Image.new("RGB", (1200, 400))
    assert reel._cover(wide, 200, 300).size == (200, 300)


@requires_ffmpeg
def test_render_matches_the_instagram_reel_spec():
    photo, card = _photo(), _card(360, 640)
    mp4 = reel.render(photo, card, width=360, height=640, seconds=4, fps=24)
    assert mp4[4:8] == b"ftyp" and len(mp4) > 1000
    info = reel.probe(mp4)
    assert info["duration_s"] == 4.0, info
    assert info["width"] == 360 and info["height"] == 640
    assert info["faststart"] is True, "moov must precede mdat for Meta's fetcher"


@requires_ffmpeg
def test_duration_is_clamped_to_the_api_window():
    photo, card = _photo(), _card(240, 426)
    long = reel.render(photo, card, width=240, height=426, seconds=999, fps=20)
    assert reel.probe(long)["duration_s"] == reel.MAX_SECONDS
    short = reel.render(photo, card, width=240, height=426, seconds=0.5, fps=20)
    assert reel.probe(short)["duration_s"] == reel.MIN_SECONDS


@requires_ffmpeg
def test_odd_dimensions_are_refused_before_ffmpeg():
    with pytest.raises(ValueError, match="even"):
        reel.render(_photo(), _card(361, 640), width=361, height=640)


def test_frames_yield_full_rgb_buffers_and_end_on_the_card():
    plan = reel.Plan(width=120, height=200, fps=24, seconds=3)
    photo = _photo(240, 400)
    card = _card(120, 200)
    buffers = list(reel.frames(photo, card, plan))
    assert len(buffers) == plan.frames
    assert all(len(buf) == 120 * 200 * 3 for buf in buffers)
    # The last frame is the card composited over the photo (not the bare photo).
    last = Image.frombytes("RGB", (120, 200), buffers[-1])
    first = Image.frombytes("RGB", (120, 200), buffers[0])
    assert last.getpixel((60, 170)) != first.getpixel((60, 170)), "the card is not visible at start"


def test_probe_reads_a_minimal_box_tree():
    plan = reel.Plan(80, 80, 12, 3)
    assert plan.frames == 36
    empty = reel.probe(b"")
    assert empty == {"duration_s": None, "width": None, "height": None, "faststart": False}


def test_reel_brief_is_always_9_16_single():
    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "format": {"type": "reel", "aspect_ratio": "1:1"}}
    )
    assert brief.is_reel()
    assert brief.format.aspect_ratio == "9:16" and brief.format.slide_count == 1
    assert brief.pixel_size() == (1080, 1920)
    assert len(brief.units()) == 1


def test_grid_guard_never_flags_a_reel():
    from app.creative import grid

    fp = grid.Fingerprint(posts=8)
    fp.aspects["4:5"] = 8
    fp.templates["lower_third"] = 8
    assert fp.enough
    still = CreativeBrief.model_validate({**EXAMPLE, "format": {"aspect_ratio": "1:1"}})
    assert grid.check(fp, still) is not None, "a 1:1 still against a 4:5 grid is a deviation"
    reel_brief = CreativeBrief.model_validate({**EXAMPLE, "format": {"type": "reel"}})
    assert grid.check(fp, reel_brief) is None, "a reel is a video, never a grid deviation"


def test_meta_builds_a_whatsapp_video_payload():
    from app.channels.base import OutboundMessage
    from app.channels.whatsapp.adapters.meta import MetaAdapter

    msg = OutboundMessage(to="9199", kind="video", video_url="https://x/r.mp4", caption="Fresh")
    payload = MetaAdapter.__new__(MetaAdapter)._build_payload(msg)
    assert payload["type"] == "video"
    assert payload["video"] == {"link": "https://x/r.mp4", "caption": "Fresh"}


async def test_send_video_goes_out_as_a_video_message(monkeypatch):
    import uuid

    from app.channels.base import SendResult
    from app.channels.whatsapp import send

    sent = []

    class FakeAdapter:
        name = "mock"

        async def send(self, msg):
            sent.append(msg)
            return SendResult(provider_message_id="m1")

    monkeypatch.setattr(send, "get_adapter", lambda *a, **k: FakeAdapter())
    monkeypatch.setattr(send, "_window_open", lambda *a, **k: True)
    monkeypatch.setattr(send, "record_outbound", lambda **k: None)
    ok = await send.send_video(
        account_id=uuid.uuid4(), session_id=None, wa_id="9199", video_url="https://x/r.mp4"
    )
    assert ok and sent[0].kind == "video" and sent[0].video_url == "https://x/r.mp4"


# --------------------------------------------------------------------------- #
# the reel shows the approved card
# --------------------------------------------------------------------------- #
def _frame(buf: bytes, w: int, h: int) -> Image.Image:
    return Image.frombytes("RGB", (w, h), buf)


def test_the_move_settles_to_exactly_the_approved_still_and_holds_there():
    """The hold sat at 1.03 for the whole clip: the still was never shown
    and its bottom 28px -- the signature bar -- were cropped throughout.
    The last second is the card, pixel for pixel."""
    plan = reel.Plan(width=120, height=200, fps=24, seconds=7)
    card = _card(120, 200)
    buffers = list(reel.frames(_photo(240, 400), card, plan))
    still = Image.open(BytesIO(card)).convert("RGB").tobytes()
    assert buffers[-1] == still and buffers[-plan.fps] == still, "held at 1.0 for the last second"
    zooms = [reel._zoom_at(i, plan.frames, plan.fps)[0] for i in range(plan.frames)]
    assert zooms[0] == 1.0 and max(zooms) == reel.ZOOM_PHOTO and zooms[-1] == 1.0
    peak = zooms.index(max(zooms))
    assert all(a <= b for a, b in zip(zooms[:peak], zooms[1 : peak + 1], strict=True)), "in"
    assert all(a >= b for a, b in zip(zooms[peak:-1], zooms[peak + 1 :], strict=True)), "settle"
    # sideways drift is spare width: none at 1.0
    assert reel._zoom_at(plan.frames - 1, plan.frames, plan.fps)[1] == 0.0
    # and the bottom rows (the signature bar's place) are the card's own
    assert (
        _frame(buffers[-1], 120, 200).crop((0, 190, 120, 200)).tobytes()
        == Image.open(BytesIO(card)).convert("RGB").crop((0, 190, 120, 200)).tobytes()
    )


def test_on_a_windowed_layout_only_the_photo_window_moves():
    """split_card, frame_card and top_band cross-faded a full-bleed photo
    into a card whose picture had another scale and position: the subject
    jumped. With the window given, everything outside it is the card,
    unmoved, in every frame."""
    plan = reel.Plan(width=120, height=200, fps=24, seconds=3)
    card = _card(120, 200)
    window = (0, 0, 120, 120)
    still = Image.open(BytesIO(card)).convert("RGB")
    ground = Image.open(BytesIO(_photo(120, 200))).convert("RGB")
    buffers = list(reel.frames(_photo(120, 200), card, plan, photo_box=window))
    photo_end = int(plan.frames * reel.PHOTO_SHARE)
    hold = photo_end + int(plan.fps * reel.FADE_S)
    below = (0, 130, 120, 200)
    for i, buf in enumerate(buffers):
        frame = _frame(buf, 120, 200)
        expect = still if i >= hold else (ground if i < photo_end else None)
        if expect is not None:
            assert frame.crop(below).tobytes() == expect.crop(below).tobytes(), i
    moved = {_frame(b, 120, 200).crop(window).tobytes() for b in buffers[:photo_end]}
    assert len(moved) > 5, "the window itself pushes in"


def test_frames_come_from_the_higher_resolution_source():
    """Handed the compositor's 2x rasters, every frame is a Lanczos
    downscale: the still is the 2x card resampled, never a bilinear
    enlargement of a 1x crop."""
    plan = reel.Plan(width=120, height=200, fps=24, seconds=3)
    card2x = _card(240, 400)
    buffers = list(reel.frames(_photo(240, 400), card2x, plan))
    want = Image.open(BytesIO(card2x)).convert("RGB").resize((120, 200), Image.LANCZOS)
    assert buffers[-1] == want.tobytes()
    assert all(len(b) == 120 * 200 * 3 for b in buffers)


def test_a_rotated_photo_plays_upright():
    im = Image.new("RGB", (400, 240), (120, 90, 60))
    ImageDraw.Draw(im).rectangle((0, 0, 400, 60), fill=(235, 205, 120))  # a bright band on top
    exif = im.getexif()
    exif[0x0112] = 6  # 90 CW on display: really a 240x400 portrait, band on the RIGHT
    buf = BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    plan = reel.Plan(width=120, height=200, fps=24, seconds=3)
    clear = _card(120, 200, opaque=False)
    first = _frame(next(iter(reel.frames(buf.getvalue(), clear, plan))), 120, 200)
    assert first.getpixel((110, 100))[0] > 200 and first.getpixel((10, 100))[0] < 150


@requires_ffmpeg
def test_render_accepts_the_window():
    mp4 = reel.render(_photo(), _card(360, 640), width=360, height=640, seconds=3, fps=12,
                      photo_box=(0, 0, 360, 400))  # fmt: skip
    assert reel.probe(mp4)["duration_s"] == 3.0
