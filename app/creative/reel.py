"""Photo to reel: the still, set in motion.

A reel here is not a new creative; it is the creative the owner already
approved, moving. The photograph gets a slow push-in (a Ken Burns move),
the designed card fades in over it and holds, and the whole thing runs
seven seconds -- long enough to read, short enough to loop. No text is ever
rendered by the video path: the card is the compositor's PNG, so the type,
the logo and the grid-safe padding are exactly what the still had.

Encoded to what the Instagram Reels API accepts: MP4, H.264 high profile,
yuv420p, closed GOPs, a silent AAC track (the spec lists one; owners add
music in the app when they post by hand), and the moov atom at the front.
The binary comes from imageio-ffmpeg (a static build) or, failing that,
an ffmpeg on PATH.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from app.logging import get_logger

log = get_logger(__name__)

FPS = 30
SECONDS = 7.0
MIN_SECONDS, MAX_SECONDS = 3.0, 15.0  # the API's floor; our own ceiling
PHOTO_SHARE = 0.42  # the photograph alone, before the card comes in
FADE_S = 0.7
# The push-in crops 4% a side at its tightest -- inside every layout's
# side padding, so no word is ever clipped while the card fades in.
ZOOM_PHOTO = 1.08
ZOOM_CARD = 1.03  # where the card settles and holds
VIDEO_BITRATE = "6M"
MAX_BITRATE = "9M"


def ffmpeg_path() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - the wheel is optional; PATH is the fallback
        return shutil.which("ffmpeg")


def available() -> bool:
    return ffmpeg_path() is not None


@dataclass(frozen=True, slots=True)
class Plan:
    width: int
    height: int
    fps: int
    seconds: float

    @property
    def frames(self) -> int:
        return max(int(round(self.fps * self.seconds)), int(self.fps * MIN_SECONDS))


def _ease(t: float) -> float:
    """Smoothstep: no jolt at either end of a move."""
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale to fill w x h, centre-cropped, like CSS object-fit: cover."""
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _zoomed(base: Image.Image, zoom: float, w: int, h: int, drift: float = 0.0) -> Image.Image:
    """A crop of `base` (already w x h) scaled by `zoom`, drifting slightly
    sideways by `drift` in [-1, 1] of the spare width."""
    if zoom <= 1.0005:
        return base
    cw, ch = round(w / zoom), round(h / zoom)
    spare_w, spare_h = w - cw, h - ch
    left = round(spare_w / 2 + drift * spare_w / 2)
    top = round(spare_h / 2)
    return base.crop((left, top, left + cw, top + ch)).resize((w, h), Image.BILINEAR)


def frames(photo: bytes, card: bytes, plan: Plan):
    """Yield raw RGB frames in order. Two images live in memory, never the video.

    One continuous move: the photograph pushes in and drifts, the designed
    card (which carries the same photograph) cross-fades in at the same zoom
    so nothing jumps, then the card settles back a touch and holds.
    """
    w, h = plan.width, plan.height
    with Image.open(BytesIO(photo)) as p:
        base = _cover(p.convert("RGB"), w, h)
    with Image.open(BytesIO(card)) as c:
        top = _cover(c.convert("RGBA"), w, h)
    # An opaque card (the compositor's PNG) simply replaces the photo; a
    # transparent one is laid over it.
    card_rgb = Image.alpha_composite(base.convert("RGBA"), top).convert("RGB")
    n = plan.frames
    photo_end = int(n * PHOTO_SHARE)
    fade = max(1, int(plan.fps * FADE_S))
    hold_start = photo_end + fade
    for i in range(n):
        drift = -0.3 + 0.6 * (i / max(1, n - 1))
        if i < hold_start:
            z = 1.0 + (ZOOM_PHOTO - 1.0) * _ease(i / max(1, hold_start - 1))
        else:
            k = (i - hold_start) / max(1, n - hold_start - 1)
            z = ZOOM_PHOTO + (ZOOM_CARD - ZOOM_PHOTO) * _ease(k)
        if i < photo_end:
            frame = _zoomed(base, z, w, h, drift)
        elif i < hold_start:
            a = _ease((i - photo_end + 1) / fade)
            frame = Image.blend(_zoomed(base, z, w, h, drift), _zoomed(card_rgb, z, w, h, drift), a)
        else:
            frame = _zoomed(card_rgb, z, w, h, drift)
        yield frame.tobytes()


def render(
    photo: bytes,
    card: bytes,
    *,
    width: int = 1080,
    height: int = 1920,
    seconds: float = SECONDS,
    fps: int = FPS,
) -> bytes:
    """MP4 bytes. Frames stream to ffmpeg's stdin; nothing is held in memory."""
    exe = ffmpeg_path()
    if exe is None:
        raise RuntimeError("ffmpeg not available (pip install imageio-ffmpeg)")
    seconds = min(MAX_SECONDS, max(MIN_SECONDS, float(seconds)))
    if width % 2 or height % 2:
        raise ValueError("width and height must be even for yuv420p")
    plan = Plan(width=width, height=height, fps=fps, seconds=seconds)
    with TemporaryDirectory(prefix="reel-") as tmp:
        out = Path(tmp) / "reel.mp4"
        cmd = [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "-",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(fps * 2),
            "-keyint_min",
            str(fps * 2),
            "-sc_threshold",
            "0",
            "-flags",
            "+cgop",
            "-b:v",
            VIDEO_BITRATE,
            "-maxrate",
            MAX_BITRATE,
            "-bufsize",
            "12M",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(out),
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert proc.stdin is not None
            for frame in frames(photo, card, plan):
                proc.stdin.write(frame)
            # communicate() flushes and closes stdin, then waits for the encoder.
            _, err = proc.communicate(timeout=180)
        except Exception:
            proc.kill()
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[-400:]}")
        data = out.read_bytes()
    info = probe(data)
    log.info("reel_rendered", bytes=len(data), **info)
    return data


def probe(mp4: bytes) -> dict:
    """What the container says about itself: size, duration, faststart.

    A small MP4 box walk -- enough to assert the file is what the API wants
    without shipping ffprobe. `faststart` is true when moov precedes mdat.
    """
    order: list[str] = []
    info: dict = {"duration_s": None, "width": None, "height": None, "faststart": False}
    pos = 0
    while pos + 8 <= len(mp4):
        size, kind = struct.unpack(">I4s", mp4[pos : pos + 8])
        name = kind.decode("latin-1")
        header = 8
        if size == 1:
            size = struct.unpack(">Q", mp4[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = len(mp4) - pos
        order.append(name)
        if name == "moov":
            _walk_moov(mp4[pos + header : pos + size], info)
        pos += max(size, header)
    if "moov" in order and "mdat" in order:
        info["faststart"] = order.index("moov") < order.index("mdat")
    return info


def _walk_moov(moov: bytes, info: dict) -> None:
    pos = 0
    while pos + 8 <= len(moov):
        size, kind = struct.unpack(">I4s", moov[pos : pos + 8])
        name = kind.decode("latin-1")
        if size < 8:
            break
        body = moov[pos + 8 : pos + size]
        if name == "mvhd" and len(body) >= 20:
            version = body[0]
            if version == 1:
                timescale, duration = struct.unpack(">IQ", body[20:32])
            else:
                timescale, duration = struct.unpack(">II", body[12:20])
            if timescale:
                info["duration_s"] = round(duration / timescale, 3)
        elif name == "trak":
            _walk_trak(body, info)
        pos += size


def _walk_trak(trak: bytes, info: dict) -> None:
    pos = 0
    while pos + 8 <= len(trak):
        size, kind = struct.unpack(">I4s", trak[pos : pos + 8])
        name = kind.decode("latin-1")
        if size < 8:
            break
        body = trak[pos + 8 : pos + size]
        if name == "tkhd" and len(body) >= 84:
            version = body[0]
            off = 96 if version == 1 else 84
            if len(body) >= off:
                w16, h16 = struct.unpack(">II", body[off - 8 : off])
                w, h = w16 >> 16, h16 >> 16
                if w and h and info["width"] is None:
                    info["width"], info["height"] = w, h
        pos += size


__all__ = ["available", "ffmpeg_path", "frames", "probe", "render", "Plan"]
