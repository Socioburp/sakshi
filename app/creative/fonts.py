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


def script_fonts_href(scripts: list[str]) -> str | None:
    """A stylesheet URL for the script faces alone, or None for Latin copy."""
    if not scripts:
        return None
    params = [_family_param(fam, SCRIPT_WEIGHTS) for fam in scripts]
    return f"{GOOGLE_CSS}?{'&'.join(params)}&display=block"


def css_stack(scripts: list[str]) -> str:
    """The comma-separated fallback list that goes after the brand font."""
    parts = [f"'{f}'" for f in scripts]
    parts += ["'Noto Sans'", "system-ui", "sans-serif"]
    return ", ".join(parts)
