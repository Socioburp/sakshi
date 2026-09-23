"""Photo to reel: the still, set in motion.

A reel here is not a new creative; it is the creative the owner already
approved, moving. The picture gets a slow push-in (a Ken Burns move), the
designed card fades in over it, settles to exactly 1.0 and holds, and the
whole thing runs seven seconds -- long enough to read, short enough to loop.
No text is ever rendered by the video path: the card is the compositor's
own raster, so the type, the logo and the grid-safe padding are exactly
what the still had, and the last second of the reel IS the still.

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

from PIL import Image, ImageOps

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
# How long the card takes to settle back from the push-in to 1.0, after the
# fade. It used to hold at 1.03 for the whole rest of the clip, so the
# approved still was never shown: 28px of the top and bottom -- the 15px
# signature bar among them -- were cropped for the whole video.
SETTLE_S = 1.6
# Sideways drift during the move, as a share of the spare width. It is
# spare width that drifts, so at zoom 1.0 there is none and nothing moves.
DRIFT = 0.3
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
    """Scale to fill w x h, centre-cropped, like CSS object-fit: cover. The
    picture's EXIF rotation is applied first: a phone photo used here as
    the reel's opening frame played sideways."""
    img = ImageOps.exif_transpose(img)
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _zoom_at(i: int, n: int, fps: int) -> tuple[float, float]:
    """(zoom, drift) of frame i: the push-in over the photo and the fade,
    then an eased settle back to exactly 1.0 for the rest of the hold."""
    photo_end = int(n * PHOTO_SHARE)
    fade = max(1, int(fps * FADE_S))
    hold_start = photo_end + fade
    if i < hold_start:
        t = _ease(i / max(1, hold_start - 1))
        return 1.0 + (ZOOM_PHOTO - 1.0) * t, -DRIFT + 2 * DRIFT * t
    settle = max(1, min(int(fps * SETTLE_S), n - hold_start - 1))
    t = _ease((i - hold_start) / settle)
    return ZOOM_PHOTO + (1.0 - ZOOM_PHOTO) * t, DRIFT * (1 - t)


def _window_at(
    src: Image.Image,
    box: tuple[int, int, int, int],
    zoom: float,
    drift: float,
    out_size: tuple[int, int],
) -> Image.Image:
    """The photo window of `src` (a k-times source) pushed in by `zoom`,
    drifting sideways by `drift` of the spare width, resampled with Lanczos
    to its delivery size. Only the window moves; a crop of a k-times source
    at zoom <= k is never an enlargement."""
    left, top, right, bottom = box
    bw, bh = right - left, bottom - top
    cw, ch = bw / zoom, bh / zoom
    spare_w, spare_h = bw - cw, bh - ch
    x0 = left + spare_w / 2 + drift * spare_w / 2
    y0 = top + spare_h / 2
    crop = src.crop((round(x0), round(y0), round(x0 + cw), round(y0 + ch)))
    return crop.resize(out_size, Image.LANCZOS)


def frames(
    photo: bytes, card: bytes, plan: Plan, photo_box: tuple[int, int, int, int] | None = None
):
    """Yield raw RGB frames in order. Two images live in memory, never the video.

    `photo` is the frame without its words (the compositor's own ground, or a
    photograph) and `card` the approved still; either may be supplied above
    the delivery size (the compositor's 2x rasters), and every frame is then
    a Lanczos DOWNSCALE of a crop rather than a bilinear enlargement.
    `photo_box` is the photo window on the delivery canvas: on a layout that
    shows the picture through a panel, a band or a frame only that window
    pushes in and the rest of the card stays put, so the picture does not
    jump at the cross-fade. The move settles to exactly 1.0 and holds there:
    the last second of the reel IS the approved still, signature and all.
    """
    w, h = plan.width, plan.height
    with Image.open(BytesIO(card)) as c:
        c.load()
        k = max(1, min(2, c.width // w))
        top = _cover(c.convert("RGBA"), w * k, h * k)
    with Image.open(BytesIO(photo)) as p:
        p.load()
        ground = _cover(p.convert("RGB"), w * k, h * k)
    # An opaque card (the compositor's PNG) simply replaces the ground; a
    # transparent one is laid over it.
    card_k = Image.alpha_composite(ground.convert("RGBA"), top).convert("RGB")
    box = tuple(int(v) for v in (photo_box or (0, 0, w, h)))
    box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
    box_k = tuple(v * k for v in box)
    out_size = (box[2] - box[0], box[3] - box[1])
    ground_1 = ground.resize((w, h), Image.LANCZOS) if k > 1 else ground
    card_1 = card_k.resize((w, h), Image.LANCZOS) if k > 1 else card_k
    still = card_1.tobytes()
    n = plan.frames
    photo_end = int(n * PHOTO_SHARE)
    fade = max(1, int(plan.fps * FADE_S))
    hold_start = photo_end + fade
    for i in range(n):
        z, drift = _zoom_at(i, n, plan.fps)
        if i < photo_end:
            base, mix = ground_1, 0.0
        elif i < hold_start:
            base, mix = ground_1, _ease((i - photo_end + 1) / fade)
        else:
            base, mix = card_1, 1.0
        if z <= 1.0005 and mix in (0.0, 1.0):
            yield still if mix else ground_1.tobytes()
            continue
        frame = base if mix in (0.0, 1.0) else Image.blend(ground_1, card_1, mix)
        frame = frame.copy()
        moving = _window_at(ground, box_k, z, drift, out_size)
        if mix > 0.0:
            over = _window_at(card_k, box_k, z, drift, out_size)
            moving = over if mix >= 1.0 else Image.blend(moving, over, mix)
        frame.paste(moving, (box[0], box[1]))
        yield frame.tobytes()


def render(
    photo: bytes,
    card: bytes,
    *,
    width: int = 1080,
    height: int = 1920,
    seconds: float = SECONDS,
    fps: int = FPS,
    photo_box: tuple[int, int, int, int] | None = None,
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
            for frame in frames(photo, card, plan, photo_box):
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
