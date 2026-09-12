"""Regressions from the full-code review. Each test is named for the bug it pins."""

from __future__ import annotations

import pytest

from app.agent import language as lang
from app.creative import photoref
from app.creative.brief import EXAMPLE, NEGATIVE_PROMPT_DEFAULT, CreativeBrief, check_brand_rules
from app.creative.photoreal import ENRICHMENT_MAX, photographic


# --------------------------------------------------------------------------- #
# brief validator
# --------------------------------------------------------------------------- #
def _with_prompt(prompt: str, **vd) -> CreativeBrief:
    return CreativeBrief.model_validate(
        {**EXAMPLE, "visual_direction": {**EXAMPLE["visual_direction"], "prompt": prompt, **vd}}
    )


def test_two_possessives_are_not_a_quotation():
    """'a potter's wheel ... the artisan's hands' was rejected as quoted copy."""
    _with_prompt("a potter's wheel in soft light, the artisan's hands shaping clay")


def test_real_quotes_are_still_rejected():
    with pytest.raises(ValueError):
        _with_prompt("a shop board that says 'big sale' in the window")
    with pytest.raises(ValueError):
        _with_prompt('a jar with "fresh" on it')


def test_mood_cannot_smuggle_text_into_the_image():
    with pytest.raises(ValueError):
        _with_prompt("warm kitchen", mood="bold typography with the slogan")
    _with_prompt("warm kitchen", mood="wholesome, homely")


def test_never_say_covers_hashtags_and_ignores_punctuation():
    class Brand:
        never_say = ["cheap", "100% pure"]

    brief = CreativeBrief.model_validate(
        {**EXAMPLE, "caption": {**EXAMPLE["caption"], "hashtags": ["#cheap"]}}
    )
    assert check_brand_rules(brief, Brand()) == ["cheap"]

    brief = CreativeBrief.model_validate({**EXAMPLE, "subhead": "100 % Pure, always"})
    assert check_brand_rules(brief, Brand()) == ["100% pure"]


def test_never_say_is_whole_phrase():
    class Brand:
        never_say = ["free"]

    brief = CreativeBrief.model_validate({**EXAMPLE, "headline": "Freedom Sale"})
    assert check_brand_rules(brief, Brand()) == []
    brief = CreativeBrief.model_validate({**EXAMPLE, "headline": "Free delivery"})
    assert check_brand_rules(brief, Brand()) == ["free"]


# --------------------------------------------------------------------------- #
# photoreal / photoref
# --------------------------------------------------------------------------- #
def test_enrichment_has_a_fixed_ceiling():
    base = "x" * 900
    prompt, _ = photographic(base, NEGATIVE_PROMPT_DEFAULT, mood="warm, homely")
    assert len(prompt) - len(base) <= ENRICHMENT_MAX


def test_asset_kind_is_not_evidence():
    """'New products this week' must not match every photo the brand owns."""

    class A:
        id, kind, label, width, height = "a", "product", None, 1600, 1600

    assert photoref.choose("New products this week", [A()]) is None


# --------------------------------------------------------------------------- #
# language
# --------------------------------------------------------------------------- #
def test_repeated_english_retail_words_do_not_flip_the_language():
    assert lang.detect("we sell banana chips and banana shake, make a post").language == "en"
    assert lang.detect("new matte lipstick and matte foundation launch").language == "en"
    assert lang.detect("please undo that, undo the last change").language == "en"


def test_marathi_example_spelling_is_a_marker():
    assert lang.detect("udya sale aahe, post kara").language == "mr"


def test_locale_round_trip_is_bcp47():
    assert lang.to_locale("hi") == "hi-IN"
    assert lang.to_locale("hi-IN") == "hi-IN"
    p = lang.from_locale("kn-IN")
    assert p.language == "kn" and p.script == "latin" and p.confidence >= 0.6
    assert lang.from_locale(None).language == "en"


def test_sorry_line_exists_for_every_locked_language():
    for code in ("hi", "kn", "ta", "te", "mr", "ml", "en"):
        assert lang.sorry_line(lang.LanguageProfile(language=code))
    assert lang.sorry_line(None)


def test_stt_hint_shapes():
    from app.integrations.stt.providers import bare_code, bcp47_code

    assert bare_code("hi-IN") == "hi"
    assert bcp47_code("hi") == "hi-IN"
    assert bcp47_code("kn-IN") == "kn-IN"


# --------------------------------------------------------------------------- #
# round two: what the adversarial pass found
# --------------------------------------------------------------------------- #
def test_voice_only_owner_keeps_their_language_in_latin_letters():
    """Language from the transcript, script defaults to Latin when never typed."""
    p = lang.from_locale("kn-IN", script=None, fallback=lang.detect("ನಾಳೆ ಸೇಲ್"))
    assert p.language == "kn" and p.script == "latin"


def test_devanagari_typist_is_not_told_to_romanise():
    p = lang.from_locale("hi-IN", script="devanagari", fallback=lang.detect(""))
    assert p.script == "devanagari" and p.code_mixed is False


def test_never_say_works_in_native_script():
    class Brand:
        never_say = ["मुफ़्त"]

    brief = CreativeBrief.model_validate({**EXAMPLE, "headline": "आज मुफ़्त डिलीवरी"})
    assert check_brand_rules(brief, Brand()) == ["मुफ़्त"]


def test_enrichment_ceiling_holds_for_the_longest_allowed_mood():
    from app.creative.brief import MOOD_MAX

    base = "x" * 900
    prompt, _ = photographic(base, NEGATIVE_PROMPT_DEFAULT, mood="m" * MOOD_MAX)
    assert len(prompt) - len(base) <= ENRICHMENT_MAX
    with pytest.raises(ValueError):
        _with_prompt("warm kitchen", mood="m" * (MOOD_MAX + 1))


async def test_concurrent_first_callers_launch_one_browser(monkeypatch):
    """Six slides of a carousel call get_browser at once; one Chromium, not six."""
    import asyncio
    import sys
    import types

    from app.creative import compose

    launches = []

    class FakeBrowser:
        def is_connected(self):
            return True

        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kw):
            launches.append(1)
            await asyncio.sleep(0.05)  # long enough for every caller to arrive
            return FakeBrowser()

    class FakePW:
        chromium = FakeChromium()

        async def stop(self):
            pass

    class _Starter:
        async def start(self):
            return FakePW()

    fake = types.ModuleType("playwright.async_api")
    fake.async_playwright = lambda: _Starter()
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake)
    monkeypatch.setattr(compose, "_browser", None)
    monkeypatch.setattr(compose, "_playwright", None)
    monkeypatch.setattr(compose, "_loop", None)
    monkeypatch.setattr(compose, "_lock", None)
    monkeypatch.setattr(compose, "_lock_loop", None)

    browsers = await asyncio.gather(*(compose.get_browser() for _ in range(6)))
    assert len(launches) == 1
    assert all(b is browsers[0] for b in browsers)
    await compose.shutdown()
