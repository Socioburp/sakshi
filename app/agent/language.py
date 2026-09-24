"""Which language, and which script, to answer in.

The product decision this encodes: **reply in the script they typed in.**

A shop owner who writes "kal se sale hai" is writing Hindi in Latin letters
because that is how they type. Answering in Devanagari is not more respectful,
it is unreadable on their phone and it reads as a machine that did not notice.
The reverse is equally wrong: someone who types "ಕಲ್ ಸೇಲ್ ಇದೆ" wants Kannada
back, not "theek hai".

So a language decision here has two parts, always: the language, and the script
it should be written in. One without the other is a guess.

Detection is deliberately conservative. When the signal is weak we return
`unknown`, and the caller falls back to English -- a shop owner reading slightly
stiff English is a small cost, a shop owner reading confident Marathi they do
not speak is a lost client.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

# fmt: off
Script = Literal["latin", "devanagari", "kannada", "tamil", "telugu", "malayalam",
                 "bengali", "gujarati", "gurmukhi", "unknown"]

# Unicode blocks, in the order we test them.
SCRIPT_RANGES: list[tuple[Script, int, int]] = [
    ("devanagari", 0x0900, 0x097F),   # Hindi, Marathi
    ("bengali",    0x0980, 0x09FF),
    ("gurmukhi",   0x0A00, 0x0A7F),   # Punjabi
    ("gujarati",   0x0A80, 0x0AFF),
    ("tamil",      0x0B80, 0x0BFF),
    ("telugu",     0x0C00, 0x0C7F),
    ("kannada",    0x0C80, 0x0CFF),
    ("malayalam",  0x0D00, 0x0D7F),
]

# fmt: on

SCRIPT_DEFAULT_LANG: dict[Script, str] = {
    "devanagari": "hi",
    "bengali": "bn",
    "gurmukhi": "pa",
    "gujarati": "gu",
    "tamil": "ta",
    "telugu": "te",
    "kannada": "kn",
    "malayalam": "ml",
}

# Romanised markers. Chosen to be distinctive: short words that collide with
# English ("hi", "me", "ka", "to") are deliberately excluded, because one false
# positive means answering a London client in Hinglish.
# fmt: off
ROMAN_MARKERS: dict[str, set[str]] = {
    "hi": {
        "kya", "hai", "hain", "nahi", "nahin", "karo", "karna", "kijiye", "chahiye",
        "banao", "banade", "bana", "aur", "mera", "meri", "mujhe", "hamara",
        "aapka", "apna", "tumhara", "kal", "aaj", "abhi", "thoda", "bahut", "accha",
        "achha", "theek", "thik", "matlab", "wala", "wali", "jaldi", "dijiye", "bhej",
        "bhejo", "dekho", "chalega", "hoga", "kaise", "kaisa", "kitna", "sirf", "ekdum",
    },
    "kn": {
        "madi", "maadi", "beku", "bekku", "illa", "ide", "hege", "enu", "yenu",
        "swalpa", "olle", "chennagi", "nanna", "nimma", "agatte",
        "banni", "kodi", "thumba", "tumba", "yaake", "aytu", "bittu",
    },
    "ta": {
        "pannunga", "panunga", "venum", "illai", "enna", "romba", "nalla", "seri",
        "kudunga", "irukku", "vanakkam", "epdi", "yaaru", "konjam",
    },
    "te": {
        "cheyandi", "kavali", "ledu", "enti", "chala", "bagundi", "ivvandi", "undi",
        "meeru", "nenu", "ela", "koncham",
    },
    "mr": {
        "kara", "karaycha", "pahije", "kasa", "changla", "tumhala", "mala", "ahe", "aahe",
        "nahiye", "kiti", "thoda",
    },
    "ml": {
        "cheyyu", "venam", "enthu", "kollam", "njan", "ningal", "ethra",
        "sheri", "ittiri",
    },
}

# fmt: on

# Words that mean the message is plain English even if a marker slipped through.
_WORD = re.compile(r"[a-z]+")

MIN_MARKERS = 2  # one match is a coincidence
MIN_MARKER_RATIO = 0.12  # ...and it has to be a real share of a short message


@dataclass(frozen=True)
class LanguageProfile:
    """How to write back to this person."""

    language: str = "en"
    script: Script = "latin"
    code_mixed: bool = False
    confidence: float = 0.0

    @property
    def is_romanised_indic(self) -> bool:
        return self.script == "latin" and self.language != "en"

    def label(self) -> str:
        names = {
            "en": "English",
            "hi": "Hindi",
            "kn": "Kannada",
            "ta": "Tamil",
            "te": "Telugu",
            "ml": "Malayalam",
            "mr": "Marathi",
            "bn": "Bengali",
            "pa": "Punjabi",
            "gu": "Gujarati",
        }
        name = names.get(self.language, self.language)
        if self.is_romanised_indic:
            return f"{name} written in English letters"
        if self.script != "latin":
            return f"{name} in {self.script.title()} script"
        return name


def detect_script(text: str) -> tuple[Script, float]:
    """Returns the dominant non-Latin script and how much of the text it covers."""
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        return "unknown", 0.0
    counts: dict[Script, int] = {}
    for ch in letters:
        cp = ord(ch)
        for name, lo, hi in SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    if not counts:
        return "latin", 1.0
    best = max(counts, key=lambda k: counts[k])
    return best, counts[best] / len(letters)


def detect(text: str | None) -> LanguageProfile:
    if not text or not text.strip():
        return LanguageProfile()

    lowered = text.lower()
    script, share = detect_script(text)

    # Native script wins outright -- it is unambiguous in a way word lists never are.
    if script in SCRIPT_DEFAULT_LANG and share >= 0.20:
        latin_words = len(_WORD.findall(lowered))
        return LanguageProfile(
            language=SCRIPT_DEFAULT_LANG[script],
            script=script,
            code_mixed=latin_words >= 2,
            confidence=min(1.0, 0.6 + share * 0.4),
        )

    words = _WORD.findall(lowered)
    if not words:
        return LanguageProfile()

    scores = {
        lang: len({w for w in words if w in markers}) for lang, markers in ROMAN_MARKERS.items()
    }
    lang = max(scores, key=lambda k: scores[k])
    hits = scores[lang]
    ratio = hits / len(words)

    if hits >= MIN_MARKERS and ratio >= MIN_MARKER_RATIO:
        return LanguageProfile(
            language=lang,
            script="latin",
            code_mixed=ratio < 0.6,  # mixed with English, which is the normal case
            confidence=min(1.0, 0.4 + ratio),
        )

    return LanguageProfile(language="en", script="latin", confidence=0.5 if words else 0.0)


# --------------------------------------------------------------------------- #
# the locale column, in one shape
# --------------------------------------------------------------------------- #
# accounts.locale is BCP-47 with a region ("hi-IN"), because that is what the
# speech-to-text providers take as a hint. The detector works in bare ISO
# codes. These two functions are the only place the conversion happens.

_REGION = "IN"


def to_locale(language: str | None) -> str:
    lang = (language or "en").split("-")[0].lower()
    return f"{lang}-{_REGION}"


def from_locale(
    locale: str | None,
    script: str | None = None,
    fallback: LanguageProfile | None = None,
) -> LanguageProfile:
    """A profile for a locked locale -- used when this message carries no signal.

    `script` is what the owner has TYPED in (accounts.script). With none on
    record -- an owner who has only ever sent voice notes -- the reply goes in
    Latin letters, which is what an Indian phone keyboard produces and what a
    transcript's Devanagari must not override.
    """
    if not locale:
        return fallback or LanguageProfile()
    lang = locale.split("-")[0].lower()
    if lang == "en":
        return LanguageProfile(language="en", script="latin", confidence=0.6)
    if lang in ROMAN_MARKERS or lang in SCRIPT_DEFAULT_LANG.values():
        chosen = script if script and script != "unknown" else "latin"
        return LanguageProfile(
            language=lang,
            script=chosen,  # type: ignore[arg-type]
            code_mixed=(chosen == "latin"),
            confidence=0.6,
        )
    return fallback or LanguageProfile()


_SORRY: dict[str, str] = {
    "hi": "Ek second — kuch gadbad ho gayi. Dobara bhejein?",
    "kn": "Ondu nimisha — swalpa problem aaytu. Matte kalisi?",
    "ta": "Oru nimisham — konjam problem aachu. Thirumba anuppunga?",
    "te": "Oka nimisham — chinna problem vachindi. Malli pampandi?",
    "mr": "Ek minute — kahitari chukla. Parat pathva?",
    "ml": "Oru nimisham — cheriya problem undayi. Onnude ayakkamo?",
    "en": "One second — something went wrong on my side. Please send that again?",
}


def sorry_line(profile: LanguageProfile | None) -> str:
    """The one message an owner gets when a turn fails. Never silence."""
    lang = (profile.language if profile else "en") or "en"
    return _SORRY.get(lang, _SORRY["en"])


# --------------------------------------------------------------------------- #
# what the model is told
# --------------------------------------------------------------------------- #
_EXAMPLES: dict[str, str] = {
    "hi": (
        'They write "kal se weekend sale hai" -> you write '
        '"Theek hai, weekend sale ka post banata hoon. Kya offer hai?"'
    ),
    "kn": (
        'They write "nale sale ide, post madi" -> you write '
        '"Sari, nale sale post madtini. Offer yenu?"'
    ),
    "ta": (
        'They write "naalaikku sale, post pannunga" -> you write '
        '"Seri, naalaikku sale post panren. Enna offer?"'
    ),
    "te": (
        'They write "repu sale undi, post cheyandi" -> you write '
        '"Sare, repu sale post chesta. Offer enti?"'
    ),
    "mr": (
        'They write "udya sale ahe, post kara" -> you write '
        '"Theek aahe, udya sale cha post karto. Offer kay aahe?"'
    ),
    "ml": (
        'They write "naale sale und, post cheyyu" -> you write '
        '"Sheri, naale sale post cheyyam. Entha offer?"'
    ),
}


def instruction(profile: LanguageProfile) -> str:
    """The language rule, written as a rule rather than a hope."""
    if profile.language == "en" or profile.confidence < 0.4:
        return (
            "## Language\n"
            "Write in plain English. Short sentences, the way a person texts -- not "
            "marketing English. If they switch to another language, switch with them "
            "from the next message."
        )

    lines = [
        "## Language",
        f"This owner writes in **{profile.label()}**. Answer the same way.",
    ]

    if profile.is_romanised_indic:
        lines.append(
            "Write it in ENGLISH LETTERS, exactly as they did. Do not switch to the "
            "native script -- they type in Latin because that is how their keyboard "
            "works, and a reply in another script is unreadable to them."
        )
    else:
        lines.append(f"Write in {profile.script.title()} script, as they did. Do not romanise it.")

    if profile.code_mixed:
        lines.append(
            "They mix in English words. Mix the same way -- forcing pure vocabulary "
            "sounds like a textbook, not a person."
        )

    if example := _EXAMPLES.get(profile.language):
        lines.append(example)

    lines.append(
        "The creative's copy is a separate decision from this chat. Headline and CTA "
        "go in whatever language their CUSTOMERS read, which is usually this one -- "
        "but ask once if it is genuinely unclear, and never more than once."
    )
    return "\n".join(lines)
