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
MIN_BOOST = 0.75
MAX_BOOST = 1.85

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

    # Brightness is the primary term: white type on a bright band is the case
    # that actually fails. Below BRIGHT the scrim is eased off instead, which
    # is what keeps a dark, moody photograph from turning to mud.
    bright_term = (m["luminance"] - BRIGHT) / BRIGHT  # -1.0 .. +1.0
    # Busyness only ever adds. A calm bright band can be handled with a little
    # more scrim; a busy one needs more than its brightness alone suggests.
    busy_term = max(0.0, m["busyness"] - BUSY) / BUSY

    boost = 1.0 + 0.55 * bright_term + 0.35 * busy_term
    boost = round(max(MIN_BOOST, min(MAX_BOOST, boost)), 3)
    return boost, {
        "luminance": round(m["luminance"], 1),
        "busyness": round(m["busyness"], 1),
        "boost": boost,
    }
