"""How hard the scrim has to work, measured rather than guessed.

Every template ships a scrim: a multi-stop black gradient between the
photograph and the type. Its opacity was a constant, chosen once against
whatever test backgrounds were on hand. A constant is wrong in both
directions and the failure is asymmetric:

  too weak   a bright or busy photograph swallows the headline. The type is
             still there, still inside its box, still passing the autofit
             check in compose.py -- and still unreadable. This is what an
             owner means by "the text is overlapping the image": nothing
             actually overlaps, the words have simply stopped separating from
             what is behind them.
  too strong a dark photograph gets crushed into grey mud, and the creative
             looks like a stock photo with a filter on it.

So it is measured. Two numbers off the background decide it:

  luminance  how bright the band behind the type is. White type needs the
             band pushed down; there is nothing to push on a dark one.
  busyness   the standard deviation. A calm gradient at mid-grey is readable;
             the same average brightness made of hard edges and speckle is
             not, because the eye loses the letterform against the detail.

Only the band the type actually occupies is measured. Averaging the whole
frame is how a dark foreground and a blown-out sky cancel out to a
comfortable-looking middle that is wrong for both.

Pillow only -- it is already in the image path, and this runs on the
background bytes before Chromium is asked for anything.

That much is a PREDICTION, made from the photograph. The second half of this
module is the GUARANTEE, made from the rendered frame: the compositor
screenshots the finished page with the ink hidden, and `contrast_on` reads the
ground actually behind each word -- photograph, gradient, plate, panel,
whatever is there -- and holds it to WCAG 4.5:1. Where it falls short,
`plate_alpha` says exactly how strong the plate under that cluster of words
has to be, from the contrast target rather than from a cap chosen by eye.
(That half uses numpy for the cell averages; it is already a dependency of
the image path.)
"""

from __future__ import annotations

from io import BytesIO

from app.logging import get_logger

log = get_logger(__name__)

# The vertical slice of the canvas each template sets its type in, as
# (top, bottom) fractions of the height. Keep these in step with
# templates/creative/*.j2 -- a template whose copy moves and whose band does
# not is measured in the wrong place, which is worse than not measuring.
TYPE_BANDS: dict[str, tuple[float, float]] = {
    "centered_overlay": (0.28, 0.78),  # headline stack through the middle
    "lower_third": (0.55, 1.00),  # copy sits on the floor of the frame
    "poster_stack": (0.00, 0.46),  # headline top-left, CTA lower
    "split_card": (0.00, 0.58),  # type over the photo half only
    "frame_card": (0.00, 0.00),  # words live under the photo, not on it
    "top_band": (0.00, 0.00),  # solid brand band, no photograph behind
}
DEFAULT_BAND = (0.28, 0.78)

# The dial the templates read. 1.0 is the scrim as designed.
#
# The ceiling was 1.85 while the boost was applied as CSS `opacity`, which
# clamps at 1 -- so nothing above 1.0 ever did anything, and nobody saw what
# 1.85 looks like. Now that the boost is real (it multiplies the alpha of every
# gradient stop), 1.85 would turn the foot of a centred layout solid black and
# kill the photograph the owner paid for. The gradient is allowed a modest
# lift; whatever the words still need is delivered UNDER the words, by the
# measured plate, where it costs the picture least.
MIN_BOOST = 0.75
MAX_BOOST = 1.4

# Above this luminance (0-255) a white headline is in trouble.
BRIGHT = 128.0
# Measured as a high percentile, not a mean. White type fails against the
# BRIGHT parts of a band, not its average -- and the two come apart exactly
# where it matters. A pale background with hard dark detail across it (a
# sunlit shopfront through railings, a white plate on a dark cloth) averages
# down to a comfortable mid-grey while every light patch still swallows a
# letterform. Averaging made the scrim WEAKER on those, which is backwards.
BRIGHT_PERCENTILE = 0.75
# Standard deviation at which a band counts as properly busy.
BUSY = 58.0

# Sampled at 256px on the long edge, not smaller. An average survives any
# downsample, but busyness does not: at 96px a hard 7px stripe pattern
# averaged out to a spread of 15, reading as a calm background when it is the
# single hardest thing to put type over. 256 keeps the detail that matters and
# still costs well under a millisecond per slide.
_SAMPLE = 256


def _band_for(template: str | None) -> tuple[float, float]:
    return TYPE_BANDS.get((template or "").strip(), DEFAULT_BAND)


def measure(image: bytes, template: str | None = None) -> dict[str, float]:
    """Mean luminance and spread in the band this template sets type in."""
    from PIL import Image, ImageStat

    top, bottom = _band_for(template)
    with Image.open(BytesIO(image)) as im:
        grey = im.convert("L")
        w, h = grey.size
        if bottom > top:
            y0, y1 = int(h * top), max(int(h * bottom), int(h * top) + 1)
            grey = grey.crop((0, y0, w, y1))
        grey.thumbnail((_SAMPLE, _SAMPLE), Image.LANCZOS)
        stat = ImageStat.Stat(grey)
        hist = grey.histogram()
    return {
        "luminance": _percentile(hist, BRIGHT_PERCENTILE),
        "busyness": float(stat.stddev[0]),
    }


def measure_box(image: bytes, box: tuple[float, float, float, float]) -> dict[str, float]:
    """The same two numbers, for an arbitrary (l, t, r, b) box in 0..1 fractions."""
    from PIL import Image, ImageStat

    left, top, right, bottom = (min(1.0, max(0.0, float(v))) for v in box)
    with Image.open(BytesIO(image)) as im:
        grey = im.convert("L")
        w, h = grey.size
        x0, y0 = int(w * left), int(h * top)
        x1, y1 = max(int(w * right), x0 + 1), max(int(h * bottom), y0 + 1)
        grey = grey.crop((x0, y0, x1, y1))
        grey.thumbnail((_SAMPLE, _SAMPLE), Image.LANCZOS)
        stat = ImageStat.Stat(grey)
        hist = grey.histogram()
    return {
        "luminance": _percentile(hist, BRIGHT_PERCENTILE),
        "busyness": float(stat.stddev[0]),
    }


# The plate under a cluster of words. Its strength is computed from the
# contrast target, so the cap is only a backstop: at .88 even a pure white
# photograph sits at 30/255 behind white type (14:1). The old cap of .42 was
# chosen by eye and, blurred, left white-on-white at 2.78:1.
LOCAL_MAX = 0.88


def _boost_from(m: dict[str, float]) -> float:
    # Brightness is the primary term: white type on a bright band is the case
    # that actually fails. Below BRIGHT the scrim is eased off instead, which
    # is what keeps a dark, moody photograph from turning to mud.
    bright_term = (m["luminance"] - BRIGHT) / BRIGHT  # -1.0 .. +1.0
    # Busyness only ever adds. A calm bright band can be handled with a little
    # more scrim; a busy one needs more than its brightness alone suggests.
    busy_term = max(0.0, m["busyness"] - BUSY) / BUSY
    boost = 1.0 + 0.55 * bright_term + 0.35 * busy_term
    return round(max(MIN_BOOST, min(MAX_BOOST, boost)), 3)


def scrim_for_box(image: bytes, box: tuple[float, float, float, float]) -> tuple[float, dict]:
    """(gradient boost, why) for a MEASURED cluster of words.

    `box` is where the compositor actually set the words, after fitting -- not
    a band the template was assumed to use. A two-line headline and a six-line
    one in the same layout sit over different pixels, and TYPE_BANDS could not
    tell them apart. Falls back to the scrim as designed on any failure: this
    is the prediction, and the rendered frame is measured afterwards whatever
    it says.
    """
    try:
        m = measure_box(image, box)
    except Exception as exc:  # noqa: BLE001
        log.warning("legibility_measure_failed", box=list(box), error=repr(exc)[:120])
        return 1.0, {"reason": "measurement failed"}
    boost = _boost_from(m)
    return boost, {
        "luminance": round(m["luminance"], 1),
        "busyness": round(m["busyness"], 1),
        "boost": boost,
        "box": [round(float(v), 3) for v in box],
    }


def _percentile(hist: list[int], q: float) -> float:
    """The luminance below which `q` of the band's pixels fall."""
    total = sum(hist)
    if not total:
        return 0.0
    target, seen = total * q, 0
    for value, count in enumerate(hist):
        seen += count
        if seen >= target:
            return float(value)
    return 255.0


def scrim_boost(image: bytes, template: str | None = None) -> tuple[float, dict]:
    """The multiplier for the template's scrim opacity, plus what decided it.

    Returns 1.0 unchanged for templates that put no type over the photograph,
    and 1.0 on any measurement failure -- a scrim that is merely as good as
    before is an acceptable outcome; a slide that fails to render is not.
    """
    band = _band_for(template)
    if band[1] <= band[0]:
        return 1.0, {"reason": "no type over the photograph"}
    try:
        m = measure(image, template)
    except Exception as exc:  # noqa: BLE001
        log.warning("legibility_measure_failed", template=template, error=repr(exc)[:120])
        return 1.0, {"reason": "measurement failed"}

    boost = _boost_from(m)
    return boost, {
        "luminance": round(m["luminance"], 1),
        "busyness": round(m["busyness"], 1),
        "boost": boost,
    }


# --------------------------------------------------------------------------- #
# the guarantee: contrast measured on the rendered frame
# --------------------------------------------------------------------------- #
# WCAG 2.x AA for text. The subhead and the brand name are body-sized, so every
# word on the creative is held to the body figure, not the large-text one.
TEXT_CONTRAST = 4.5
# WCAG 1.4.11 for a graphical object: the logo against what it sits on.
MARK_CONTRAST = 3.0
# A brand ink that readable_on() passed at exactly 4.50:1 on its panel is read
# back off the frame through 8-bit pixels and a second implementation of the
# same formula. That round trip must never turn a pass into a refusal.
#
# The slack is for DESIGNED grounds only -- a panel, the pill -- where the
# colour passed on paper and a plate is the wrong tool (a dark blur on a flat
# brand colour). Over a photograph or a gradient the bar is held exactly: the
# gradient and the plate are ours, so a shortfall there is fixed, not excused.
# Applying it before plating shipped a brand name at 4.48:1 over a white
# photograph that one more plate round had been taking to 4.6. It is also the
# tolerance at the very end, when every plate is as strong as it goes and a
# value lands a hair under the bar through the same rounding.
#
# "Designed" is decided from the DOM -- the element sits inside a panel, a
# band, the pill, something with a solid background of its own (FIT_JS reports
# it as `solid`) -- and confirmed from the pixels (`is_flat`). The pixels
# alone could not tell a seamless grey studio backdrop from a brand panel, and
# a brand name over one shipped at 4.48:1 with no plate.
MEASURE_SLACK = 0.03
# A ground whose bright and dark ends are this close (relative luminance) is a
# flat designed colour, not a picture. One 8-bit level at mid grey is ~0.005.
FLAT_GROUND = 0.01
# Light ink fails against the BRIGHT part of its ground and dark ink against
# the dark part, so the ground is read at both ends and the worse one counts.
#
# It is read line by line, in windows, and the worst window counts. It used to
# be the 90th percentile of the whole element's box, and a tenth of the ground
# is a lot: a white band 20px tall through the first line of a 130px headline
# was 8% of the box, so white type shipped with the lower half of "Weekend"
# dissolved into it and the report saying 18.75:1. Two window shapes, because
# two things happen to type: a BLOB behind part of a line (a highlight, a
# plate on the table) is read in cells of .3em -- about two stems of bold
# display type, so a feature that fills one eats a letter -- and a BAND across
# the line (a table edge, a window bar, a stripe of sun) is read in strips
# .15em thick spanning the line, one axis at a time, so it weighs what it
# covers of every letter it crosses. A speck of a few pixels averages away in
# either, which is the one allowance a highlight gets.
GROUND_CELL_EM = 0.3
GROUND_STRIP_EM = 0.15
GROUND_WINDOW_MIN = 3
# The plate is aimed a little past the bar, so that the blur at its edge and
# the resample to the delivery size cannot leave the measured frame a hair
# under it and cost another round.
_AIM = 1.08
# Pixels that differ by more than this between the frame with and without the
# mark are the mark. Film grain and PNG rounding move a pixel by 2-3 levels.
_MARK_DIFF = 12


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance of an sRGB colour given as 0-255 channels."""
    out = []
    for channel in rgb:
        v = channel / 255
        out.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast_ratio(a: float, b: float) -> float:
    """WCAG contrast between two relative luminances."""
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _encoded(luminance: float) -> float:
    """The sRGB-encoded grey (0..1) that has this relative luminance."""
    lum = min(1.0, max(0.0, luminance))
    return lum * 12.92 if lum <= 0.0031308 else 1.055 * lum ** (1 / 2.4) - 0.055


def _decoded(value: float) -> float:
    v = min(1.0, max(0.0, value))
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance_plane(frame, box: tuple[float, float, float, float]):
    """Per-pixel relative luminance of `box` (l, t, r, b in pixels) of a PIL image."""
    import numpy as np

    left, top = max(0, int(box[0])), max(0, int(box[1]))
    right = min(frame.width, max(left + 1, int(round(box[2]))))
    bottom = min(frame.height, max(top + 1, int(round(box[3]))))
    rgb = np.asarray(frame.convert("RGB").crop((left, top, right, bottom)), dtype=np.float64) / 255
    linear = np.where(rgb <= 0.03928, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return linear @ np.array([0.2126, 0.7152, 0.0722])


def _windows(plane, height: int, width: int):
    """The mean of every `height` x `width` window of the plane, at every
    position, so that a feature is weighed whole by some window wherever it
    falls: a grid aligned to the box halved a band that straddled two of its
    cells. An integral image makes the full sweep a few milliseconds. A plane
    smaller than one window is filled out with its own edge pixels, so a thin
    feature still weighs what it covers and no more."""
    import numpy as np

    rows, cols = plane.shape
    plane = np.pad(plane, ((0, max(0, height - rows)), (0, max(0, width - cols))), mode="edge")
    integral = np.pad(plane, ((1, 0), (1, 0))).cumsum(axis=0).cumsum(axis=1)
    y0 = np.arange(plane.shape[0] - height + 1)[:, None]
    x0 = np.arange(plane.shape[1] - width + 1)[None, :]
    y1, x1 = y0 + height, x0 + width
    sums = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
    return sums / (height * width)


def windows_for(em: float) -> tuple[int, int]:
    """(cell, strip) in pixels for type set at `em` pixels (or a mark that tall)."""
    cell = max(GROUND_WINDOW_MIN, int(round(em * GROUND_CELL_EM)))
    strip = max(GROUND_WINDOW_MIN, int(round(em * GROUND_STRIP_EM)))
    return cell, strip


def ground(frame, rows: list[tuple[float, float, float, float]], em: float) -> dict[str, float]:
    """How bright and how dark the ground under this type gets, as relative
    luminance: the extremes over every cell and every strip of every line box
    in `rows`, for type (or a mark) `em` pixels tall. `frame` is the rendered
    page with the ink hidden."""
    cell, strip = windows_for(em)
    bright, dark = 0.0, 1.0
    for row in rows:
        plane = _luminance_plane(frame, row)
        for means in (
            _windows(plane, cell, cell),
            _windows(plane, strip, plane.shape[1]),
            _windows(plane, plane.shape[0], strip),
        ):
            bright, dark = max(bright, float(means.max())), min(dark, float(means.min()))
    return {"bright": bright, "dark": dark}


def contrast_on(ink: float, opacity: float, stats: dict[str, float]) -> float:
    """The worst contrast `ink` has anywhere on this ground.

    `opacity` is the ink's effective CSS opacity: a subhead at .90 is not the
    colour the stylesheet names, it is that colour mixed with whatever is
    behind it, and a pair that just clears 4.5:1 on paper renders below it.
    """
    worst = None
    for lum in (stats["bright"], stats["dark"]):
        seen = ink
        if opacity < 1:
            seen = _decoded(opacity * _encoded(ink) + (1 - opacity) * _encoded(lum))
        ratio = contrast_ratio(seen, lum)
        worst = ratio if worst is None else min(worst, ratio)
    return float(worst)


def plate_alpha(ink: float, stats: dict[str, float], current: float, target: float) -> float:
    """The opacity a plate under these words needs for `target` contrast.

    A black plate under light ink, a white one under dark ink (the caller picks
    the colour with `plate_colour`). `current` is the plate already there when
    this ground was measured: the answer is absolute, not an increment, so a
    second round tightens the first instead of stacking on it.
    """
    wanted = target * _AIM
    if ink >= 0.18:
        # Light ink: bring the BRIGHT end of the ground down to this luminance.
        limit = (ink + 0.05) / wanted - 0.05
        if limit <= 0:
            return LOCAL_MAX
        keep = min(1.0, _encoded(limit) / max(_encoded(stats["bright"]), 1e-6))
    else:
        # Dark ink: lift the DARK end of the ground up to this luminance.
        limit = (ink + 0.05) * wanted - 0.05
        have = _encoded(stats["dark"])
        keep = min(1.0, (1 - _encoded(limit)) / max(1 - have, 1e-6))
    return round(min(LOCAL_MAX, max(current, 1 - (1 - current) * keep)), 3)


def is_flat(stats: dict[str, float]) -> bool:
    """The pixels of a solid ground -- a panel, a pill -- as opposed to a
    photograph or a gradient, which vary across the box. Necessary for the
    measurement slack, not sufficient: see `designed`."""
    return stats["bright"] - stats["dark"] <= FLAT_GROUND


def designed(solid: bool, stats: dict[str, float]) -> bool:
    """A ground that gets the measurement slack: the DOM says the words sit on
    something with a solid background of its own, and the pixels agree."""
    return solid and is_flat(stats)


def bar_for(cls: str) -> float:
    return MARK_CONTRAST if cls == "logo" else TEXT_CONTRAST


def plate_colour(ink: float) -> str:
    return "#000000" if ink >= 0.18 else "#FFFFFF"


def mark_stats(with_mark, without_mark, box: tuple[float, float, float, float]) -> dict:
    """What the logo looks like ON THIS FRAME, from two screenshots of it.

    Read off the render rather than the upload, so it holds for a PNG, a JPEG,
    an SVG and a remote URL alike, and it sees the mark at the size and on the
    ground it actually ships with.

      luminance  mean relative luminance of the pixels the mark changed
      opaque     the mark is a filled rectangle -- a JPEG with its background,
                 or a PNG exported without transparency
      edge       that rectangle's own colour, for the card it is set on
    """
    import numpy as np

    left, top = max(0, int(box[0])), max(0, int(box[1]))
    right, bottom = int(round(box[2])), int(round(box[3]))
    a = np.asarray(with_mark.convert("RGB").crop((left, top, right, bottom)), dtype=np.int16)
    b = np.asarray(without_mark.convert("RGB").crop((left, top, right, bottom)), dtype=np.int16)
    mask = np.abs(a - b).max(axis=2) > _MARK_DIFF
    plane = _luminance_plane(with_mark, (left, top, right, bottom))
    if not mask.any():
        # The mark changed nothing: it IS its ground. Its luminance is the
        # ground's, the contrast comes out at 1:1, and it gets a plate.
        return {"luminance": float(plane.mean()), "opaque": False, "edge": None}
    rows, cols = np.where(mask.any(axis=1))[0], np.where(mask.any(axis=0))[0]
    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    filled = float(mask[y0:y1, x0:x1].mean())
    ring = np.concatenate([a[y0, x0:x1], a[y1 - 1, x0:x1], a[y0:y1, x0], a[y0:y1, x1 - 1]]).mean(
        axis=0
    )
    return {
        "luminance": float(plane[mask].mean()),
        "opaque": filled >= 0.97,
        "edge": "#{:02X}{:02X}{:02X}".format(*(int(round(c)) for c in ring)),
    }
