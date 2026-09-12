"""The product lane: the owner's product is cut, gated, and stood in a studio.

The model itself is not exercised here (CI does not download 180MB); a fake
session returns masks that stand for the ways a real cut goes wrong. A test
that uses the real model runs only where the weights already exist.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.creative import product as P


def _photo(w=1200, h=1500, color=(180, 140, 100)) -> bytes:
    im = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([w * 0.3, h * 0.25, w * 0.7, h * 0.85], radius=60, fill=(220, 200, 150))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeSession:
    """Stands in for rembg: `mask_fn(size) -> L image` decides the cut."""

    def __init__(self, mask_fn):
        self.mask_fn = mask_fn


def _fake_remove(monkeypatch, mask_fn):
    import types

    def remove(data, session=None, only_mask=False, **kw):
        im = Image.open(io.BytesIO(data))
        m = mask_fn(im.size)
        buf = io.BytesIO()
        m.save(buf, "PNG")
        return buf.getvalue()

    fake = types.ModuleType("rembg")
    fake.remove = remove
    fake.new_session = lambda name: FakeSession(mask_fn)
    monkeypatch.setitem(__import__("sys").modules, "rembg", fake)
    monkeypatch.setattr(P, "_session", None)


def _box_mask(frac_w, frac_h, *, offset=(0.0, 0.0), soft=0):
    def fn(size):
        w, h = size
        m = Image.new("L", size, 0)
        x0 = int(w * (0.5 - frac_w / 2 + offset[0]))
        y0 = int(h * (0.5 - frac_h / 2 + offset[1]))
        ImageDraw.Draw(m).rectangle([x0, y0, x0 + int(w * frac_w), y0 + int(h * frac_h)], fill=255)
        if soft:
            from PIL import ImageFilter

            m = m.filter(ImageFilter.GaussianBlur(soft))
        return m

    return fn


def test_a_clean_product_cut_passes_the_gate(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    cut = P.cutout(_photo())
    assert cut.ok, cut.reason
    assert 0.2 < cut.coverage < 0.3 and cut.soft_share < 0.05 and cut.borders_touched == 0
    assert cut.rgba is not None and cut.rgba.mode == "RGBA"


def test_a_mask_that_kept_the_background_is_refused(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.98, 0.98))
    cut = P.cutout(_photo())
    assert not cut.ok and "background" in cut.reason


def test_a_tiny_or_missing_product_is_refused(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.1, 0.1))
    cut = P.cutout(_photo())
    assert not cut.ok and "small" in cut.reason


def test_a_product_running_off_two_edges_is_refused(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.6, 0.6, offset=(0.3, 0.3)))
    cut = P.cutout(_photo())
    assert not cut.ok and "edge" in cut.reason


def test_a_product_standing_on_the_bottom_edge_is_fine(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.4, 0.6, offset=(0.0, 0.2)))
    cut = P.cutout(_photo())
    assert cut.ok and cut.borders_touched == 1


def test_a_fuzzy_mask_is_refused_not_improved(monkeypatch):
    """Glass, hair, motion blur: a mask that is mostly guess ships the photo whole."""
    _fake_remove(monkeypatch, _box_mask(0.25, 0.25, soft=60))
    cut = P.cutout(_photo())
    assert not cut.ok and "uncertain" in cut.reason


def test_product_background_composes_at_canvas_size(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    res = P.product_background(
        _photo(),
        1080,
        1350,
        palette={"primary": "#175B3D", "secondary": "#F6F1E8"},
        template="lower_third",
    )
    assert res is not None
    data, metrics = res
    im = Image.open(io.BytesIO(data))
    assert im.size == (1080, 1350) and metrics["ok"]
    # The product sits in the upper zone, clear of the lower-third type.
    top, bottom, _ = P.PLACEMENT["lower_third"]
    arr = im.convert("L").load()
    # Backdrop pixels in the type zone are light paper; a product there would be darker.
    zone_samples = [arr[540, y] for y in range(int(1350 * (bottom + 0.05)), 1340, 40)]
    assert min(zone_samples) > 150, "the type zone must stay clear of the product"


def test_refused_cut_returns_none_so_the_photo_is_used_whole(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.98, 0.98))
    assert P.product_background(_photo(), 1080, 1350, palette={}, template="lower_third") is None


def test_lane_can_be_switched_off(monkeypatch):
    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    monkeypatch.setattr(P.settings, "cutout_enabled", False)
    assert P.product_background(_photo(), 1080, 1350, palette={}, template="lower_third") is None


def test_exif_rotation_is_honoured(monkeypatch):
    """Phones store rotation in EXIF; a landscape file that is a portrait photo."""
    _fake_remove(monkeypatch, _box_mask(0.4, 0.6))
    im = Image.open(io.BytesIO(_photo(1500, 1200)))
    buf = io.BytesIO()
    exif = im.getexif()
    exif[0x0112] = 6  # rotate 90 CW on display
    im.save(buf, "JPEG", exif=exif.tobytes())
    cut = P.cutout(buf.getvalue())
    assert cut.ok and cut.rgba.height > cut.rgba.width


def _bottle_photo(w=1600, h=1600) -> bytes:
    """A photo-like shot of a bottle: soft gradient backdrop, shaded body, cap,
    a floor shadow and sensor grain. Enough for a real matting model to find
    one clean object with no fringe -- and nothing on disk for CI to miss."""
    import numpy as np

    y = np.linspace(0, 1, h)[:, None]
    x = np.linspace(0, 1, w)[None, :]
    bg = 235 - 60 * y - 15 * x  # paper sweep, darker at the floor
    arr = np.repeat(bg[:, :, None], 3, axis=2) + np.array([0, -4, -10])
    im = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    d = ImageDraw.Draw(im)
    bx0, bx1, by0, by1 = int(w * 0.36), int(w * 0.64), int(h * 0.30), int(h * 0.82)
    # floor shadow
    sh = Image.new("L", (w, h), 0)
    ImageDraw.Draw(sh).ellipse([bx0 - 40, by1 - 30, bx1 + 40, by1 + 40], fill=110)
    sh = sh.filter(ImageFilter.GaussianBlur(28))
    im.paste(Image.new("RGB", (w, h), (60, 50, 40)), mask=sh)
    d = ImageDraw.Draw(im)
    # body with a highlight stripe, neck and cap
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=70, fill=(70, 110, 60))
    d.rounded_rectangle([bx0 + 30, by0 + 40, bx0 + 70, by1 - 60], radius=20, fill=(120, 160, 105))
    nx0, nx1 = int(w * 0.45), int(w * 0.55)
    d.rectangle([nx0, int(h * 0.22), nx1, by0 + 5], fill=(60, 95, 52))
    cap = [nx0 - 12, int(h * 0.17), nx1 + 12, int(h * 0.23)]
    d.rounded_rectangle(cap, radius=12, fill=(30, 30, 30))
    noise = Image.effect_noise((w, h), 6).convert("L")
    im = Image.blend(im, Image.merge("RGB", (noise, noise, noise)), 0.04)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


@pytest.mark.skipif(
    not (Path.home() / ".rembg" / "models" / "isnet-general-use").exists(),
    reason="real model weights not present",
)
def test_real_model_cuts_a_synthetic_bottle():
    from rembg import new_session

    cut = P.cutout(_bottle_photo(), session=new_session("isnet-general-use"))
    assert cut.ok, cut.reason
    assert cut.soft_share < 0.1
    assert 0.08 < cut.coverage < 0.5
