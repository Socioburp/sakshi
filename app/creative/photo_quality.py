"""Is the owner's photo good enough to build on?

A cutout of a blurry jar is a blurry jar on a clean backdrop; nothing
downstream can put back what the phone did not capture. So the moment a photo
lands it is measured, and a poor one is named to the owner in the same reply
("thoda blurry hai, ek aur bhejo?") -- while they are still holding the
product, which is the only time a retake costs nothing.

Sharpness: the variance of the Laplacian (Pech-Pacheco et al., 2000) is the
standard cheap measure, but it scales with contrast squared, so a crisp
pastel bottle on a pastel sweep -- exactly the shot the skincare playbook
asks for -- reads as "blurry" on the raw number. What is used instead is
the ratio of that variance after a small re-blur to before it: a sharp
photo loses ~95% of its Laplacian energy to a 1.5px blur, a photo that was
already soft loses far less. The ratio is contrast-free by construction.

Exposure is judged on percentiles, not the mean: a product on a pure white
catalogue background has a bright mean and is a perfectly good photo.
Computed with Pillow + numpy; the worker needs no OpenCV.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

MEASURE_EDGE = 800  # long edge the metrics are computed at
MIN_EDGE_PX = 600  # anything smaller upsamples visibly on a 1080 canvas
REBLUR_RADIUS = 1.5

# Measured on synthetic shots at MEASURE_EDGE: crisp 0.01-0.05, a 1px blur
# 0.08, a 2px blur 0.26-0.40, 3px 0.5 -- the same for a high-contrast bottle
# and a 30-level pastel one. The cut sits above the 1px case: a false
# "blurry" costs the owner a retake they did not need.
SOFT_ABOVE = 0.20
DARK_P99_BELOW = 70.0  # even the brightest 1% of pixels are dark
BRIGHT_SHARE_ABOVE = 0.60  # most pixels clipped white ...
BRIGHT_P5_ABOVE = 200.0  # ... and nothing left in the shadows either


@dataclass(slots=True)
class PhotoQuality:
    ok: bool
    verdict: str  # ok | blurry | dark | blown_out | small | unreadable
    sharpness: float  # Laplacian variance (informational)
    softness: float  # re-blur ratio; the number the verdict is made on
    brightness: float  # mean luminance
    width: int
    height: int

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "sharpness": round(self.sharpness, 1),
            "softness": round(self.softness, 3),
            "brightness": round(self.brightness, 1),
            "width": self.width,
            "height": self.height,
        }

    def owner_note(self) -> str:
        """What the agent should tell the owner, or '' when nothing is wrong."""
        return {
            "blurry": "the photo is soft/blurry; ask for a sharper one (hold still, tap to focus)",
            "dark": "the photo is very dark; if they can, a shot near a window would look better",
            "blown_out": "the photo is washed out; if they can, one away from direct flash/sun",
            "small": "the photo is low resolution; ask them to send it as a document or "
            "retake at full size",
            "unreadable": "the file could not be read as an image; ask them to resend",
        }.get(self.verdict, "")


def _laplacian(gray: np.ndarray) -> np.ndarray:
    g = gray.astype(np.float32)
    return -4.0 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]


def assess(image_bytes: bytes) -> PhotoQuality:
    try:
        im = Image.open(io.BytesIO(image_bytes))
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        im.load()
    except Exception:  # noqa: BLE001
        return PhotoQuality(False, "unreadable", 0.0, 1.0, 0.0, 0, 0)
    scale = MEASURE_EDGE / max(w, h)
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    gray_im = im.convert("L")
    gray = np.asarray(gray_im)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return PhotoQuality(False, "small", 0.0, 1.0, float(gray.mean()), w, h)
    lap_var = float(_laplacian(gray).var())
    reblurred = np.asarray(gray_im.filter(ImageFilter.GaussianBlur(REBLUR_RADIUS)))
    softness = float(_laplacian(reblurred).var()) / max(lap_var, 1e-6) if lap_var > 0 else 1.0
    bright = float(gray.mean())
    p5, p99 = (float(x) for x in np.percentile(gray, [5, 99]))
    white_share = float((gray >= 250).mean())
    if min(w, h) < MIN_EDGE_PX:
        verdict = "small"
    elif p99 < DARK_P99_BELOW:
        verdict = "dark"
    elif white_share > BRIGHT_SHARE_ABOVE and p5 > BRIGHT_P5_ABOVE:
        verdict = "blown_out"
    elif softness > SOFT_ABOVE:
        verdict = "blurry"
    else:
        verdict = "ok"
    return PhotoQuality(verdict == "ok", verdict, lap_var, softness, bright, w, h)
