"""Fonts for the copy that is actually on the creative.

The brand's display face (Poppins by default) carries Latin and, on Google
Fonts, Devanagari -- and nothing else. A Tamil or Kannada headline set in it
falls through to whatever the render box has installed, which on a slim
container is a fallback face at best and tofu boxes at worst. So the
compositor looks at the copy, names the scripts in it, and loads a Noto Sans
face for each one alongside the brand fonts. Latin copy costs nothing extra.

Noto Sans <Script> faces are SIL Open Font License; Google Fonts serves them
with `display=block` so the render waits for the real glyphs instead of
painting a stand-in.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

# Unicode blocks -> Google Fonts family. Ordered as they appear in Unicode.
SCRIPT_BLOCKS: tuple[tuple[str, int, int], ...] = (
    ("Noto Sans Devanagari", 0x0900, 0x097F),
    ("Noto Sans Bengali", 0x0980, 0x09FF),
    ("Noto Sans Gurmukhi", 0x0A00, 0x0A7F),
    ("Noto Sans Gujarati", 0x0A80, 0x0AFF),
    ("Noto Sans Oriya", 0x0B00, 0x0B7F),
    ("Noto Sans Tamil", 0x0B80, 0x0BFF),
    ("Noto Sans Telugu", 0x0C00, 0x0C7F),
    ("Noto Sans Kannada", 0x0C80, 0x0CFF),
    ("Noto Sans Malayalam", 0x0D00, 0x0D7F),
    ("Noto Sans Sinhala", 0x0D80, 0x0DFF),
    ("Noto Sans Arabic", 0x0600, 0x06FF),  # Urdu
)

GOOGLE_CSS = "https://fonts.googleapis.com/css2"

# Vendored faces (scripts/fetch_fonts.py -> templates/fonts/). The compositor
# answers this host from disk (compose._serve_fonts); nothing leaves the box.
# `.invalid` is reserved by RFC 2606, so it can never resolve to a real server
# if the route is somehow not installed -- the render fails instead of leaking.
LOCAL_HOST = "https://fonts.sakshi.invalid"
LOCAL_DIR = Path(__file__).resolve().parents[2] / "templates" / "fonts"


@lru_cache(maxsize=1)
def vendored() -> frozenset[str]:
    """Families that are served from disk. Empty when nothing has been fetched."""
    listing = LOCAL_DIR / "families.txt"
    if not (listing.exists() and (LOCAL_DIR / "fonts.css").exists()):
        return frozenset()
    return frozenset(
        s.strip() for s in listing.read_text(encoding="utf-8").splitlines() if s.strip()
    )


def local_href() -> str | None:
    return f"{LOCAL_HOST}/fonts.css" if vendored() else None


HEADING_WEIGHTS = "600;700;800"
BODY_WEIGHTS = "400;600;700"
SCRIPT_WEIGHTS = "400;600;700;800"


def script_families(text: str) -> list[str]:
    """The Noto families the characters in `text` need, in block order."""
    found: set[str] = set()
    for ch in text or "":
        cp = ord(ch)
        if cp < 0x0600:  # Latin, punctuation, digits: the brand font has these
            continue
        for family, lo, hi in SCRIPT_BLOCKS:
            if lo <= cp <= hi:
                found.add(family)
                break
    return [f for f, _, _ in SCRIPT_BLOCKS if f in found]


def _family_param(name: str, weights: str) -> str:
    return f"family={quote(name).replace('%20', '+')}:wght@{weights}"


def google_fonts_href(heading: str, body: str, scripts: list[str] = ()) -> str:
    """One stylesheet URL for the brand faces (plus script faces if given).

    The script faces normally go in their own request (`script_fonts_href`):
    the CSS2 API answers 400 for the WHOLE request when any family in it is
    unknown, and a brand face that is not on Google Fonts must not take the
    Kannada face down with it.
    """
    params = [_family_param(heading, HEADING_WEIGHTS)]
    if body and body != heading:
        params.append(_family_param(body, BODY_WEIGHTS))
    for fam in scripts:
        params.append(_family_param(fam, SCRIPT_WEIGHTS))
    return f"{GOOGLE_CSS}?{'&'.join(params)}&display=block"


def remote_brand_href(heading: str, body: str) -> str | None:
    """Google Fonts, for brand faces that are NOT vendored -- a brand that asked
    for a face outside the four looks. None when both faces are on disk."""
    need = [f for f in dict.fromkeys([heading, body]) if f and f not in vendored()]
    if not need:
        return None
    params = [_family_param(f, HEADING_WEIGHTS if f == heading else BODY_WEIGHTS) for f in need]
    return f"{GOOGLE_CSS}?{'&'.join(params)}&display=block"


def script_fonts_href(scripts: list[str]) -> str | None:
    """A stylesheet URL for the script faces alone, or None for Latin copy."""
    scripts = [s for s in scripts if s not in vendored()]
    if not scripts:
        return None
    params = [_family_param(fam, SCRIPT_WEIGHTS) for fam in scripts]
    return f"{GOOGLE_CSS}?{'&'.join(params)}&display=block"


# Leading by script, because "Indic" is not one shape. Devanagari, Bengali,
# Gujarati, Gurmukhi and Odia hang matras and stacked conjuncts below the line
# and carry reph and vowel signs above it: at the 1.14 every script used to
# share, a two-line Hindi headline in Poppins measured -20px between the ink of
# its lines. The southern scripts are rounder and mostly stay between their
# lines; Arabic-script Urdu dips deep below. Latin display type stays tight --
# that is what makes it a headline. These are starting points, not the
# guarantee: FIT_JS measures the real ink and opens a pair of lines further if
# they would still touch (`linegap`).
_TALL_SCRIPTS = frozenset(
    {
        "Noto Sans Devanagari",
        "Noto Sans Bengali",
        "Noto Sans Gurmukhi",
        "Noto Sans Gujarati",
        "Noto Sans Oriya",
        "Noto Sans Arabic",
    }
)
LATIN_DISPLAY_LEADING, ROUND_DISPLAY_LEADING, TALL_DISPLAY_LEADING = ".98", "1.26", "1.38"
LATIN_BODY_LEADING, ROUND_BODY_LEADING, TALL_BODY_LEADING = "1.34", "1.5", "1.6"


def display_leading(scripts: list[str]) -> str:
    """The headline's line-height for the scripts its copy is written in."""
    if not scripts:
        return LATIN_DISPLAY_LEADING
    return TALL_DISPLAY_LEADING if _TALL_SCRIPTS & set(scripts) else ROUND_DISPLAY_LEADING


def body_leading(scripts: list[str]) -> str:
    """The subhead's line-height, by the same families."""
    if not scripts:
        return LATIN_BODY_LEADING
    return TALL_BODY_LEADING if _TALL_SCRIPTS & set(scripts) else ROUND_BODY_LEADING


def text_direction(text: str) -> str:
    """'rtl' when the first strong character reads right to left (Urdu), else
    'ltr'. The first strong character is the rule browsers use for dir=auto."""
    for ch in text or "":
        kind = unicodedata.bidirectional(ch)
        if kind in ("R", "AL"):
            return "rtl"
        if kind == "L":
            return "ltr"
    return "ltr"


def css_stack(scripts: list[str]) -> str:
    """The comma-separated fallback list that goes after the brand font."""
    parts = [f"'{f}'" for f in scripts]
    parts += ["'Noto Sans'", "system-ui", "sans-serif"]
    return ", ".join(parts)
