"""The photo gate: blur, exposure and size are named before anything is built."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from app.creative import photo_quality as Q
from tests.test_product_lane import _bottle_photo


def _jpeg(im: Image.Image, quality=90) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _pastel(contrast: int) -> Image.Image:
    """A crisp pastel bottle on a pastel sweep: sharp, but low contrast."""
    w = h = 1600
    base = np.full((h, w, 3), 220, dtype=np.float32) - np.linspace(0, 12, h)[:, None, None]
    im = Image.fromarray(np.clip(base, 0, 255).astype("uint8"))
    k = 220 - contrast
    ImageDraw.Draw(im).rounded_rectangle([600, 450, 1000, 1250], radius=60, fill=(k, k + 8, k + 4))
    return im


def test_a_crisp_product_photo_passes():
    q = Q.assess(_bottle_photo())
    assert q.ok and q.verdict == "ok" and q.owner_note() == ""
    assert q.width == 1600 and q.height == 1600 and q.softness < 0.1


def test_the_same_photo_blurred_is_named():
    im = Image.open(io.BytesIO(_bottle_photo()))
    q = Q.assess(_jpeg(im.filter(ImageFilter.GaussianBlur(2))))
    assert not q.ok and q.verdict == "blurry" and "sharper" in q.owner_note()
    # A one-pixel softening (a phone's own processing) is not a retake.
    assert Q.assess(_jpeg(im.filter(ImageFilter.GaussianBlur(1)))).ok


def test_sharpness_does_not_depend_on_contrast():
    """The skincare playbook asks for pastel-on-pastel; that must not read as blur."""
    assert Q.assess(_jpeg(_pastel(30))).verdict == "ok"
    assert Q.assess(_jpeg(_pastel(30).filter(ImageFilter.GaussianBlur(2)))).verdict == "blurry"


def test_exposure_is_judged_on_percentiles_not_the_mean():
    # A product on a pure white catalogue background is a good photo.
    white = Image.new("RGB", (1600, 1600), (255, 255, 255))
    ImageDraw.Draw(white).rounded_rectangle([650, 500, 950, 1100], radius=40, fill=(40, 60, 120))
    assert Q.assess(_jpeg(white)).verdict == "ok"
    # Everything clipped, nothing in the shadows: washed out.
    blown = Image.new("RGB", (1600, 1600), (253, 253, 253))
    ImageDraw.Draw(blown).ellipse([500, 500, 1100, 1100], fill=(230, 230, 230))
    assert Q.assess(_jpeg(blown)).verdict == "blown_out"
    # A black bottle on dark slate with a highlight is a deliberate look.
    slate = Image.new("RGB", (1600, 1600), (40, 42, 45))
    ImageDraw.Draw(slate).rounded_rectangle([600, 400, 1000, 1250], radius=50, fill=(15, 15, 18))
    ImageDraw.Draw(slate).rectangle([620, 420, 660, 1200], fill=(90, 90, 95))
    assert Q.assess(_jpeg(slate)).verdict == "ok"
    # Underexposed for real: even the brightest pixels are dark.
    im = Image.open(io.BytesIO(_bottle_photo()))
    dark = Q.assess(_jpeg(Image.eval(im, lambda v: v // 7)))
    assert dark.verdict == "dark" and "window" in dark.owner_note()


def test_small_and_unreadable():
    im = Image.open(io.BytesIO(_bottle_photo()))
    assert Q.assess(_jpeg(im.resize((400, 400)))).verdict == "small"
    assert Q.assess(b"not an image").verdict == "unreadable"


def test_exif_rotation_is_honoured_in_the_reported_size():
    im = Image.open(io.BytesIO(_bottle_photo())).resize((1600, 1200))
    exif = im.getexif()
    exif[0x0112] = 6  # 90 CW on display: the photo is really portrait
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    q = Q.assess(buf.getvalue())
    assert (q.width, q.height) == (1200, 1600)


def test_as_dict_is_log_friendly():
    d = Q.assess(_bottle_photo()).as_dict()
    assert set(d) == {"ok", "verdict", "sharpness", "softness", "brightness", "width", "height"}
