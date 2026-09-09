"""Language and script detection.

The cases here are written the way shop owners actually type, not the way
language samples are usually written. That distinction is the whole point:
almost nobody types Kannada in Kannada script on a phone keyboard.
"""

import pytest

from app.agent.language import detect, instruction

HINGLISH = "kal se weekend sale hai, coconut oil ka post banao"
KANGLISH = "nale sale ide, post madi swalpa"
KANNADA = "ನಾಳೆ ಸೇಲ್ ಇದೆ, ಪೋಸ್ಟ್ ಮಾಡಿ"
HINDI = "कल से वीकेंड सेल है, पोस्ट बनाओ"
TAMLISH = "naalaikku sale irukku, post pannunga"
ENGLISH = "please make me a poster for the weekend sale"


def test_native_script_is_detected_with_its_language():
    kn, hi = detect(KANNADA), detect(HINDI)
    assert (kn.language, kn.script) == ("kn", "kannada")
    assert (hi.language, hi.script) == ("hi", "devanagari")
    assert kn.confidence > 0.8 and hi.confidence > 0.8


def test_romanised_indic_keeps_the_latin_script():
    """The bug this prevents: replying in Kannada script to someone who
    typed Latin, which is unreadable on their phone."""
    p = detect(KANGLISH)
    assert p.language == "kn"
    assert p.script == "latin"
    assert p.is_romanised_indic is True


def test_hinglish_is_hindi_not_english():
    p = detect(HINGLISH)
    assert p.language == "hi"
    assert p.script == "latin"
    assert p.code_mixed is True


def test_tamil_romanised():
    assert detect(TAMLISH).language == "ta"


def test_plain_english_stays_english():
    p = detect(ENGLISH)
    assert p.language == "en"
    assert p.is_romanised_indic is False


@pytest.mark.parametrize("text", ["hi", "ok", "yes please", "thanks", ""])
def test_short_messages_do_not_guess(text):
    """One stray marker word must not flip a London client into Hinglish."""
    assert detect(text).language == "en"


def test_a_single_marker_is_not_enough():
    # "kal" alone in an English sentence is a coincidence, not a language.
    assert detect("what is the kal deadline for this poster").language == "en"


def test_instruction_tells_the_model_the_script_explicitly():
    romanised = instruction(detect(KANGLISH))
    assert "ENGLISH LETTERS" in romanised
    assert "Kannada written in English letters" in romanised

    native = instruction(detect(KANNADA))
    assert "Kannada script" in native
    assert "romanise" in native.lower()


def test_instruction_carries_a_worked_example():
    assert "Sari, nale sale post madtini" in instruction(detect(KANGLISH))
    assert "Theek hai, weekend sale ka post banata hoon" in instruction(detect(HINGLISH))


def test_low_confidence_falls_back_to_english_not_a_guess():
    block = instruction(detect(ENGLISH))
    assert "plain English" in block
    assert "ENGLISH LETTERS" not in block


def test_creative_copy_is_separated_from_chat_language():
    """A client may chat in Hinglish and want the poster in English."""
    assert "separate decision" in instruction(detect(HINGLISH))
